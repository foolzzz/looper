// Package config parses the looper command line.
package config

import (
	"errors"
	"flag"
	"fmt"
	"strconv"
	"strings"
)

// ServeConfig is the parsed `looper serve` command.
type ServeConfig struct {
	Port   int
	DBURL  string
	WebDir string
}

// HelpError asks the process to print usage and exit 0.
type HelpError struct {
	Text string
}

func (e *HelpError) Error() string { return e.Text }

func (e *HelpError) Unwrap() error { return flag.ErrHelp }

// UsageError is a bad command line. The process exits 2.
type UsageError struct {
	Err error
}

func (e *UsageError) Error() string { return e.Err.Error() }

func (e *UsageError) Unwrap() error { return e.Err }

func usage(err error) error {
	if err == nil {
		return nil
	}
	return &UsageError{Err: err}
}

// ParseServeArgs parses `looper serve` arguments, without the serve word.
// --port and --db-url are required. --web-dir is optional.
func ParseServeArgs(args []string) (ServeConfig, error) {
	fs := flag.NewFlagSet("serve", flag.ContinueOnError)
	var buf strings.Builder
	fs.SetOutput(&buf)
	portStr := fs.String("port", "", "TCP port to listen on")
	dbURL := fs.String("db-url", "", "PostgreSQL URL")
	webDir := fs.String("web-dir", "", "dashboard directory; unknown paths fall back to index.html")
	fs.Usage = func() {
		fmt.Fprintf(&buf, "usage: looper serve --port N --db-url URL [--web-dir DIR]\n")
		fs.PrintDefaults()
	}
	if err := fs.Parse(args); err != nil {
		msg := strings.TrimSpace(buf.String())
		if errors.Is(err, flag.ErrHelp) {
			if msg == "" {
				msg = "usage: looper serve --port N --db-url URL [--web-dir DIR]"
			}
			return ServeConfig{}, &HelpError{Text: msg}
		}
		if msg == "" {
			msg = err.Error()
		}
		return ServeConfig{}, usage(errors.New(msg))
	}
	if fs.NArg() != 0 {
		return ServeConfig{}, usage(fmt.Errorf("unexpected argument %q", fs.Arg(0)))
	}
	if *portStr == "" {
		return ServeConfig{}, usage(errors.New("--port is required"))
	}
	port, err := strconv.Atoi(*portStr)
	if err != nil || port < 1 || port > 65535 {
		return ServeConfig{}, usage(fmt.Errorf("--port %q is not a TCP port", *portStr))
	}
	url := strings.TrimSpace(*dbURL)
	if url == "" {
		return ServeConfig{}, usage(errors.New("--db-url is required"))
	}
	return ServeConfig{
		Port:   port,
		DBURL:  url,
		WebDir: strings.TrimSpace(*webDir),
	}, nil
}
