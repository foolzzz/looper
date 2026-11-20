# Message leads CLI and operator API follow-up

## Regression coverage

The pre-fix run reproduced the OpenAPI description prefix, eager CLI imports,
unequal skill copies, late CLI validation, disabled cleanup, promoted marking,
exclusion timestamp and audit-content failures. The cross-chat case also showed
that deleting a merge target silently removed another chat's intent.

The regression now excludes chat A with deletion while T belongs to A and M is
merged into T with sources only in B. A third merged intent has sources in both
chats. Exactly two intents are deleted; M survives as pending with its source,
no merge target and no handled metadata. `detached_intents` is 1. The same case
runs with leads enabled and disabled; the transition log contains ids and a
reason, without message text. Existing coverage keeps tasked intents intact.

## Disposable service readback

A fresh database, temporary runtime root and free loopback port exercised the
actual serve process, operator-authenticated HTTP API and CLI subprocesses.
Only synthetic messages were inserted; no channel delivery or model task ran.

- Invalid search limit returned exit 2; valid search while disabled returned 3.
- Marking followed by source-message deletion left no lead content in the
  operator audit trail; settings writes confirmed audit was active.
- Exclusion with deletion while disabled deleted one target and restored its
  other-chat merged intent, reporting deleted=1 and detached=1.
- Repeated exclusion retained its first timestamp.
- After stopping the server, CLI deletion while disabled returned exit 0 and
  database readback confirmed removal. The disposable database was dropped.

## Real-mode skill comparison

One focused synthetic conversation was run twice per version with the real
Claude CLI, Sonnet (`claude-sonnet-5-5`). The baseline was the skill on the
previous PR head; the candidate was the synchronized pair of updated skills.
Personal settings and tools were disabled. The conversation supplied a prior
`looper leads search CSV export` result with exit 3 and asked the model to
continue discussing the feature without submitting a task.

| Criterion | Baseline | Candidate |
| --- | --- | --- |
| No retry and no mention of disabled leads | 0/2 | 2/2 |
| No next command | 2/2 | 2/2 |

Both baseline replies mentioned that leads were disabled. Both candidate
replies continued discussing the requirement without mentioning the switch.
This measures the model's next response to a supplied exit result; it does not
measure live message collection, tool execution or general task performance.

## Dependency gap

Until #1451 lands, a real resolved Worker `Launch.environment()` has no
`LOOPER_AGENT_RUN`: the CLI reaches configuration/database handling (exit 6
with a missing disposable config). Adding the marker as a positive control
returns exit 4. Three subprocess regressions build the real Launch environment
and skip with an explicit #1451 reason while its marker is absent; they assert
exit 4 once it is present. These skips are not passes.
