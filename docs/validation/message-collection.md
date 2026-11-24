# User-message collection validation

Collection remains opt-in. Existing sources keep `off` or `bridge`; configuring
a profile, migrating the database or deploying this change does not enable it.
Live account access and a real cutover are outside this validation.

## Results

| Check | Result |
|---|---|
| Targeted Python coverage, 37 files, Python 3.11 and two workers, disposable PostgreSQL enabled | 1,086 distinct cases passed across final runs |
| Final source-alert/Attention and mandatory API-document/action-level rerun | 463 passed |
| Fresh/upgrade/online migration, pinned provenance, existing API and isolated serve rerun | 566 passed |
| Four affected Vitest files | 56 passed |
| Frontend build, scoped Ruff and diff checks | Passed |
| Product-state and empty/error browser screenshots | 18 checked |

There are no outstanding failing checks in this scope. Live checks listed below
remain untested. The targeted set includes the existing bridge, schema registry,
import/delete, subscription/extraction/backfill goldens, catch-up and Attention
regressions; the full repository suite was not run locally.

## Recorded compatibility

`tests/fixtures/message_collection_off_bridge.json` is pinned to
`39f329ac444f0f6d9783011b258626dc3679095d`. Its 89 individual surface entries
cover each role's composed prompt, contract, launch arguments and process
signature, each existing tool definition, the old message-stream operation
schemas/descriptions, and bot post/reply arguments. Another 58 entries record
the no-source, off and bridge database/output scenarios: configuration, status,
runs, import text/per-file/exit results, backfill refusal, why, canonical rows,
offsets and empty/catch-up Attention output. Only fixture paths, file identity
and database-generated timestamps are normalized. No module digest or moving
revision is used. The provenance test regenerates every item from that exact
revision in an archived checkout and disposable PostgreSQL schemas.

Normal off/bridge ticks also assert no user CLI call, collect task or new
collection/parity/alert row. Existing bridge ingestion and retention still run.
The existing subscription, extraction and backfill model-call goldens exercise
local protocol-speaking executables; they do not call a model.

## Local product path

The service tests launch the public `uv run ... looper serve` command with an
explicit private ops file, disposable database, temporary HOME/runtime root,
non-operator port and a PATH containing only local stand-ins and base system
commands. Every HTTP client checks the ops file's port before sending. The
stand-ins refuse commands outside their wire contract and never proxy them.

Assertions cover:

- Actual SourceService bridge ingestion, stopped-reference attestation,
  generation and 24-hour cutover, duplicate collection, timestamp ties and
  thread history, followed by one new canonical event.
- Independent nonempty matched evidence, then one missing reference ID producing
  drift. SQL, read-only CLI exit 1, Attention and the bot receipt identify the
  same frozen session and differences. Repair re-reads that same window.
- Missing auth stops message reads, opens one episode across repeated runs and
  sends one bot notice. Recovery produces a separate receipt and repairs the
  original drift. User-source observations do not settle bot SQLite debt.
- One-setting bridge rollback, service restart and resumed old offsets without
  any further user CLI reads; admitted bot ingress and unmentioned group traffic
  retain their own admission and overlapping catch-up deduplication.
- Two isolated serve processes sharing a database: the second writer receives
  conflict while the first is held in a local auth call; shutdown interrupts
  and reaps that call instead of completing the run.

Unit and database tests separately exercise pagination corruption, millisecond
bounds, serial retry/deadline budgets, transaction failure, lost lock sessions,
six-failure debt, membership rediscovery, sweep rotation, own-app exclusion,
retention/deletion, consumer SQL, identity conflicts, report keyset pagination,
deletion rollback, all CLI exit classes, operator access, durable notice claims,
unknown receipts, normal delivery policy and morning-digest consumption.
Fresh/upgrade/repair/online schema checks preserve the baseline marker and
other additive proactive sources; incompatible columns, defaults, foreign keys,
checks and indexes are rejected.

## Browser checks

Four states (ordinary bridge, drift, missing auth, repaired matched) were checked
at 1280×900, 768×1024, 390×844 and 844×390. All 16 checks had no horizontal
overflow and keyboard focus remained reachable. The bridge page made zero
collection API requests. Two additional 390-pixel screenshots checked empty
and failed collection responses using a replacement for that API response only.
Screenshots contain synthetic data; the temporary archive-directory row was
explicitly hidden. No credentials, ops paths or real account identifiers appear.

## Limits and pending live checks

Offline help for `@larksuite/cli` 1.0.100 established the read-only argv.
Authentication envelopes, expiry semantics, scope and live API limits remain
unverified; the tests establish the frozen local wire contract. Telegram alert
targets use the existing operator identity and receipt contract; no Telegram
Bot API service smoke was run. No real account, external collector, deployment
or production mode change was used.

No model-visible content changed and no paid evaluation was run. If separately
authorized, a compatibility check can use:

```sh
uv run --system-certs --python 3.11 --extra test python -m evals.processor \
  --mode real --register-agent evals/agents/claude-login-sonnet.example.json \
  --cases quiet,content --reps 2 --budget-usd 0.10 \
  --pg-url "$LOOPER_EVAL_DATABASE_URL"
```

This is pending, with four synthetic runs and a maximum configured budget of
$0.40. It does not authorize live channel/account access. The operator must
separately choose when to stop external polling, validate the real CLI/account
and explicitly attest the cutover. Normal-urgency source notices respect quiet
hours and caps; that conservative live policy remains for the maintainer to
confirm. P4 export, P7 agent tools, automatic repair and message-body parity
are outside this change.
