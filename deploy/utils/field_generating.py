"""TODO: DO NOT SUBMIT without one-line documentation for field_generating.

TODO: DO NOT SUBMIT without a detailed description of
field_generating.
"""

from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

from deploy.utils import utils

# Name of field where generated fields will be added.
_GENERATED_FIELDS_NAME = 'generated_fields'


def is_generated_fields_exist(project_id, input_config):
  return project_id in input_config.get(_GENERATED_FIELDS_NAME, {})


def get_generated_fields_ref(project_id, input_config):
  return input_config[_GENERATED_FIELDS_NAME][project_id]


def get_generated_fields_copy(project_id, input_config):
  if is_generated_fields_exist(project_id, input_config):
    return get_generated_fields_ref(project_id, input_config).copy()
  else:
    return {}


def create_and_get_generated_fields_ref(project_id, input_config):
  if _GENERATED_FIELDS_NAME not in input_config:
    input_config[_GENERATED_FIELDS_NAME] = {}
  if project_id not in input_config[_GENERATED_FIELDS_NAME]:
    input_config[_GENERATED_FIELDS_NAME][project_id] = {}
  return get_generated_fields_ref(project_id, input_config)


def is_deployed(project_id, input_config):
  """Determine whether the project has been deployed."""
  generated_fields = get_generated_fields_copy(project_id, input_config)
  return generated_fields and 'failed_step' not in generated_fields


def move_generated_fields_out_of_projects(input_yaml_path):
  """Move generated_fields from each project outside."""
  overall = utils.load_config(input_yaml_path)
  generated_fields = {}
  projects = overall.get('projects', [])
  for proj in projects:
    if _GENERATED_FIELDS_NAME in proj:
      generated_fields[proj['project_id']] = proj.pop(_GENERATED_FIELDS_NAME)

  audit_logs_project = overall.get('audit_logs_project', {})
  if _GENERATED_FIELDS_NAME in audit_logs_project:
    generated_fields[audit_logs_project['project_id']] = audit_logs_project.pop(
        _GENERATED_FIELDS_NAME)

  forseti_project = overall.get('audit_logs_project', {}).get('project', {})
  if _GENERATED_FIELDS_NAME in forseti_project:
    generated_fields[audit_logs_project['project_id']] = forseti_project.pop(
        _GENERATED_FIELDS_NAME)

  if generated_fields:
    if _GENERATED_FIELDS_NAME in overall:
      raise utils.InvalidConfigError(
          ('Generated fields should not appear in both the config file '
           'and the sub-config files:\n%s' % generated_fields))
    else:
      if utils.wait_for_yes_no('Move generated_fields out of projects [y/N]?'):
        overall[_GENERATED_FIELDS_NAME] = generated_fields
        utils.write_yaml_file(overall, input_yaml_path)


def get_forseti_project_id(input_config):
  """Get the project id where forseti settles."""
  return input_config['forseti']['project']['project_id']
