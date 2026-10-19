// Package version is the Go port's release version.
package version

import (
	"fmt"
	"regexp"
	"strings"
)

// semver is the X.Y.Z shape of Version and of a vX.Y.Z-go tag.
var semver = regexp.MustCompile(`^[0-9]+\.[0-9]+\.[0-9]+$`)

// Version is the release version without a leading v, for example 0.1.0.
// A release tag is v<Version>-go. The variable is a var so a release build
// can stamp the same value with
//
//	-ldflags "-X github.com/foolzzz/looper/go/internal/version.Version=<version>"
//
// cmd/looper reports main.version. An ordinary build leaves that as dev, and
// the release workflow sets it to this same version.
var Version = "0.1.0"

// CheckTag reports whether tag is v<Version>-go.
// The version is the tag without the leading v and the trailing -go.
func CheckTag(tag string) error {
	if !semver.MatchString(Version) {
		return fmt.Errorf("release tag: version constant %q is not X.Y.Z", Version)
	}
	const suffix = "-go"
	if !strings.HasPrefix(tag, "v") || !strings.HasSuffix(tag, suffix) {
		return fmt.Errorf("release tag: %q is not vX.Y.Z-go", tag)
	}
	got := strings.TrimSuffix(strings.TrimPrefix(tag, "v"), suffix)
	if !semver.MatchString(got) {
		return fmt.Errorf("release tag: %q version %q is not X.Y.Z", tag, got)
	}
	if got != Version {
		return fmt.Errorf("release tag: %q version %s does not match %s", tag, got, Version)
	}
	return nil
}
