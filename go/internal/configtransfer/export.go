// Package configtransfer defines the configuration download options and summary.
package configtransfer

import "time"

// Options matches config export's explicit opt-ins. Now and Version make the
// exported metadata reproducible; zero values use the clock and parity version.
type Options struct {
	IncludeProjects bool
	IncludeSecrets  bool
	Now             time.Time
	Version         string
}

// Summary counts exported entries, secret values, path references and bytes.
type Summary struct {
	Counts  map[string]int
	Secrets int
	Repoint int
	Bytes   int64
}
