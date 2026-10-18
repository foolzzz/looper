// Command looper is the Go port of the Looper server.
package main

import (
	"errors"
	"flag"
	"fmt"
	"io"
	"log/slog"
	"os"

	"github.com/foolzzz/looper/go/internal/config"
)

// version is the build version. Release builds set it with
// -ldflags "-X main.version=<version>". Package main's symbol is main.version;
// the module path does not match it, so that form leaves this as "dev".
var version = "dev"

func main() {
	os.Exit(run(os.Args[1:], os.Stdout, os.Stderr))
}

func run(args []string, stdout, stderr io.Writer) int {
	logger := slog.New(slog.NewTextHandler(stderr, nil))
	prev := slog.Default()
	slog.SetDefault(logger)
	defer slog.SetDefault(prev)

	if len(args) == 0 {
		fmt.Fprintln(stderr, "usage: looper <version|serve>")
		return 2
	}
	switch args[0] {
	case "version":
		if len(args) > 1 {
			fmt.Fprintf(stderr, "unexpected argument %q\n", args[1])
			return 2
		}
		fmt.Fprintf(stdout, "looper-go %s\n", version)
		return 0
	case "serve":
		if err := serve(args[1:], logger); err != nil {
			if errors.Is(err, flag.ErrHelp) {
				fmt.Fprintln(stdout, err.Error())
				return 0
			}
			var usage *config.UsageError
			if errors.As(err, &usage) {
				fmt.Fprintln(stderr, err.Error())
				return 2
			}
			logger.Error("serve failed", "err", err)
			return 1
		}
		return 0
	case "-h", "--help", "help":
		fmt.Fprintln(stdout, "usage: looper <version|serve>")
		return 0
	default:
		fmt.Fprintf(stderr, "unknown command %q\nusage: looper <version|serve>\n", args[0])
		return 2
	}
}
