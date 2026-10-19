package version

import (
	"os"
	"testing"
)

func TestCheckTag(t *testing.T) {
	if !semver.MatchString(Version) {
		t.Fatalf("Version %q is not X.Y.Z", Version)
	}
	other := "9.9.9"
	if other == Version {
		other = "8.8.8"
	}
	tests := []struct {
		name string
		tag  string
		ok   bool
	}{
		{name: "this release", tag: "v" + Version + "-go", ok: true},
		{name: "python tag", tag: "v" + Version, ok: false},
		{name: "no v", tag: Version + "-go", ok: false},
		{name: "other version", tag: "v" + other + "-go", ok: false},
		{name: "empty", tag: "", ok: false},
		{name: "double suffix", tag: "v" + Version + "-go-go", ok: false},
		{name: "uppercase suffix", tag: "v" + Version + "-GO", ok: false},
		{name: "prefix junk", tag: "xv" + Version + "-go", ok: false},
		{name: "pre-release", tag: "v" + Version + "-rc.1-go", ok: false},
	}
	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			err := CheckTag(tt.tag)
			if tt.ok && err != nil {
				t.Fatal(err)
			}
			if !tt.ok && err == nil {
				t.Fatalf("CheckTag(%q) ok, want error", tt.tag)
			}
		})
	}
}

func TestReleaseTagFromEnv(t *testing.T) {
	tag := os.Getenv("LOOPGO_RELEASE_TAG")
	if tag == "" {
		t.Skip("LOOPGO_RELEASE_TAG is not set")
	}
	if err := CheckTag(tag); err != nil {
		t.Fatal(err)
	}
}
