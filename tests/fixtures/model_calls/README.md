# Tool-less call compatibility fixtures

The original outbound, advisor and leads-check fixtures were captured before
`model_call` from main `2b77afb1672cda93be6681259623bafa4e290f2d`.
The subscription, extraction, backfill and spawn fixtures were captured from
unchanged main `1f2ac843e01adac83448c09a329df01a4dbb8566`, before migrating each caller.

These are temporary refactoring evidence. Keep the fixtures and their golden
harnesses through the 2.15 release series; remove them in 2.16 after the caller
migration has shipped. Ordinary domain and backend contract tests remain.

The stand-in is a real CLI subprocess. Comparisons retain argv, raw stdin,
system prompts, working directories, every column the fixtures' database rows
have, events, delivery calls and caller outcomes. A column a later migration
adds to one of these tables is not in the fixtures and is not compared, so an
additive migration needs no new snapshot; a missing fixture column, a changed
value or an extra row still fails. Test-owned paths and clocks
are normalized, and UUIDs are fixed. Ancestor-exclusion paths use a portable
logical directory tree. No prompt or record field is dropped. The one explicit
record difference is the newly assigned `leads_check` run kind; its original
fixture stays unchanged.

The later failure-typing migration adds `failure_class` and `failure_cause` to
record rows even while typing is off. The newer snapshots additionally pin both
columns to NULL; their original prompts, launches, outcomes and other fields stay
unchanged. Validate these additive expectations against unchanged main on the
migrated schema as well as against the migrated callers.

The later schedule-throttling migration adds `active_hours` (NULL) and
`idle_only` (false) to schedule rows. The snapshots pin those additive defaults;
launches, prompts, outcomes and all existing fields stay unchanged.

The platform sub-agent migration (#1554) adds eleven `agent_spawns` columns.
The spawn snapshots pin `attempt` to 1 and the ten nullable columns
(`idempotency_key`, `spawner`, `profile_id`, `delivery`, `delivered_at`,
`cancel_reason`, `failure_class`, `failure_cause`, `child_process`, and
`parent_spend_known`) to NULL, in both the returned outcome and persisted row.
The event-triage migration (#1538) adds `subscription_runs.window_seqs`; the
subscription and extraction snapshots pin it to NULL for cursor windows.
These additions retain every original launch, prompt, outcome and record field.

To regenerate the newer fixtures, use a separate checkout of the recorded
baseline with the golden test modules and `tests/model_call_support.py` copied
in. Set `UPDATE_MODEL_CALL_GOLDENS=1` and run only those test modules against a
disposable PostgreSQL database. The helper verifies the imported package path
and that the production sources match the recorded baseline before writing.
Never regenerate compatibility fixtures from the migrated implementation.
