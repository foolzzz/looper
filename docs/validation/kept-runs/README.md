# Kept-run launchd validation

## Goal

Validate kept-run upgrades and failure recovery on an isolated macOS launchd
service. **The real Claude acceptance gate remains open:** the isolated Claude
and Codex directories were not logged in and no model credentials were supplied.
The runs below use scripted CLI peers; their reported costs are synthetic.

## Changes

- Include startup-ended run counts and reasons in the `kept_runs` P1 check and
  the deployment report's `ended_at_start` field.
- Report Manager and Planner turns interrupted at a `CONTROL_PLANE` deadline
  in `cut_turns`, including `agent_id` and `role`.
- Read systemd's effective `KillMode`, including drop-ins; an incompatible unit
  requires `FULL` with only the reinstall reason.

The dashboard uses its existing task activity timeline and system checks. The CLI
entry points are `looper service upgrade` and `looper service restart`. Telegram
and Lark are not involved. No model-visible content changed; no prompt eval is
needed. Keeper mode remains `off` by default.

## Setup and limits

The worktree started at `60ecbce91` (`origin/main`). A disposable source copy ran
existing predecessor `6767f2978` and upgraded by fast-forward to `60ecbce91`,
without creating a validation commit. The sole changed control-plane source file
in that upgrade was `looper/agents/mcp_server.py`; release metadata, documentation
and tests also differ. This uses a predecessor package to exercise an actual
installation rather than upgrading an already-current package.

The service used its own launchd label, a short temporary runtime root, a fresh
database on the disposable PostgreSQL endpoint, keeper mode `on`, a port other
than 8767, and a git-backed project with the agent room on. Profiles selected
Claude Code/sonnet with caps of $2 per run and $5 per task. The lack of isolated
Codex login also prevents the requested real Codex Manager/Planner profiles;
scripted Claude peers drove those roles for the service-mechanics checks.

The successful package upgrade ran before the report fixes were copied into the
disposable installation. All subsequent restart/failure checks ran with the
fixes. The failure-injection and bridge reports use the real deployment executor
with a no-package installer hook, not another package upgrade.

## Validation

### Passed

| Scenario | Observed result | Evidence |
|---|---|---|
| Scripted upgrade, supplementary to RFC case 7 | CLI plans `CONTROL_PLANE`; `kept_before=1`, `adopted=1`; keeper 61357 and child 61360 retain PID, PGID and start time. Record 5 is completed, event counts are 35/35, step `s1` retains its Worker id. | [Plan](plan.txt), [reports](deploy-reports.json), [process snapshots](processes.txt), [SQL readback](sql.txt) |
| Dashboard continuity | Exactly one “Worker dispatched”, one “carried on across the restart”, one “Worker finished”; asserted against the rendered DOM. The curated timeline extract omits command and transcript rows. | [Timeline extract](timeline.txt) |
| RFC case 8 | The child exits while launchd has no loaded service; startup replays its log. One completed Worker record, one dispatch and one finish; event counts 6/6. | [Outcomes](outcomes.json), [log excerpts](log-excerpts.txt) |
| RFC case 12, turns drain then CLI restart | Keeper and child survive launchd `bootout`/`bootstrap` with unchanged PIDs; startup adopts one of one. | [Process snapshots](processes.txt), [outcomes](outcomes.json), [log excerpts](log-excerpts.txt) |
| RFC case 12, plain CLI restart | The previous keeper and child end; the old record is interrupted and adopted count is zero. The saved task then resumes with a replacement run, which is stopped during cleanup. | [Outcomes](outcomes.json), [log excerpts](log-excerpts.txt) |
| RFC case 13 | Killing one of two keepers fails only that run. The peer retains keeper 71425 and child 71432; Planner dispatches a replacement for the affected step. Final records: one failed, two completed. | [Process snapshots](processes.txt), [outcomes](outcomes.json) |
| RFC case 14 | A second real `looper serve` refuses startup and names the existing lock holder. | [Refusal](second-serve.txt) |
| RFC case 15, connection termination | Terminating only this database's lock backend makes serve exit 75. launchd restarts it (70472 → 75637); startup ends the unclean kept run as `no_detach`, with P1 and reason visible in `system.status`. | [launchd exit status](launchd-lock-loss.txt), [outcomes](outcomes.json), [log excerpts](log-excerpts.txt) |
| Keeper killed during clean restart gap | Deployment remains serving and reports `kept_before=1`, `adopted=0`, `ended_at_start={"keeper_dead":1}` and P1. `system.status` exposes the same reason. | [Reports](deploy-reports.json), [outcomes](outcomes.json) |
| Bridge reconnect on launchd | A real MCP bridge retries through an injected restart gap: 5 attempts, 8.1 seconds, `outcome=success`. The run completes; both room writes occur once. | [Bridge log](bridge-retry.txt), [room counts](bridge-messages.json), [report](deploy-reports.json) |

### Failed, then corrected

- The pre-fix regressions reproduced missing startup-ended warnings/reasons,
  missing cut-turn reporting, the unit-file/effective-KillMode mismatch and the
  misleading keeper-off reason. The four original test files then passed:
  **411 passed**, no skipped cases. Commands, each bounded by `timeout 180`:

  ```text
  uv run --extra test python -m pytest tests/test_service.py tests/test_service_upgrade.py -q
  uv run --extra test python -m pytest tests/test_api_v1_v1_transport_status.py tests/test_service_upgrade_execution.py -q
  ```

  The first pair passed 205 cases in 5.34 seconds; the second passed 206 in
  69.52 seconds. Production diff: 20 added / 11 removed lines. Test diff:
  46 added / 13 removed lines. Additions exceed 2:1 because the four externally
  visible behaviors need both successful and failure/boundary variants; existing
  fixtures and parameterized tests were reused.
- Initial rehearsal setup failures were confined to the disposable harness:
  an empty git repository had no readable HEAD; the isolated install's offline
  package cache was unset; an early downtime attempt interrupted the peer's
  initial room call before its positive control; and a bridge waiter expected
  `outcome=ok` instead of `outcome=success`. The harness reused an existing git
  history, selected the existing package cache, waited for initial output and
  used the bridge's actual success marker before rerunning. The successful
  evidence above comes from the corrected attempts. The package installer
  failure recovered the serving service and its held run; no product change
  was needed for those harness failures.

### Not run

- **RFC case 7 with the real Claude CLI:** isolated `claude auth status` returned
  `loggedIn=false`; isolated `codex login status` returned `Not logged in`.
  The requested sonnet edits and two Bash `sleep 90` calls were not executed
  in this PR.
  The scripted upgrade and timeline extract are supplementary evidence, not a pass
  of this real-model acceptance case. This case is being run separately, and its
  result will be posted on this PR.
- **Linux user systemd:** no Linux host with an available user systemd session
  was supplied. Effective-KillMode/drop-in handling has regression coverage;
  real `systemctl --user` restart survival and the harmless “left-over process”
  message remain unverified.
- **A real PostgreSQL cluster restart:** only the shared disposable endpoint
  was available; no exclusively owned cluster/server executable was available
  to restart. The connection-termination portion of case 15 passed independently.

## Metrics

These are single observations with scripted peers, not performance estimates
for real Claude runs. RSS is in KiB from `ps`. The curated process snapshots
retain only process identity and start time; `ppid=unknown` means it was not
captured, and `sid` retains the captured `SESS` value. RSS observations from the
original snapshots are retained in the table below.

| Observation | Keepers | Keeper RSS | Drain seconds | Gap seconds | Replay |
|---|---:|---|---:|---:|---|
| CLI package upgrade | 1 | PID 61357: 25,792 before / 25,920 after | 0.003 | 9.191 | 4 events, 0 ms |
| Two-run failure isolation | 2 | Before: PID 71424 16,320; PID 71425 15,920. After: peer 71425 15,920; retry 75345 26,048. | — | — | — |
| Keeper dies in gap | 1 before / 0 adopted | See failure-injection report | 0.516 | 2.685 | No adoption |
| Bridge reconnect | 1 | Not sampled separately | 0.004 | 7.786 | 3 events, 0 ms |

Replay time is integer milliseconds; zero means below the reported resolution.
The [log excerpts](log-excerpts.txt) retain detach offsets and replay counters.

## Cleanup

The throwaway launchd service and unit are removed, its disposable database is
dropped, and its runtime root is deleted. The final process scan found zero
processes from the validation runtime or installation; see [cleanup readback](cleanup.json).
No persistent service or shared PostgreSQL cluster was restarted.
