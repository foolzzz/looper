package config

import (
	"errors"
	"flag"
	"strings"
	"testing"
)

func TestParseServeArgs(t *testing.T) {
	tests := []struct {
		name    string
		args    []string
		want    ServeConfig
		wantErr string
		help    bool
	}{
		{
			name: "all flags",
			args: []string{"--port", "8080", "--db-url", "postgresql://postgres:postgres@127.0.0.1:54329/loopgo_x", "--web-dir", "/tmp/web"},
			want: ServeConfig{Port: 8080, DBURL: "postgresql://postgres:postgres@127.0.0.1:54329/loopgo_x", WebDir: "/tmp/web"},
		},
		{
			name: "web dir optional",
			args: []string{"--port", "1", "--db-url", "postgres://localhost/db"},
			want: ServeConfig{Port: 1, DBURL: "postgres://localhost/db"},
		},
		{
			name: "flag order",
			args: []string{"--db-url", "postgres://localhost/db", "--web-dir", "web", "--port", "9"},
			want: ServeConfig{Port: 9, DBURL: "postgres://localhost/db", WebDir: "web"},
		},
		{
			name: "equals form",
			args: []string{"--port=443", "--db-url=postgres://localhost/db"},
			want: ServeConfig{Port: 443, DBURL: "postgres://localhost/db"},
		},
		{
			name: "trims db url and web dir",
			args: []string{"--port", "80", "--db-url", "  postgres://localhost/db  ", "--web-dir", "  web  "},
			want: ServeConfig{Port: 80, DBURL: "postgres://localhost/db", WebDir: "web"},
		},
		{name: "missing port", args: []string{"--db-url", "postgres://localhost/db"}, wantErr: "--port is required"},
		{name: "missing db", args: []string{"--port", "80"}, wantErr: "--db-url is required"},
		{name: "blank db", args: []string{"--port", "80", "--db-url", "   "}, wantErr: "--db-url is required"},
		{name: "port zero", args: []string{"--port", "0", "--db-url", "postgres://localhost/db"}, wantErr: "not a TCP port"},
		{name: "port negative", args: []string{"--port", "-1", "--db-url", "postgres://localhost/db"}, wantErr: "not a TCP port"},
		{name: "port too big", args: []string{"--port", "65536", "--db-url", "postgres://localhost/db"}, wantErr: "not a TCP port"},
		{name: "port not a number", args: []string{"--port", "abc", "--db-url", "postgres://localhost/db"}, wantErr: "not a TCP port"},
		{name: "port empty", args: []string{"--port", "", "--db-url", "postgres://localhost/db"}, wantErr: "--port is required"},
		{name: "unknown flag", args: []string{"--port", "80", "--db-url", "postgres://localhost/db", "--host", "0.0.0.0"}, wantErr: "not defined"},
		{name: "extra arg", args: []string{"--port", "80", "--db-url", "postgres://localhost/db", "extra"}, wantErr: "unexpected argument"},
		{name: "help", args: []string{"--help"}, help: true, wantErr: "--port"},
		{name: "short help", args: []string{"-h"}, help: true, wantErr: "--db-url"},
	}
	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			got, err := ParseServeArgs(tt.args)
			if tt.wantErr != "" || tt.help {
				if err == nil {
					t.Fatalf("got config %+v, want error containing %q", got, tt.wantErr)
				}
				if tt.help && !errors.Is(err, flag.ErrHelp) {
					t.Fatalf("error %v, want help", err)
				}
				if !tt.help {
					var usage *UsageError
					if !errors.As(err, &usage) {
						t.Fatalf("error %T %v, want UsageError", err, err)
					}
				}
				if tt.wantErr != "" && !strings.Contains(err.Error(), tt.wantErr) {
					t.Fatalf("error %q, want substring %q", err.Error(), tt.wantErr)
				}
				return
			}
			if err != nil {
				t.Fatalf("parse: %v", err)
			}
			if got != tt.want {
				t.Fatalf("got %+v, want %+v", got, tt.want)
			}
		})
	}
}
