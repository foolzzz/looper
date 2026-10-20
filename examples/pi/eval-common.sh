#!/usr/bin/env bash
# Shared mechanical setup for the two Pi eval entry points. Source only.

set -euo pipefail

ROOT=$(cd "$(dirname "$0")/../.." && pwd)
cd "$ROOT"

STATE="$ROOT/.local/pi-eval-$EVAL_KIND"
GAPS="$STATE/gaps.txt"
SPEC="$STATE/agent.json"
if [[ "$EVAL_KIND" == deepseek ]]; then
  EXAMPLE="$ROOT/evals/agents/pi-deepseek.example.json"
  KEY_ENV=DEEPSEEK_API_KEY
  BASE_URL=https://api.deepseek.com
else
  EXAMPLE="$ROOT/evals/agents/pi-cpa.example.json"
  KEY_ENV=PI_EVAL_API_KEY
  BASE_URL="${PI_EVAL_BASE_URL:-}"
fi
mkdir -p "$STATE"

TASKS="single_step,dependent_steps,reviewed_step,room_interface,room_consult,operator_notes,planner_plan_file,vault_closeout_keeps,vault_closeout_nothing"
DIALOGS="status,confirm_create_start,config_tools,cancel_confirmation"

write_gaps_header() {
  cat > "$GAPS" <<'EOF'
unverified: steering has no task id in tasks.py
unverified: worker session resume (--session) has no task id; room_consult is the consult-copy case
unverified: worker cancel tree has no task id; cancel_confirmation is the Manager confirmation case
unverified: responses strict and optional tool parameters are absent from runner JSON
unverified: operator_notes_stale and schedule_* dialogs were not selected
pricing: catalog cost 0 is not a free run; Looper provider_price is the cost basis
EOF
  if [[ "$EVAL_KIND" == deepseek ]]; then
    cat >> "$GAPS" <<'EOF'
unverified: provider/API compatibility vs model decision failure requires actual request/error and tool-result readback; runner outcome alone is insufficient
unverified: platform tool success/refusal, schema optional/strict and read_only enforcement need raw trace evidence
unverified: four roles, room/vault, session continuity, steering and cancel tree must be checked separately; fake uv is not protocol acceptance
unverified: CPA comparison requires its independent results; never reuse its sessions or model statistics
usage: input is ordinary input excluding cache_read; compare input/cache_read/output and provider_price/unpriced separately, never Pi catalog zero prices
EOF
  fi
}

preflight_fail() {
  rm -f "$SPEC"
  write_gaps_header
  printf 'real readback: unverified\n' >> "$GAPS"
  printf 'preflight: %s\n' "$1" >> "$GAPS"
  printf 'error: %s\n' "$1" >&2
  exit 2
}

require_text() {
  local name="$1"
  if [[ -z "${!name+x}" ]]; then
    preflight_fail "$name is required"
  fi
  local value="${!name}"
  if [[ -z "${value//[[:space:]]/}" ]]; then
    preflight_fail "$name is required"
  fi
}

require_text "$KEY_ENV"
if [[ "$EVAL_KIND" == responses ]]; then
  require_text PI_EVAL_BASE_URL
fi
require_text PI_EVAL_TOTAL_CAP_USD
require_text PI_EVAL_CASE_BUDGET_USD

pg_url=""
if [[ -n "${LOOPER_EVAL_DATABASE_URL+x}" ]] && [[ -n "${LOOPER_EVAL_DATABASE_URL//[[:space:]]/}" ]]; then
  pg_url=$LOOPER_EVAL_DATABASE_URL
elif [[ -n "${LOOPER_TEST_DATABASE_URL+x}" ]] && [[ -n "${LOOPER_TEST_DATABASE_URL//[[:space:]]/}" ]]; then
  pg_url=$LOOPER_TEST_DATABASE_URL
else
  preflight_fail "a local disposable database URL is required"
fi

deadline="${PI_EVAL_DEADLINE_SECONDS:-1500}"
if ! message=$(python3 - "$BASE_URL" "$PI_EVAL_TOTAL_CAP_USD" "$PI_EVAL_CASE_BUDGET_USD" "$pg_url" "$deadline" <<'PY'
import math
import sys
from urllib.parse import unquote, urlsplit

base, total_text, case_text, pg_url, deadline_text = sys.argv[1:]

def die(text: str) -> None:
    print(text)
    raise SystemExit(2)

def money(text: str, label: str) -> float:
    try:
        value = float(text)
    except ValueError:
        die(f"{label} must be a finite positive number")
    if not math.isfinite(value) or value <= 0:
        die(f"{label} must be a finite positive number")
    return value

total = money(total_text, "PI_EVAL_TOTAL_CAP_USD")
case = money(case_text, "PI_EVAL_CASE_BUDGET_USD")
if case > total:
    die("PI_EVAL_CASE_BUDGET_USD exceeds PI_EVAL_TOTAL_CAP_USD")

endpoint = urlsplit(base)
if endpoint.scheme not in ("http", "https") or not endpoint.hostname:
    die("PI_EVAL_BASE_URL must be an http or https URL")

database = urlsplit(pg_url)
name = unquote(database.path.lstrip("/"))
if (database.scheme not in ("postgres", "postgresql") or database.hostname not in ("127.0.0.1", "localhost")
        or not name or name == "loopx"):
    die("database URL must name a local disposable database other than loopx")

if not deadline_text.isdigit() or not 1 <= int(deadline_text) <= 1500:
    die("PI_EVAL_DEADLINE_SECONDS must be a whole number from 1 to 1500")
PY
); then
  preflight_fail "${message:-invalid eval configuration}"
fi

write_gaps_header
python3 - "$EXAMPLE" "$SPEC" "$BASE_URL" "$KEY_ENV" <<'PY'
import json
import os
import sys

source, dest, base, key_env = sys.argv[1:]
key = os.environ[key_env]
spec = json.loads(open(source, encoding="utf-8").read())
spec["provider"]["base_url"] = base
spec["provider"]["api_key_env"] = key_env
text = json.dumps(spec, indent=2) + "\n"
if key in text:
    raise SystemExit("refusing to write the API key into the spec")
open(dest, "w", encoding="utf-8").write(text)
PY

# A DeepSeek invocation cannot consume results or implicit baselines from CPA
# or a previous invocation. The runner creates fresh databases and runtime roots.
RUNS_DIR=evals/runs
if [[ "$EVAL_KIND" == deepseek ]]; then
  RUNS_DIR=$(mktemp -d "$STATE/run.XXXXXX")
fi

python3 - "$RUNS_DIR" "$STATE/before.txt" <<'PY'
import sys
from pathlib import Path

directory, dest = Path(sys.argv[1]), Path(sys.argv[2])
names = sorted(str(path.resolve()) for path in directory.glob("*.json")) if directory.is_dir() else []
dest.write_text(("\n".join(names) + "\n") if names else "", encoding="utf-8")
PY

unset LOOPER_TELEGRAM_BOT_TOKEN || true

DEADLINE=$deadline
START_EPOCH=$(date +%s)

run_bounded() {
  local label=$1
  shift
  local now elapsed remain
  now=$(date +%s)
  elapsed=$((now - START_EPOCH))
  remain=$((DEADLINE - elapsed))
  if (( remain < 1 )); then
    printf 'gap: deadline: %s not started\n' "$label" >> "$GAPS"
    return 124
  fi
  python3 - "$remain" "$@" <<'PY'
import os
import signal
import subprocess
import sys

remaining = float(sys.argv[1])
command = sys.argv[2:]
process = subprocess.Popen(command, start_new_session=True)
try:
    code = process.wait(timeout=remaining)
except subprocess.TimeoutExpired:
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    try:
        process.wait(timeout=2)
    except subprocess.TimeoutExpired:
        pass
    # The leader may exit on SIGTERM while its children keep running.
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    process.wait()
    raise SystemExit(124)
raise SystemExit(code if code is not None else 1)
PY
}

common=(
  --mode real
  --register-agent "$SPEC"
  --reps 2
  --jobs 3
  --total-cap-usd "$PI_EVAL_TOTAL_CAP_USD"
  --case-budget-usd "$PI_EVAL_CASE_BUDGET_USD"
  --max-budget-usd "$PI_EVAL_CASE_BUDGET_USD"
  --pg-url "$pg_url"
  --out-dir "$RUNS_DIR"
)

status=0
run_bounded tasks uv run --extra test python -m evals.prompt_regression run \
  "${common[@]}" --tasks "$TASKS" || status=$?
if [[ "$status" -eq 124 ]]; then
  printf 'gap: deadline: stopped before the runner finished\n' >> "$GAPS"
else
  run_dialogs=true
  if ! remaining=$(python3 - "$RUNS_DIR" "$PI_EVAL_TOTAL_CAP_USD" "$SPEC" "$STATE/before.txt" <<'PY_BUDGET'
import json
import math
import sys
from decimal import Decimal
from pathlib import Path

before = set(Path(sys.argv[4]).read_text(encoding="utf-8").splitlines())
paths = [path for path in Path(sys.argv[1]).glob("*.json") if str(path.resolve()) not in before]
try:
    if len(paths) != 1:
        raise ValueError("missing task spend readback")
    data = json.loads(paths[0].read_text(encoding="utf-8"))
    spec = json.loads(Path(sys.argv[3]).read_text(encoding="utf-8"))
    if data.get("agent") != spec["agent"]["name"]:
        raise ValueError("unexpected agent in task spend readback")
    rows = data.get("tasks") or []
    if not rows or data.get("aborted"):
        raise ValueError("incomplete task spend readback")
    for row in rows:
        if row.get("skipped"):
            continue
        basis = row.get("cost_basis") or {}
        if basis.get("unpriced") or not basis.get("provider_price"):
            raise ValueError("pricing unverified")
        cost = row.get("cost_usd")
        if cost is None or not math.isfinite(float(cost)) or float(cost) < 0:
            raise ValueError("pricing unverified")
    cost = Decimal(str(data["total_cost_usd"]))
    row_cost = sum((Decimal(str(row.get("cost_usd") or 0)) for row in rows), Decimal(0))
    if not cost.is_finite() or cost < 0 or abs(cost - row_cost) > Decimal("0.000001"):
        raise ValueError("inconsistent task spend readback")
    remaining = Decimal(sys.argv[2]) - cost
    if remaining <= 0:
        raise ValueError("total budget exhausted")
    print(remaining)
except (KeyError, ValueError, TypeError, ArithmeticError):
    print("task spend missing, unknown, inconsistent or total budget exhausted")
    raise SystemExit(2)
PY_BUDGET
  ); then
    printf 'dialogs unverified: %s\n' "$remaining" >> "$GAPS"
    status=2
    run_dialogs=false
  fi
  # The task runner already used the cap; dialogs receive only its remainder.
  for ((i=0; i<${#common[@]}; i++)); do
    if [[ "$run_dialogs" == true && "${common[$i]}" == --total-cap-usd ]]; then
      common[$((i + 1))]=$remaining
    fi
  done
  if [[ "$run_dialogs" == true ]]; then
    dialog_status=0
    run_bounded dialogs uv run --extra test python -m evals.prompt_regression dialogs \
      "${common[@]}" --dialogs "$DIALOGS" || dialog_status=$?
    if [[ "$dialog_status" -ne 0 ]]; then
      status=$dialog_status
    fi
    if [[ "$dialog_status" -eq 124 ]]; then
      printf 'gap: deadline: stopped before the runner finished\n' >> "$GAPS"
    fi
  fi
fi

python3 - "$GAPS" "$STATE/before.txt" "$RUNS_DIR" <<'PY'
import json
import sys
from pathlib import Path

gaps_path, before_path, runs_dir = (Path(arg) for arg in sys.argv[1:])
before = set(before_path.read_text(encoding="utf-8").splitlines()) if before_path.is_file() else set()
current = sorted(path.resolve() for path in runs_dir.glob("*.json")) if runs_dir.is_dir() else []
new_paths = [path for path in current if str(path) not in before]
lines = []
if not new_paths:
    lines.append("real readback: unverified")
for path in new_paths:
    data = json.loads(path.read_text(encoding="utf-8"))
    agent = data.get("agent")
    if agent:
        lines.append(f"readback agent: {agent}")
    rows = data["dialogs"] if "dialogs" in data else data.get("tasks") or []
    for row in rows:
        if row.get("skipped"):
            outcome = "skipped"
        elif "outcome" in row:
            outcome = str(row["outcome"])
        elif row.get("stopped_by_cap") or row.get("timed_out"):
            outcome = "error"
        else:
            outcome = "pass" if row.get("success") else "fail"
        basis = row.get("cost_basis") or {}
        priced = int(basis.get("provider_price") or 0)
        unpriced = int(basis.get("unpriced") or 0)
        tokens = row.get("tokens") or {}
        usage_missing = not any(int(tokens.get(name) or 0) for name in
                                ("input", "output", "cache_read", "cache_creation"))
        cost = float(row.get("cost_usd") or 0)
        lines.append(
            f"row {row.get('id')} rep {row.get('rep')} outcome {outcome} "
            f"cost_usd {row.get('cost_usd')} provider_price {priced} unpriced {unpriced}"
        )
        lines.append(
            f"usage input {tokens.get('input')} cache_read {tokens.get('cache_read')} "
            f"output {tokens.get('output')} cache_creation {tokens.get('cache_creation')}"
        )
        if row.get("error_class") or row.get("run_errors"):
            # Do not infer a provider incompatibility from a failed task score.
            lines.append(f"failure cause unverified: error_class {row.get('error_class')} "
                         f"run_errors {row.get('run_errors')}")
        if unpriced or priced <= 0 or usage_missing or (cost == 0 and priced <= 0):
            lines.append(f"pricing unverified: {row.get('id')} rep {row.get('rep')}")
with gaps_path.open("a", encoding="utf-8") as handle:
    for line in lines:
        handle.write(line + "\n")
PY

if [[ "$status" -ne 0 ]]; then
  printf 'real readback: unverified (runner failed or timed out); protocol acceptance is not established\n' >> "$GAPS"
fi
exit "$status"
