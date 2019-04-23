# Copyright 2018 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
r"""A script to deploy monitored projects.

Create a project config YAML file (see README.md for details) then run the
script with:
  bazel run :create_project -- \
    --project_yaml=my_project_config.yaml \
    --projects='*' \
    --output_yaml_path=/tmp/output.yaml \
    --output_cleanup_path=/tmp/cleanup.sh \
    --nodry_run \
    --alsologtostderr

To preview the commands that will run, use `--dry_run`.

After the script has finished executing (success or failure), be sure to sync
--output_yaml_path with --project_yaml.
"""

from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

import collections
import copy
import os
import subprocess
import time

from absl import app
from absl import flags
from absl import logging

import jsonschema

from ruamel import yaml

from deploy.rule_generator import rule_generator
from deploy.utils import field_generating
from deploy.utils import forseti
from deploy.utils import runner
from deploy.utils import utils

FLAGS = flags.FLAGS

flags.DEFINE_string('project_yaml', None,
                    'Location of the project config YAML.')
flags.DEFINE_list('projects', ['*'],
                  ('Project IDs within --project_yaml to deploy, '
                   'or "*" to deploy all projects.'))
flags.DEFINE_string('output_yaml_path', None,
                    ('Path to save a new YAML file with any '
                     'environment variables substituted and generated '
                     'fields populated. This can be the same as project_yaml '
                     'to overwrite the original.'))
flags.DEFINE_string('output_rules_path', None,
                    ('Path to local directory or GCS bucket to output rules '
                     'files. If unset, directly writes to the Forseti server '
                     'bucket.'))
flags.DEFINE_string('output_cleanup_path', None, 'Path to save cleanup file.')
flags.DEFINE_boolean('enable_new_style_resources', None, 'Enable new style '
                     'resources. Developer only.')
flags.DEFINE_string('cft_binary', None,
                    'Path to CFT binary. Set automatically by Bazel rule.')

# Name of the Log Sink created in the data_project deployment manager template.
_LOG_SINK_NAME = 'audit-logs-to-bigquery'

# Roles to temporarily grant the deployment manager service account to function.
_DEPLOYMENT_MANAGER_ROLES = ['roles/owner', 'roles/storage.admin']

# IAM binding changes can take some time to propagate to child resources, so
# wait to give it enough time.
_IAM_PROPAGATAION_WAIT_TIME_SECS = 60

# Restriction for project lien.
_LIEN_RESTRICTION = 'resourcemanager.projects.delete'

_CLEANUP_HEADER = """#!/bin/sh

# This file contains commands to remove unexpected configuration.
# If they are legitimate, consider adding them to the input YAML config so they
# don't get flagged again.
# WARNING: proceed with caution!

set -e

"""

# Configuration for deploying a single project.
ProjectConfig = collections.namedtuple(
    'ProjectConfig',
    [
        # Dictionary of configuration values of the entire config.
        'root',
        # Dictionary of configuration values for this project.
        'project',
        # Dictionary of configuration values of the remote audit logs project,
        # or None if the project uses local logs.
        'audit_logs_project',
        # Extra steps to perform for this project.
        'extra_steps',
    ])

Step = collections.namedtuple(
    'Step',
    [
        # Function that implements this step.
        'func',
        # Description of the step.
        'description',
        # Whether this step should be run when updating a project.
        'updatable',
    ])

Output = collections.namedtuple('Output', ['cleanup_commands'])


def create_new_project(config):
  """Creates the new GCP project."""
  project_id = config.project['project_id']

  overall_config = config.root['overall']
  org_id = overall_config.get('organization_id')
  folder_id = overall_config.get('folder_id')

  create_project_command = ['projects', 'create', project_id]
  if folder_id:
    create_project_command.extend(['--folder', folder_id])
  elif org_id:
    create_project_command.extend(['--organization', org_id])
  else:
    logging.info('Deploying without a parent organization or folder.')
  # Create the new project.
  runner.run_gcloud_command(create_project_command, project_id=None)


def setup_billing(config):
  """Sets the billing account for this project."""
  billing_acct = config.root['overall']['billing_account']
  project_id = config.project['project_id']
  # Set the appropriate billing account for this project:
  runner.run_gcloud_command([
      'beta', 'billing', 'projects', 'link', project_id, '--billing-account',
      billing_acct
  ],
                            project_id=None)


def enable_deployment_manager(config):
  """Enables Deployment manager, with role/owners for its service account."""
  project_id = config.project['project_id']

  # Enabled Deployment Manger and Cloud Resource Manager for this project.
  runner.run_gcloud_command([
      'services', 'enable', 'deploymentmanager',
      'cloudresourcemanager.googleapis.com'
  ],
                            project_id=project_id)

  # Grant deployment manager service account (temporary) owners access.
  dm_service_account = utils.get_deployment_manager_service_account(project_id)
  for role in _DEPLOYMENT_MANAGER_ROLES:
    runner.run_gcloud_command([
        'projects', 'add-iam-policy-binding', project_id, '--member',
        dm_service_account, '--role', role
    ],
                              project_id=None)

  logging.info('Sleeping for %d seconds to let IAM updates propagate.',
               _IAM_PROPAGATAION_WAIT_TIME_SECS)
  runner.run(time.sleep, _IAM_PROPAGATAION_WAIT_TIME_SECS)


def enable_services_apis(config):
  """Enables services for this project.

  Use this function instead of enabling private APIs in deployment manager
  because deployment-management does not have all the APIs' access, which might
  triger PERMISSION_DENIED errors.

  Args:
    config (ProjectConfig): The config of a single project to setup.

  Returns:
    List[string]: commands to remove APIs not found in the enabled set.
  """
  project_id = config.project['project_id']
  existing_services = runner.run_gcloud_command(
      ['services', 'list', '--format', 'value(name)'],
      project_id=project_id).split('\n')

  # The gcloud call returns service name as a full path (including project id).
  # We only need the base name which is the URL.
  existing_apis = set([os.path.basename(svc) for svc in existing_services])

  want_apis = set(config.project.get('enabled_apis', []))

  unexpected_apis = existing_apis.difference(want_apis)
  unenabled_apis = list(want_apis.difference(existing_apis))

  # Send in batches to avoid hitting quota limits.
  for i in range(0, len(unenabled_apis), 10):
    runner.run_gcloud_command(
        ['services', 'enable'] + unenabled_apis[i:i + 10],
        project_id=project_id)

  cleanup_commands = [
      'gcloud services disable {} --project={}'.format(api, project_id)
      for api in unexpected_apis
  ]
  return Output(cleanup_commands=cleanup_commands)


def deploy_gcs_audit_logs(config):
  """Deploys the GCS logs bucket to the remote audit logs project, if used."""
  # The GCS logs bucket must be created before the data buckets.
  if not config.audit_logs_project:
    logging.info('Using local GCS audit logs.')
    return
  logs_gcs_bucket = config.project['audit_logs'].get('logs_gcs_bucket')
  if not logs_gcs_bucket:
    logging.info('No remote GCS logs bucket required.')
    return

  logging.info('Creating remote GCS logs bucket.')
  data_project_id = config.project['project_id']
  logs_project = config.audit_logs_project
  audit_project_id = logs_project['project_id']

  deployment_name = 'audit-logs-{}-gcs'.format(
      data_project_id.replace('_', '-'))
  path = os.path.join(
      os.path.dirname(__file__), 'templates/remote_audit_logs.py')
  dm_template_dict = {
      'imports': [{
          'path': path
      }],
      'resources': [{
          'type': path,
          'name': deployment_name,
          'properties': {
              'owners_group': logs_project['owners_group'],
              'auditors_group': config.project['auditors_group'],
              'logs_gcs_bucket': logs_gcs_bucket,
          },
      }]
  }
  utils.run_deployment(dm_template_dict, deployment_name, audit_project_id)


def _is_service_enabled(service_name, project_id):
  """Check if the service_name is already enabled."""
  enabled_services = runner.run_gcloud_command(
      ['services', 'list', '--format', 'value(NAME)'], project_id=project_id)
  services_list = enabled_services.strip().split('\n')
  return service_name in services_list


def get_data_bucket_name(data_bucket, project_id):
  """Get the name of data buckets."""
  if 'name' not in data_bucket:
    if 'name_suffix' not in data_bucket:
      raise utils.InvalidConfigError(
          'Data buckets must contains either name or name_suffix')
    return project_id + data_bucket['name_suffix']
  else:
    if 'name_suffix' in data_bucket:
      raise utils.InvalidConfigError(
          'Data buckets must not contains both name and name_suffix')
    return data_bucket['name']


def deploy_project_resources(config):
  """Deploys resources into the new data project."""
  setup_account = utils.get_gcloud_user()
  has_organization = bool(config.root['overall'].get('organization_id'))
  project_id = config.project['project_id']
  dm_service_account = utils.get_deployment_manager_service_account(project_id)

  # Build a deployment config for the data_project.py deployment manager
  # template.
  properties = copy.deepcopy(config.project)
  # Remove the current user as an owner of the project if project is part of an
  # organization.
  properties['has_organization'] = has_organization
  if has_organization:
    properties['remove_owner_user'] = setup_account

  # Change audit_logs to either local_audit_logs or remote_audit_logs in the
  # deployment manager template properties.
  audit_logs = properties.pop('audit_logs')
  if config.audit_logs_project:
    properties['remote_audit_logs'] = {
        'audit_logs_project_id': config.audit_logs_project['project_id'],
        'logs_bigquery_dataset_id': audit_logs['logs_bigquery_dataset']['name'],
    }
    # Logs GCS bucket is not required for projects without data GCS buckets.
    if 'logs_gcs_bucket' in audit_logs:
      properties['remote_audit_logs']['logs_gcs_bucket_name'] = (
          audit_logs['logs_gcs_bucket']['name'])
  else:
    properties['local_audit_logs'] = audit_logs

  # Set data buckets' names.
  for data_bucket in properties.get('data_buckets', []):
    data_bucket['name'] = get_data_bucket_name(data_bucket, project_id)
    data_bucket.pop('name_suffix', '')

  path = os.path.join(os.path.dirname(__file__), 'templates/data_project.py')
  dm_template_dict = {
      'imports': [{
          'path': path
      }],
      'resources': [{
          'type': path,
          'name': 'data_project_deployment',
          'properties': properties,
      }]
  }

  # API iam.googleapis.com is necessary when using custom roles
  iam_api_disable = False
  if not _is_service_enabled('iam.googleapis.com', project_id):
    runner.run_gcloud_command(['services', 'enable', 'iam.googleapis.com'],
                              project_id=project_id)
    iam_api_disable = True
  try:
    # Create the deployment.
    utils.run_deployment(dm_template_dict, 'data-project-deployment',
                         project_id)

    # Create project liens if requested.
    if config.project.get('create_deletion_lien'):
      existing_restrictions = runner.run_gcloud_command(
          [
              'alpha', 'resource-manager', 'liens', 'list', '--format',
              'value(restrictions)'
          ],
          project_id=project_id).split('\n')

      if _LIEN_RESTRICTION not in existing_restrictions:
        runner.run_gcloud_command([
            'alpha', 'resource-manager', 'liens', 'create', '--restrictions',
            _LIEN_RESTRICTION, '--reason',
            'Automated project deletion lien deployment.'
        ],
                                  project_id=project_id)

    for role in _DEPLOYMENT_MANAGER_ROLES:
      runner.run_gcloud_command([
          'projects', 'remove-iam-policy-binding', project_id, '--member',
          dm_service_account, '--role', role
      ],
                                project_id=None)

  finally:
    # Disable iam.googleapis.com if it is enabled in this function
    if iam_api_disable:
      runner.run_gcloud_command(['services', 'disable', 'iam.googleapis.com'],
                                project_id=project_id)


def deploy_new_style_resources(config):
  """Deploy new style resources."""
  if FLAGS.enable_new_style_resources:
    subprocess.check_call([
        FLAGS.cft_binary,
        '--project_yaml_path',
        FLAGS.project_yaml,
        '--project',
        config.project['project_id'],
    ])


def get_iam_policy_cleanup(config):
  """Get cleanup commands for unexpected IAM bindings."""
  project_id = config.project['project_id']
  policy_str = runner.run_gcloud_command(
      ['projects', 'get-iam-policy', project_id], project_id=project_id)
  policy = yaml.YAML().load(policy_str)

  logging.info('\n\naaaaaaaaaaaaaaaaaaaaaaaaaaaa\n\n')

  existing_role_to_members = _get_role_to_members(policy['bindings'])

  # TODO: avoid duplication with data_project.py and rule generator
  # project config once we switch to CFT
  owners_group_role = ('roles/owner'
                       if 'organization_id' in config.root['overall'] else
                       'roles/resourcemanager.projectIamAdmin')
  initial_bindings = [
      {
          'role': owners_group_role,
          'members': ['group:{}'.format(config.project['owners_group'])]
      },
      {
          'role': 'roles/iam.securityReviewer',
          'members': ['group:{}'.format(config.project['auditors_group'])]
      },
  ]
  if 'editors_group' in config.project:
    initial_bindings.append({
        'role':
            'roles/editor',
        'members': [
            'group:{}'.format(group)
            for group in config.project['editors_group']
        ]
    })
  for additional in config.project.get('additional_project_permissions', []):
    for role in additional['roles']:
      initial_bindings.append({
          'role': role,
          'members': additional['members'],
      })
  if 'forseti' in config.root:
    forseti_service_account = field_generating.get_generated_fields_copy(
        'forseti', config.root).get('service_account')
    if forseti_service_account:
      for role in forseti.get_forseti_roles(project_id):
        initial_bindings.append({
            'role': role,
            'members': ['serviceAccount:{}'.format(forseti_service_account)],
        })

  want_role_to_members = _get_role_to_members(initial_bindings)

  for role, members in existing_role_to_members.items():
    existing_role_to_members[role].difference_update(want_role_to_members[role])

  cleanup_commands = []
  for role, members in existing_role_to_members.items():
    for member in members:
      cleanup_commands.append(
          'gcloud projects remove-iam-policy-binding {project_id} '
          '--member={member} --role={role} --project={project_id}'.format(
              project_id=project_id, member=member, role=role))
  logging.info('\n\nbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb\n\n')

  return Output(cleanup_commands=cleanup_commands)


def _get_role_to_members(bindings):
  res = collections.defaultdict(set)
  for binding in bindings:
    res[binding['role']].update(set(binding['members']))
  return res


def deploy_bigquery_audit_logs(config):
  """Deploys the BigQuery audit logs dataset, if used."""
  data_project_id = config.project['project_id']
  logs_dataset = copy.deepcopy(
      config.project['audit_logs']['logs_bigquery_dataset'])
  if config.audit_logs_project:
    logging.info('Creating remote BigQuery logs dataset.')
    audit_project_id = config.audit_logs_project['project_id']
    owners_group = config.audit_logs_project['owners_group']
  else:
    logging.info('Creating local BigQuery logs dataset.')
    audit_project_id = data_project_id
    logs_dataset['name'] = 'audit_logs'
    owners_group = config.project['owners_group']

  # Get the service account for the newly-created log sink.
  logs_dataset['log_sink_service_account'] = utils.get_log_sink_service_account(
      _LOG_SINK_NAME, data_project_id)

  deployment_name = 'audit-logs-{}-bq'.format(data_project_id.replace('_', '-'))
  path = os.path.join(
      os.path.dirname(__file__), 'templates/remote_audit_logs.py')
  dm_template_dict = {
      'imports': [{
          'path': path
      }],
      'resources': [{
          'type': path,
          'name': deployment_name,
          'properties': {
              'owners_group': owners_group,
              'auditors_group': config.project['auditors_group'],
              'logs_bigquery_dataset': logs_dataset,
          },
      }]
  }
  utils.run_deployment(dm_template_dict, deployment_name, audit_project_id)


def create_compute_images(config):
  """Creates new Compute Engine VM images if specified in config.

  Note: for updates, only new images will be created. Existing images will not
  be modified.

  Args:
    config (ProjectConfig): config of the project.
  """
  gce_instances = config.project.get('gce_instances')
  if not gce_instances:
    logging.info('No GCS Images required.')
    return
  project_id = config.project['project_id']

  for instance in gce_instances:
    custom_image = instance.get('custom_boot_image')
    if not custom_image:
      logging.info('Using existing compute image %s.',
                   instance['existing_boot_image'])
      continue
    # Check if custom image already exists.
    if runner.run_gcloud_command([
        'compute', 'images', 'list', '--no-standard-images', '--filter',
        'name={}'.format(custom_image['image_name']), '--format', 'value(name)'
    ],
                                 project_id=project_id):
      logging.info('Image %s already exists, skipping image creation.',
                   custom_image['image_name'])
      continue
    logging.info('Creating VM Image %s.', custom_image['image_name'])

    # Create VM image using gcloud rather than deployment manager so that the
    # deployment manager service account doesn't need to be granted access to
    # the image GCS bucket.
    image_uri = 'gs://' + custom_image['gcs_path']
    runner.run_gcloud_command([
        'compute', 'images', 'create', custom_image['image_name'],
        '--source-uri', image_uri
    ],
                              project_id=project_id)


def create_compute_vms(config):
  """Creates new GCE VMs and firewall rules if specified in config."""
  if 'gce_instances' not in config.project:
    logging.info('No GCS VMs required.')
    return
  project_id = config.project['project_id']
  logging.info('Creating GCS VMs.')

  # Enable OS Login for VM SSH access.
  runner.run_gcloud_command([
      'compute', 'project-info', 'add-metadata', '--metadata',
      'enable-oslogin=TRUE'
  ],
                            project_id=project_id)

  gce_instances = []
  for instance in config.project['gce_instances']:
    if 'existing_boot_image' in instance:
      image_name = instance['existing_boot_image']
    else:
      image_name = ('global/images/' +
                    instance['custom_boot_image']['image_name'])

    gce_template_dict = {
        'name': instance['name'],
        'zone': instance['zone'],
        'machine_type': instance['machine_type'],
        'boot_image_name': image_name,
        'start_vm': instance['start_vm']
    }
    startup_script_str = instance.get('startup_script')
    if startup_script_str:
      gce_template_dict['metadata'] = {
          'items': [{
              'key': 'startup-script',
              'value': startup_script_str
          }]
      }
    gce_instances.append(gce_template_dict)

  deployment_name = 'gce-vms'
  path = os.path.join(os.path.dirname(__file__), 'templates/gce_vms.py')

  resource = {
      'type': path,
      'name': deployment_name,
      'properties': {
          'gce_instances': gce_instances,
          'firewall_rules': config.project.get('gce_firewall_rules', []),
      }
  }
  dm_template_dict = {
      'imports': [{
          'path': path
      }],
      'resources': [resource],
  }

  deployed = utils.deployment_exists(deployment_name, project_id)
  try:
    utils.run_deployment(dm_template_dict, deployment_name, project_id)
  except subprocess.CalledProcessError:
    # Only retry vm deployment for updates
    if not deployed:
      raise

    # TODO: check error message for whether failure was specifically
    # due to vm running
    logging.info(('Potential failure due to updating a running vm. '
                  'Retrying with vm shutdown.'))

    vm_names_to_shutdown = [
        instance['name']
        for instance in config.project.get('gce_instances', [])
    ]
    resource['properties']['vm_names_to_shutdown'] = vm_names_to_shutdown
    utils.run_deployment(dm_template_dict, deployment_name, project_id)


def create_stackdriver_account(config):
  """Prompts the user to create a new Stackdriver Account."""
  # Creating a Stackdriver account cannot be done automatically, so ask the
  # user to create one.
  if 'stackdriver_alert_email' not in config.project:
    logging.warning('No Stackdriver alert email specified, skipping creation '
                    'of Stackdriver account.')
    return
  project_id = config.project['project_id']

  if _stackdriver_account_exists(project_id):
    logging.info('Stackdriver account already exists')
    return

  message = """
  ------------------------------------------------------------------------------
  To create email alerts, this project needs a Stackdriver account.
  Create a new Stackdriver account for this project by visiting:
      https://console.cloud.google.com/monitoring?project={}

  Only add this project, and skip steps for adding additional GCP or AWS
  projects. You don't need to install Stackdriver Agents.

  IMPORTANT: Wait about 5 minutes for the account to be created.

  For more information, see: https://cloud.google.com/monitoring/accounts/

  After the account is created, enter [Y] to continue, or enter [N] to skip the
  creation of Stackdriver alerts.
  ------------------------------------------------------------------------------
  """.format(project_id)
  print(message)

  # Keep trying until Stackdriver account is ready, or user skips.
  while True:
    if not utils.wait_for_yes_no('Account created [y/N]?'):
      logging.warning('Skipping creation of Stackdriver Account.')
      break

    if _stackdriver_account_exists(project_id):
      break


def _stackdriver_account_exists(project_id):
  """Determine whether the stackdriver account exists."""
  try:
    runner.run_gcloud_command(['alpha', 'monitoring', 'policies', 'list'],
                              project_id=project_id)
    return True
  except subprocess.CalledProcessError as e:
    logging.warning(
        'Error reading Stackdriver account (likely does not exist): %s', e)
    return False


def create_alerts(config):
  """"Creates Stackdriver alerts for logs-based metrics."""
  # Stackdriver alerts can't yet be created in Deployment Manager, so create
  # them here.
  alert_email = config.project.get('stackdriver_alert_email')
  if alert_email is None:
    logging.warning('No Stackdriver alert email specified, skipping creation '
                    'of Stackdriver alerts.')
    return
  project_id = config.project['project_id']

  existing_channels_str = runner.run_gcloud_command([
      'alpha', 'monitoring', 'channels', 'list', '--format',
      'value(name,labels.email_address)'
  ],
                                                    project_id=project_id)

  existing_channels = existing_channels_str.split(
      '\n') if existing_channels_str else []

  email_to_channel = {}
  for existing_channel in existing_channels:
    channel, email = existing_channel.split()

    # assume only one channel exists per email
    email_to_channel[email] = channel

  if alert_email in email_to_channel:
    logging.info('Stackdriver notification channel already exists for %s',
                 alert_email)
    channel = email_to_channel[alert_email]
  else:
    logging.info('Creating Stackdriver notification channel.')
    channel = utils.create_notification_channel(alert_email, project_id)

  existing_alerts = runner.run_gcloud_command([
      'alpha', 'monitoring', 'policies', 'list', '--format',
      'value(displayName)'
  ],
                                              project_id=project_id).split('\n')

  existing_alerts = set(existing_alerts)

  logging.info('Creating Stackdriver alerts.')
  display_name = 'IAM Policy Change Alert'
  if display_name not in existing_alerts:
    utils.create_alert_policy(
        ['global', 'pubsub_topic', 'pubsub_subscription', 'gce_instance'],
        'iam-policy-change-count', display_name,
        ('This policy ensures the designated user/group is notified when IAM '
         'policies are altered.'), channel, project_id)

  display_name = 'Bucket Permission Change Alert'
  if display_name not in existing_alerts:
    utils.create_alert_policy(
        ['gcs_bucket'], 'bucket-permission-change-count', display_name,
        ('This policy ensures the designated user/group is notified when '
         'bucket/object permissions are altered.'), channel, project_id)

  display_name = 'Bigquery Update Alert'
  if display_name not in existing_alerts:
    utils.create_alert_policy(
        ['global'], 'bigquery-settings-change-count', display_name,
        ('This policy ensures the designated user/group is notified when '
         'Bigquery dataset settings are altered.'), channel, project_id)

  for data_bucket in config.project.get('data_buckets', []):
    # Every bucket with 'expected_users' has an expected-access alert.
    if 'expected_users' not in data_bucket:
      continue

    bucket_name = get_data_bucket_name(data_bucket, project_id)
    metric_name = 'unexpected-access-' + bucket_name
    display_name = 'Unexpected Access to {} Alert'.format(bucket_name)
    if display_name not in existing_alerts:
      utils.create_alert_policy(
          ['gcs_bucket'], metric_name, display_name,
          ('This policy ensures the designated user/group is notified when '
           'bucket {} is accessed by an unexpected user.'.format(bucket_name)),
          channel, project_id)


def add_project_generated_fields(config):
  """Adds a generated_fields block to a project definition."""
  project_id = config.project['project_id']

  generated_fields = field_generating.create_and_get_generated_fields_ref(
      project_id, config.root)

  if 'project_number' not in generated_fields:
    generated_fields['project_number'] = utils.get_project_number(project_id)

  if 'log_sink_service_account' not in generated_fields:
    generated_fields[
        'log_sink_service_account'] = utils.get_log_sink_service_account(
            _LOG_SINK_NAME, project_id)

  gce_instance_info = utils.get_gce_instance_info(project_id)
  if gce_instance_info:
    generated_fields['gce_instance_info'] = gce_instance_info


# The steps to set up a project, so the script can be resumed part way through
# on error. Each func takes a config dictionary.
_SETUP_STEPS = [
    Step(
        func=create_new_project,
        description='Create project',
        updatable=False,
    ),
    Step(
        func=setup_billing,
        description='Set up billing',
        updatable=False,
    ),
    Step(
        func=enable_deployment_manager,
        description='Enable deployment manager',
        updatable=True,
    ),
    Step(
        func=deploy_gcs_audit_logs,
        description='Deploy GCS audit logs',
        updatable=False,
    ),
    Step(
        func=deploy_project_resources,
        description='Deploy project resources',
        updatable=True,
    ),
    Step(
        func=deploy_new_style_resources,
        description='Deploy new style resources',
        updatable=True,
    ),
    Step(
        func=get_iam_policy_cleanup,
        description='Generate IAM policy cleanup',
        updatable=True,
    ),
    Step(
        func=deploy_bigquery_audit_logs,
        description='Deploy BigQuery audit logs',
        updatable=False,
    ),
    Step(
        func=create_compute_images,
        description='Deploy compute images',
        updatable=True,
    ),
    Step(
        func=create_compute_vms,
        description='Deploy GCE VMs',
        updatable=True,
    ),
    Step(
        func=enable_services_apis,
        description='Enable APIs',
        updatable=True,
    ),
    Step(
        func=create_stackdriver_account,
        description='Create Stackdriver account',
        updatable=True,
    ),
    Step(
        func=create_alerts,
        description='Create Stackdriver alerts',
        updatable=True,
    ),
    Step(
        func=add_project_generated_fields,
        description='Generate project fields',
        updatable=True,
    ),
]


def setup_project(config, output_yaml_path, output_cleanup_path):
  """Run the full process for initalizing a single new project.

  Note: for projects that have already been deployed, only the updatable steps
  will be run.

  Args:
    config (ProjectConfig): The config of a single project to setup.
    output_yaml_path (str): Path to output resulting root config in JSON.
    output_cleanup_path (str): Path to output cleanup shell script.

  Returns:
    A boolean, true if the project was deployed successfully, false otherwise.
  """
  project_id = config.project['project_id']
  steps = _SETUP_STEPS + config.extra_steps

  starting_step = field_generating.get_generated_fields_copy(
      project_id, config.root).get('failed_step', 1)

  deployed = field_generating.is_deployed(project_id, config.project)

  total_steps = len(steps)
  for step_num in range(starting_step, total_steps + 1):
    step = steps[step_num - 1]
    project_id = config.project['project_id']
    logging.info('%s: step %d/%d (%s)', project_id, step_num, total_steps,
                 step.description)

    if deployed and not step.updatable:
      logging.info('Step %d is not updatable, skipping', step_num)
      continue

    try:
      output = step.func(config)
      if output and output.cleanup_commands:
        with open(output_cleanup_path, 'a') as f:
          for cmd in output.cleanup_commands:
            f.write('# {}\n'.format(cmd))
    except Exception as e:  # pylint: disable=broad-except
      logging.error('%s: setup failed on step %s: %s', project_id, step_num, e)
      logging.error(
          'Failure information has been written to --output_yaml_path. '
          'Please ensure the config at --project_yaml is updated with any '
          'changes from the config at --output_yaml_path and re-run the script'
          '(Note: only applicable if --output_yaml_path != --project_yaml)')

      # only record failed step if project was undeployed, an update can always
      # start from the beginning
      if not deployed:
        field_generating.create_and_get_generated_fields_ref(
            project_id, config.root)['failed_step'] = step_num
        utils.write_yaml_file(config.root, output_yaml_path)

      return False

    utils.write_yaml_file(config.root, output_yaml_path)

  # if this deployment was resuming from a previous failure, remove the
  # failed step as it is done
  if field_generating.is_generated_fields_exist(project_id, config.root):
    field_generating.get_generated_fields_ref(project_id, config.root).pop(
        'failed_step', None)
  utils.write_yaml_file(config.root, output_yaml_path)
  logging.info('Setup completed successfully.')

  return True


def install_forseti(config):
  """Install forseti based on the given config."""
  forseti_config = config.root['forseti']
  # forseti.install(forseti_config)
  forseti_project_id = forseti_config['project']['project_id']
  generated_field = field_generating.create_and_get_generated_fields_ref(
      forseti_project_id, config.root)
  generated_field['service_account'] = forseti.get_server_service_account(
      forseti_project_id)
  generated_field['server_bucket'] = forseti.get_server_bucket(
      forseti_project_id)


def get_forseti_access_granter_step(project_id):
  """Get step to grant access to the forseti instance for the project."""

  def grant_access(config):
    logging.warning(config.root)
    forseti.grant_access(
        project_id,
        field_generating.get_generated_fields_ref(
            field_generating.get_forseti_project_id(config.root),
            config.root)['service_account'])

  return Step(
      func=grant_access,
      description='Grant Access to Forseti Service account',
      updatable=False,
  )


def validate_project_configs(overall, projects):
  """Check if the configurations of projects are valid.

  Args:
    overall (dict): The overall configuration of all projects.
    projects (list): A list of dictionaries of projects.
  """
  if 'allowed_apis' not in overall:
    return

  allowed_apis = set(overall['allowed_apis'])
  missing_allowed_apis = collections.defaultdict(list)
  for project in projects:
    for api in project.project.get('enabled_apis', []):
      if api not in allowed_apis:
        missing_allowed_apis[api].append(project.project['project_id'])
  if missing_allowed_apis:
    raise utils.InvalidConfigError(
        ('Projects try to enable the following APIs '
         'that are not in the allowed_apis list:\n%s' % missing_allowed_apis))


def main(argv):
  del argv  # Unused.

  if FLAGS.enable_new_style_resources:
    logging.info('--enable_new_style_resources is true.')

  FLAGS.project_yaml = utils.normalize_path(FLAGS.project_yaml)
  field_generating.move_generated_fields_out_of_projects(FLAGS.project_yaml)

  FLAGS.output_yaml_path = utils.normalize_path(FLAGS.output_yaml_path)
  FLAGS.output_cleanup_path = utils.normalize_path(FLAGS.output_cleanup_path)
  if FLAGS.output_rules_path:
    FLAGS.output_rules_path = utils.normalize_path(FLAGS.output_rules_path)

  # Read and parse the project configuration YAML file.
  root_config = utils.load_config(FLAGS.project_yaml)
  if not root_config:
    logging.error('Error loading project YAML.')
    return

  logging.info('Validating project YAML against schema.')
  try:
    utils.validate_config_yaml(root_config)
  except jsonschema.exceptions.ValidationError as e:
    logging.error('Error in YAML config: %s', e)
    return

  with open(FLAGS.output_cleanup_path, 'w') as f:
    f.write(_CLEANUP_HEADER)

  want_projects = set(FLAGS.projects)

  def want_project(project_config_dict):
    if not project_config_dict:
      return False

    return want_projects == {
        '*'
    } or project_config_dict['project_id'] in want_projects

  projects = []
  audit_logs_project = root_config.get('audit_logs_project')

  # Always deploy the remote audit logs project first (if present).
  if want_project(audit_logs_project):
    projects.append(
        ProjectConfig(
            root=root_config,
            project=audit_logs_project,
            audit_logs_project=None,
            extra_steps=[]))

  forseti_config = root_config.get('forseti')

  if forseti_config and want_project(forseti_config['project']):
    extra_steps = [
        Step(
            func=install_forseti,
            description='Install Forseti',
            updatable=False,
        ),
        get_forseti_access_granter_step(
            forseti_config['project']['project_id']),
    ]

    if audit_logs_project:
      extra_steps.append(
          get_forseti_access_granter_step(audit_logs_project['project_id']))

    forseti_project_config = ProjectConfig(
        root=root_config,
        project=forseti_config['project'],
        audit_logs_project=audit_logs_project,
        extra_steps=extra_steps)
    projects.append(forseti_project_config)

  for project_config in root_config.get('projects', []):
    if not want_project(project_config):
      continue

    extra_steps = []
    if forseti_config:
      extra_steps.append(
          get_forseti_access_granter_step(project_config['project_id']))

    projects.append(
        ProjectConfig(
            root=root_config,
            project=project_config,
            audit_logs_project=audit_logs_project,
            extra_steps=extra_steps))

  validate_project_configs(root_config['overall'], projects)

  logging.info('Found %d projects to deploy', len(projects))

  for config in projects:
    logging.info('Setting up project %s', config.project['project_id'])

    if not setup_project(config, FLAGS.output_yaml_path,
                         FLAGS.output_cleanup_path):
      # Don't attempt to deploy additional projects if one project failed.
      return

  if forseti_config:
    rule_generator.run(root_config, output_path=FLAGS.output_rules_path)

  logging.info(
      'All projects successfully deployed. Please remember to sync '
      'any changes written to the config at --output_yaml_path with '
      '--project_yaml before running the script again (Note: only applicable '
      'if --output_yaml_path != --project_yaml)')


if __name__ == '__main__':
  flags.mark_flag_as_required('project_yaml')
  flags.mark_flag_as_required('output_yaml_path')
  flags.mark_flag_as_required('output_cleanup_path')
  app.run(main)
