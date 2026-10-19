# Catalog parity fixtures

`catalog_fixture_input.json` is the sole input fixture. The generator derives
`catalog_fixture.sql` from it; do not edit that generated SQL independently.
The 23 `catalog*.json` responses and two error responses in `raw/` use this
checkout's Python v2.10.0 `AgentHTTPServer._handle_catalog`, invoking the real
`catalog_view`, `item_detail` and `project_view` functions.

Acceptance is blocked. The existing responses were generated with the old
offline database-reader substitutes. External database validation found four
file-order mismatches for machine, upload, git and UI skills: PostgreSQL's
collation puts `references/note.txt` before `SKILL.md`, whereas the offline
Python string sort put `SKILL.md` first. Both production implementations use
`ORDER BY path` without an explicit collation. Keep those mixed-case fixtures;
do not change the Go query or sort response arrays to hide this difference.

The generator now requires `--db-url`; offline generation is unsupported.
It requires `LC_COLLATE=LC_CTYPE=en_US.utf8` and checks both before replacing
the schema. Both Go CI workflows pin `POSTGRES_INITDB_ARGS` to
`--locale=en_US.utf8` for their PostgreSQL 16 service.
It publishes outputs only after all database reads and captures succeed.
Connection or capture failures leave the previous outputs untouched. The
current sandbox refuses connections to the disposable PostgreSQL port with
`Operation not permitted`, so these fixtures have not yet been regenerated
through PostgreSQL and are not database-parity evidence.

Run from the repository root to regenerate using an owned disposable database
(create it from `template0` with `LC_COLLATE=LC_CTYPE=en_US.utf8`; the command
replaces that database's public schema):

```sh
.venv/bin/python go/testdata/parity/make_catalog_goldens.py \
  --db-url postgresql://postgres:postgres@127.0.0.1:54329/loopgo_catalog_g1n_parity
```

From `go/`, run the production Go SQL and HTTP path against that same database,
sequentially with other parity suites:

```sh
LOOPGO_TEST_DATABASE_URL=postgresql://postgres:postgres@127.0.0.1:54329/loopgo_catalog_g1n_parity \
  go test ./internal/parity -count=1 -v
```

File-list arrays are ordered data and are compared exactly. Database-backed
regeneration and parity must use the same collation, including in CI. Check
`SELECT datcollate, datctype FROM pg_database WHERE datname=current_database()`
on the owned fixture database. CI's locale is now explicit; successful database
regeneration and parity against that locale remain pending.

The caller creates and drops that dedicated `loopgo_*` database. Do not point
either command at a shared or active database. These commands never read a
default DSN, `.env`, machine credential stores or operator runtime files.

Run generator regressions from the repository root:

```sh
.venv/bin/python -m unittest discover -s go/testdata/parity -p test_catalog_generator.py -v
```

The fixture covers each origin and kind, mixed-case ordering, plugin marketplace
suffixes, project ownership, cross-project installs, both scopes, all roles,
manager-only global exclusion, task-role-only inheritance, disabled installs,
stdio/HTTP/SSE MCP masking, UTF-8 file sizes, listing-only file boundaries,
skills with valid/absent/unclosed frontmatter or missing SKILL.md, item/project
404s, and normal/invalid/scalar/absent import reports. Catalog versions and
seeded timestamps are compared exactly; they are not treated as volatile.
