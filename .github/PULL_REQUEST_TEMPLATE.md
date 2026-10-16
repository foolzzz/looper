## Goal

<!-- The requested outcome and where it comes from (issue, plan stage, bug),
the gap before this change and the observable result after it. If this is
one step of a larger change, name what remains and who or what picks it up. -->

## Changes

<!-- What changed, grouped the way a reviewer checks it. -->

## Behavior changes

<!-- Anything a user or an operator will notice: removed or renamed commands,
options, API fields or defaults. Write "None" when there are none. -->

## Validation

<!-- Facts, not a self-assessment. For each check: the command or procedure
(repository-relative), the commit it ran on, and the result. Include failures,
skips and anything not run, with the reason. Unit tests do not prove the real
entry point: say how `looper serve`, the database, the real Claude CLI or the
browser were exercised when the change touches them. -->

## Checklist

- [ ] No credentials, local paths, raw logs, runtime state or private context in the diff, the description or attachments.
- [ ] Docs and the plan are updated where behavior or a todo changed.
- [ ] A change to the first screen of `README.md` or `README.zh-CN.md` was previewed and approved before this commit.
- [ ] UI changes include screenshots (desktop and narrow).
