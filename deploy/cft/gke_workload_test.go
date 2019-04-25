package cft

import (
	"os/exec"
	"testing"

	"github.com/google/go-cmp/cmp"
)

func TestGetGCloudCredentials(t *testing.T) {
	region := "foo-center"
	clusterName := "bar-cluster"
	projectID := "foo-project"
	var gotArgs [][]string
	cmdRun = func(cmd *exec.Cmd) error {
		gotArgs = append(gotArgs, cmd.Args)
		return nil
	}
	wantArgs := [][]string{{
		"gcloud", "container", "clusters", "get-credentials", clusterName, "--region", region, "--project", projectID}}
	if err := getGCloudCredentials(clusterName, "--region", region, projectID); err != nil {
		t.Fatalf("getGCloudCredentials error: %v", err)
	}
	if diff := cmp.Diff(gotArgs, wantArgs); len(diff) != 0 {
		t.Fatalf("getGCloudCredentials commands differ: (-got, +want)\n:%v", diff)
	}
}

func TestApplyClusterResource(t *testing.T) {
	containerYamlPath := "foo/bar/abc.yaml"
	var gotArgs [][]string
	cmdRun = func(cmd *exec.Cmd) error {
		gotArgs = append(gotArgs, cmd.Args)
		return nil
	}
	wantArgs := [][]string{{
		"kubectl", "apply", "-f", containerYamlPath}}
	if err := applyClusterWorkload(containerYamlPath); err != nil {
		t.Fatalf("applyClusterWorkload error: %v", err)
	}
	if diff := cmp.Diff(gotArgs, wantArgs); len(diff) != 0 {
		t.Fatalf("applyClusterWorkload commands differ: (-got, +want)\n:%v", diff)
	}
}

func TestGetGKEWorkload(t *testing.T) {
	configExtend := &ConfigData{`
resources:
- gke_workload:
    cluster_name: cluster1
    properties:
      apiVersion: extensions/v1beta1
      kind: Deployment
- gke_workload:
    cluster_name: cluster2
    properties:
      apiVersion: extensions/v1beta1
      kind: Service`,
	}
	_, project := getTestConfigAndProject(t, configExtend)
	workloads, err := getGKEWorkloads(project)
	if err != nil {
		t.Fatalf("getGKEWorkloads: %v", err)
	}
	if len(workloads) != 2 {
		t.Fatalf("workload len error: %v", len(workloads))
	}
	if workloads[0].ClusterName != "cluster1" || workloads[1].ClusterName != "cluster2" {
		t.Fatalf("workload context error: %v", workloads)
	}
}

func TestLocationTypeAndValue(t *testing.T) {
	testcases := []struct {
		in            GKECluster
		locationType  string
		locationValue string
	}{
		{
			in: GKECluster{GKEClusterProperties{
				ResourceName:        "cluster_with_region",
				ClusterLocationType: "Regional",
				Region:              "some_region",
			}},
			locationType:  "--region",
			locationValue: "some_region",
		},
		{
			in: GKECluster{GKEClusterProperties{
				ResourceName:        "cluster_with_zone",
				ClusterLocationType: "Zonal",
				Zone:                "some_zone",
			}},
			locationType:  "--zone",
			locationValue: "some_zone",
		},
	}

	for _, tc := range testcases {
		locationType, locationValue, err := getLocationTypeAndValue(&tc.in)
		if err != nil || locationType != tc.locationType || locationValue != tc.locationValue {
			t.Fatalf("getLocationTypeAndValue error at cluster %q: %q, %q", tc.in.ResourceName, locationType, locationValue)
		}
	}
}

func TestInstallClusterWorkload(t *testing.T) {
	configExtend := &ConfigData{`
resources:
- gke_cluster:
    properties:
      name: cluster1
      clusterLocationType: Regional
      region: somewhere1
      cluster:
        name: cluster1
- gke_workload:
    cluster_name: cluster1
    properties:
      apiVersion: extensions/v1beta1`,
	}

	wantArgs := [][]string{
		{"gcloud", "container", "clusters", "get-credentials", "cluster1-cluster", "--region", "somewhere1", "--project", "my-project"},
		{"kubectl", "apply", "-f"},
	}

	_, project := getTestConfigAndProject(t, configExtend)
	var gotArgs [][]string
	cmdRun = func(cmd *exec.Cmd) error {
		gotArgs = append(gotArgs, cmd.Args)
		return nil
	}
	err := deployGKEWorkloads(project)
	if err != nil {
		t.Fatalf("deployGKEWorkloads error: %v", err)
	}
	if len(gotArgs) != 2 {
		t.Fatalf("deployGKEWorkloads does not run correct number of commands: %d", len(gotArgs))
	}
	if diff := cmp.Diff(gotArgs[0], wantArgs[0]); len(diff) != 0 {
		t.Fatalf("get-credentials cmd error: %v", gotArgs[0])
	}
	if diff := cmp.Diff(gotArgs[1][:3], wantArgs[1]); len(diff) != 0 {
		t.Fatalf("kubectl cmd error: %v", gotArgs[1])
	}
}

func TestLocationTypeAndValueError(t *testing.T) {
	testcases := []struct {
		in  GKECluster
		err string
	}{
		{
			in: GKECluster{GKEClusterProperties{
				ResourceName:        "cluster_zonal_error",
				ClusterLocationType: "Zonal",
				Region:              "some_region",
				Zone:                "",
			}},
			err: "failed to get cluster's zone: cluster_zonal_error",
		},
		{
			in: GKECluster{GKEClusterProperties{
				ResourceName:        "cluster_regional_error",
				ClusterLocationType: "Regional",
				Zone:                "some_zone",
			}},
			err: "failed to get cluster's region: cluster_regional_error",
		},
		{
			in: GKECluster{GKEClusterProperties{
				ResourceName:        "cluster_wrong_type",
				ClusterLocationType: "Location",
				Region:              "some_region",
				Zone:                "some_zone",
			}},
			err: "failed to get cluster's location: cluster_wrong_type",
		},
	}

	for _, tc := range testcases {
		_, _, err := getLocationTypeAndValue(&tc.in)
		if err.Error() != tc.err {
			t.Fatalf("getLocationTypeAndValue error at cluster %q: %v", tc.in.ResourceName, err)
		}
	}
}

func TestInstallClusterWorkloadErrors(t *testing.T) {
	testcases := []struct {
		in  ConfigData
		err string
	}{
		{
			in: ConfigData{`
resources:
- gke_cluster:
    properties:
      name: cluster1
      clusterLocationType: Regional
      region: somewhere1
      cluster:
        name: cluster1
- gke_workload:
    cluster_name: clusterX
    properties:
      apiVersion: extensions/v1beta1`,
			},
			err: "failed to find cluster: \"clusterX\"",
		},
		{
			in: ConfigData{`
resources:
- gke_cluster:
    properties:
      name: cluster1
      clusterLocationType: Location
      region: somewhere1
      cluster:
        name: cluster1
- gke_workload:
    cluster_name: cluster1
    properties:
      apiVersion: extensions/v1beta1`,
			},
			err: "failed to get cluster's location: cluster1",
		},
	}

	for _, tc := range testcases {
		_, project := getTestConfigAndProject(t, &tc.in)
		var gotArgs [][]string
		cmdRun = func(cmd *exec.Cmd) error {
			gotArgs = append(gotArgs, cmd.Args)
			return nil
		}
		err := deployGKEWorkloads(project)
		if err == nil {
			t.Fatalf("TestInstallClusterWorkloadErrors should have error %v", tc.err)
		} else if err.Error() != tc.err {
			t.Fatalf("TestInstallClusterWorkloadErrors wrong error %v; expect: %v", err.Error(), tc.err)
		}
	}
}
