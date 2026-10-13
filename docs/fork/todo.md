# Fork backlog

Status as of 2026-10-09 (`main` at `db206f171`).

Use case: requirement decomposition → agent development → iteration →
human-in-the-loop confirmation. Items below are scoped to what affects this
workflow.

## Setup and validation

- **End-to-end validation with a real model.** The last full run with a real
  model predates the App removal and cleanup batch. Run a disposable goal
  through the dispatcher with the `codex-cli` or `claude-code` host and
  verify the full lifecycle: goal create → todo decomposition → agent Turn →
  gate decision → acceptance.
- **Install required skills.** `loopx doctor` reports missing skills.
  Run `loopx slash-commands --install` and `loopx workflow-skills --install`.

## Known limitations

- The CLI closes a typed gate before its settlement records the chosen
  option. If the process dies between the two steps, the option is not
  recorded, and a retry settles with the option that the retry passes.
- A goal without a code repository runs its developer and acceptor Turns
  directly in the project directory, one Turn at a time.
- Closing a goal does not remove the developer worktrees. Use
  `loopx workspace cleanup` for each Todo.
- ~~`loopx update apply` defaults to upstream~~ — fixed, now points to
  this fork, `foolzzz/loopx`.

## Deferred (not blocking current use)

- **Dependency vulnerability alerts.** 36 Dependabot alerts on upstream,
  all from pinned lockfiles. Triage when deploying affected components.
- **Typing debt.** 4,058 mypy errors across 517 files. Does not affect
  runtime.
- **CI test ordering issue ([#143]).** A test assertion depends on slack
  counter order. Only matters for upstream CI contribution.

[#143]: https://github.com/foolzzz/loopx/issues/143
