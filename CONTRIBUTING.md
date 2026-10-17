# Contributing To Looper

Thank you for helping. Bug reports with clear reproduction steps, fixes,
documentation and reviews are all welcome. Please follow the
[Code of Conduct](CODE_OF_CONDUCT.md).

This repository is the Looper agent system (`looper serve` and its dashboard).
[docs/architecture/agent-system.md](docs/architecture/agent-system.md)
describes how it works, and
[the development plan](docs/plans/2026-10-13-long-horizon-agent-context.zh-CN.md)
lists what is in progress and the open todos. Agents working here also follow
[AGENTS.md](AGENTS.md).

## Set Up

You need Python 3.11+, [uv](https://docs.astral.sh/uv/), Node.js 22+ for the
dashboard, PostgreSQL and the Claude CLI.

```bash
uv sync --extra test
cd web && npm ci && npm run build && cd ..
LOOPER_DATABASE_URL=postgresql://localhost:5432/<your database> uv run looper serve --port 8800
```

## Test

```bash
uv run --extra test python -m pytest -n auto
```

The PostgreSQL tests run only when `LOOPER_TEST_DATABASE_URL` points at a
disposable database; they create and drop their own schemas. CI runs the same
command against a PostgreSQL service on every pull request. Which tests a
change needs is described in section 1 of the development plan.

The dashboard's tests (`web/src/**/*.test.tsx`, run by Vitest) render
components without a browser; run them with `cd web && npm test`.

## Pull Requests

- Work on a branch, keep the pull request to one purpose, and leave unrelated
  formatting and refactors out.
- Describe the pull request with **Goal**, **Changes** and **Validation**,
  and list any behavior change explicitly. The
  [template](.github/PULL_REQUEST_TEMPLATE.md) has the outline.
- Validate on the real path: besides the tests, run `looper serve` against a
  disposable database for backend changes. Check UI changes in a browser
  and attach screenshots.
- When you change a recommended prompt, a contract or the injection order,
  attach the real-mode prompt eval comparison
  ([evals/README.md](evals/README.md)).
- A change to the first screen of `README.md` or `README.zh-CN.md` needs the
  maintainer's approval of a preview before it is committed.
- Keep the repository public-safe. Do not commit or paste credentials, local
  paths, raw logs, runtime state or private context.

The maintainer, [@foolzzz](https://github.com/foolzzz), reviews and merges.

## Security

Report a vulnerability privately as described in
[SECURITY.md](.github/SECURITY.md), not in a public issue.

## License

Looper is licensed under the [Apache License 2.0](LICENSE). Unless you state
otherwise, contributions are submitted under the same terms, without a
copyright assignment or a contributor license agreement. Upstream LoopX
releases through v0.4.7 were MIT-licensed; see [NOTICE](NOTICE) and
[LICENSE-MIT](LICENSE-MIT).
