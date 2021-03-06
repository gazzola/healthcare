package cft

import (
	"testing"

	"github.com/ghodss/yaml"
)

func TestDefaultResource(t *testing.T) {
	resYAML := `
properties:
  name: foo-resource
`

	d := new(DefaultResource)
	if err := yaml.Unmarshal([]byte(resYAML), d); err != nil {
		t.Fatalf("yaml unmarshal: %v", err)
	}
	if d.ResourceName != "foo-resource" {
		t.Fatalf("default resource name error: %v", d.ResourceName)
	}
}
