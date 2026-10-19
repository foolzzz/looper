# Config export reference fixtures

The repository's Python v2.10.0 implementation generated these files. Every
secret is synthetic. No model, channel or operator runtime is used.

`rows.json` contains the synthetic results of the export reads. The offline
adapter returns these rows through the real Python `looper.db.config_transfer`
functions and schedule reads, then calls the real HTTP handler. The four option
combinations also exercise `write_export`, checking its exact bytes, summary
and owner-only secret-file permissions. The reference's pure legacy effort
conversion checks that the fixture's pre-upgrade system effort `high` yields
the post-upgrade profile `Fixture Agent · high`.

`*.json` bodies and `*.http` responses preserve exact Python bytes. Only time
and version are frozen: `2026-06-15T12:00:00+00:00`, download date `2026-06-15`,
version `2.10.0`. `*.summary.json` contains the four export summaries.

The cases cover default redaction, explicit secret inclusion, optional projects,
text/upload/path knowledge documents, role templates and catalog references,
schedule settings/profile names/task limits, role prompt modes, profile prompt
modes, recommended and edited role prompts, portable sources and omitted local
paths. Unknown, blank, zero, whitespace and invalid boolean query values follow
Python's exact `== "1"` behavior and remain default exports with status 200.
Default config-export GETs contain no `SYNTHETIC_CONFIG_EXPORT_SECRET_DO_NOT_USE` marker.
`channel_secrets` are never exported, even with `secrets=1`.

Regenerate without PostgreSQL from the repository root:

```sh
.venv/bin/python go/testdata/parity/make_config_export_goldens.py --offline
```

For full database validation, create an owned disposable database named
`loopgo_*` on `127.0.0.1:54329`, then run:

```sh
.venv/bin/python go/testdata/parity/make_config_export_goldens.py \
  --db-url postgresql://postgres:postgres@127.0.0.1:54329/loopgo_config_export
```

This **replaces public** in that disposable database, applies the canonical
`looper/db/schema.sql`, loads `fixture.sql` in its old-effort state, then runs
Python `layer_effort.migrate` inside a transaction. It requires every resulting
HTTP body, header and summary to match offline generation byte for byte before
writing output. The database remains available for Go parity and must be dropped
by its creator afterward. Legacy effort migration is performed by Python;
these fixtures do not claim that the Go server implements that migration.

To create a uniquely named database, regenerate, run only the seeded-database
Go export parity test and drop that database even when validation fails, run
from the repository root:

```sh
.venv/bin/python - <<'PY'
import asyncio
import asyncpg
import os
from pathlib import Path
import subprocess
import sys
import uuid

admin = "postgresql://postgres:postgres@127.0.0.1:54329/postgres"
name = "loopgo_g1o_export_" + uuid.uuid4().hex[:12]
dsn = admin.rsplit("/", 1)[0] + "/" + name

async def execute(sql):
    conn = await asyncpg.connect(admin)
    try:
        await conn.execute(sql)
    finally:
        await conn.close()

asyncio.run(execute(f'CREATE DATABASE "{name}"'))
try:
    subprocess.run([sys.executable,
        "go/testdata/parity/make_config_export_goldens.py", "--db-url", dsn], check=True)
    subprocess.run(["go", "test", "./internal/parity", "-run", "ConfigExport", "-count=1"],
        cwd=Path.cwd() / "go", check=True,
        env={**os.environ, "LOOPGO_CONFIG_EXPORT_PARITY_URL": dsn})
finally:
    asyncio.run(execute(f'DROP DATABASE "{name}"'))
PY
```

`LOOPGO_CONFIG_EXPORT_PARITY_URL` selects an already seeded disposable database;
the Go HTTP parity test reads it without replacing its schema. Other parity
suites use different variables and should run in their own databases.

Initial generation used offline rows because the execution sandbox denied TCP
connections to local PostgreSQL. PostgreSQL schema/seed execution and SQL-read
parity still require the database command above in an environment that permits
that connection.
