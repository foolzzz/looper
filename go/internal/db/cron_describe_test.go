package db

import (
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"testing"
)

func TestDescribeCron(t *testing.T) {
	tests := []struct {
		rule string
		zone string
		want string
	}{
		{rule: "0 9 * * *", zone: "UTC", want: "Daily at 09:00 (UTC)"},
		{rule: "0 9 * * 1-5", zone: "UTC", want: "Weekdays at 09:00 (UTC)"},
		{rule: "0 9 * * 0,6", zone: "UTC", want: "Weekends at 09:00 (UTC)"},
		{rule: "*/15 * * * *", zone: "UTC", want: "Every 15 minutes"},
		{rule: "* * * * *", zone: "UTC", want: "Every minute"},
		{rule: "5 */2 * * *", zone: "UTC", want: "Every 2 hours at :05"},
		{rule: "0 0 31 2 *", zone: "UTC", want: "0 0 31 2 * (UTC)"},
		{rule: "not a rule", zone: "UTC", want: "not a rule (UTC)"},
		{rule: "0 9 * * 1,3", zone: "America/Los_Angeles", want: "Mondays and Wednesdays at 09:00 (America/Los_Angeles)"},
		// The step is MaxInt64. Adding it to minute 1 wraps to a negative
		// minute unless the addition is guarded. Python's range keeps 1.
		{rule: "1/9223372036854775807 9 * * *", zone: "UTC", want: "Daily at 09:01 (UTC)"},
	}
	for _, tt := range tests {
		t.Run(tt.rule, func(t *testing.T) {
			if got := describeCron(tt.rule, tt.zone); got != tt.want {
				t.Fatalf("describe %q = %q, want %q", tt.rule, got, tt.want)
			}
		})
	}
}

func TestLocalZoneNameMatchesPython(t *testing.T) {
	root := repoRoot(t)
	bin := pythonBin(t, root)
	cmd := exec.Command(bin, "-c", "from looper.agents.cron import local_zone_name; print(local_zone_name(), end='')")
	cmd.Dir = root
	cmd.Env = append(os.Environ(), "PYTHONPATH="+root)
	out, err := cmd.Output()
	if err != nil {
		t.Fatalf("python local_zone_name: %v", err)
	}
	if got := localZoneName(); got != string(out) {
		t.Fatalf("local zone %q, python %q", got, out)
	}
}

func repoRoot(t *testing.T) string {
	t.Helper()
	dir, err := os.Getwd()
	if err != nil {
		t.Fatal(err)
	}
	for {
		if _, err := os.Stat(filepath.Join(dir, "looper", "agents", "cron.py")); err == nil {
			return dir
		}
		parent := filepath.Dir(dir)
		if parent == dir {
			t.Fatal("looper/agents/cron.py not found")
		}
		dir = parent
	}
}

func pythonBin(t *testing.T, root string) string {
	t.Helper()
	candidates := []string{
		filepath.Join(root, ".venv", "bin", "python"),
		"python3.11",
		"python3",
	}
	for _, bin := range candidates {
		path := bin
		if !strings.Contains(bin, "/") {
			found, err := exec.LookPath(bin)
			if err != nil {
				continue
			}
			path = found
		} else if _, err := os.Stat(bin); err != nil {
			continue
		}
		cmd := exec.Command(path, "-c", "import looper.agents.cron")
		cmd.Dir = root
		cmd.Env = append(os.Environ(), "PYTHONPATH="+root)
		if cmd.Run() == nil {
			return path
		}
	}
	t.Fatal("python with looper.agents.cron is required")
	return ""
}
