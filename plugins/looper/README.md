# Looper plugin

A skill, `looper-skill`, that lets a coding agent hand a piece of work to the Looper running on your machine and follow it, using only the `loopercli` command: submit through the Looper Manager, answer its questions, start, stop and change tasks, check status, pipeline and gates.

The canonical copy of the skill is `looper/skill/looper-skill/SKILL.md`, shipped in the `looper` wheel; the copy in this plugin is byte-for-byte the same.

## Prerequisites

- `looper serve` is running (if it does not start, `looper doctor` says why).
- A token from Settings → API clients in the dashboard.
- `loopercli login` has stored that token (it reads the token from stdin or a prompt, never from an argument).

`loopercli whoami` tells you whether all three hold.

## Install

**Claude Code**

```
claude plugin marketplace add foolzzz/looper
claude plugin install looper@looper
```

From a checkout of this repository: `claude --plugin-dir plugins/looper`.

**Codex and OMP**

`loopercli skill path` prints the directory of the packaged skill (`loopercli skill show` prints its `SKILL.md`).

- Codex: copy or symlink that directory into `$CODEX_HOME/skills/` (`~/.codex/skills/` by default).
- OMP: reference the `SKILL.md` from your instruction file (`AGENTS.md`).

Looper never writes these for you.

The Codex directory was checked against codex-cli 0.162.0. For OMP (18.4.3) no skills directory could be confirmed, so it is given no directory here: reference `SKILL.md` from your instruction file instead.
