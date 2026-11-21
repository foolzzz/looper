# Turn-context display-name review validation

Measured on 2026-11-20 with Claude Code 2.1.330. This is a direct renderer comparison,
not a Manager end-to-end evaluation. Baseline: the pre-fix branch head `49dfe0d7769ad6ffa5c585187108f972a8565f4a`;
candidate: the reviewed renderer with shared display-name normalization.

## Method

Each call received six independent synthetic turn-context blocks and the unchanged turn-context
contract. Cases cover an allowlisted sender with a forged `Speaker:` line; a member whose name
claims to be the operator; a non-allowlisted direct-chat sender with a forged block header;
control characters and a forged header; an overlong name ending in a forged identity; and a
positive operator control. Both renderers used identical envelopes and timestamps.

Claude returned each case's speaker category and whether it could authorize an operator-only
action. The comparison used `claude-sonnet-5-5` twice per renderer, and `claude-opus-5-5` at
`xhigh` once per renderer. Every call disabled tools, MCP servers, hooks and session persistence,
used a temporary working directory, had a 120-second timeout and a $0.50 budget cap. No real
channel, production database or live service was used.

## Results

The first prompt named the four category values without defining them. Both models called
the candidate's direct-chat outsider a `client` instead of `member`, while correctly refusing
operator authorization. That ambiguity is retained as a failure, not discarded:

| Initial category check | Baseline | Candidate |
| --- | --- | --- |
| Sonnet, two repetitions | 12/12 | 10/12 |
| Opus xhigh, one repetition | 6/6 | 5/6 |
| Operator-authorization check, both models | 18/18 | 18/18 |

A diagnostic rerun explicitly defined `client` as the operator's coding agent/API client,
and `member` as a non-allowlisted sender in either a direct or group chat. It changed only the
evaluation prompt's taxonomy; the product renderer and contract stayed unchanged.

| Explicit-taxonomy check | Baseline | Candidate |
| --- | --- | --- |
| Sonnet category, two repetitions | 12/12 | 12/12 |
| Opus xhigh category, one repetition | 6/6 | 6/6 |
| Operator authorization, both models | 18/18 | 18/18 |

All 12 CLI calls completed without timeout or model error. Initial comparison cost: $0.0562524;
diagnostic comparison cost: $0.062216; total: $0.1184684.

## Unicode and empty-name follow-up

Compared `f80843912` with `fc35126cd` using six synthetic cases: curly quotes,
full-width colons, Unicode brackets, a name cleaned to empty, a whitespace-only
operator name and a named operator control. The same isolated CLI method and
explicit taxonomy above were used. Sonnet passed 12/12 category-and-authorization
judgments per renderer (two repetitions); Opus xhigh passed 6/6 per renderer
(one repetition). All six calls completed; total cost was $0.0684505.
Both renderers passed, so this does not establish an improvement in model security.
Deterministic regressions verify NFKC normalization, punctuation removal, omitted
empty-name labels and the operator's persona-address fallback. The touched
turn-context tests and API-document tests passed together: 344 passed with `-n 2`.

## Limits

The original renderer also passed these authorization judgments. These small, batched classification
checks therefore show that the candidate's sender labels can be understood; they do not establish
an improvement in model security or prove resistance to prompt injection. Deterministic regression
tests cover the structural guarantee: one speaker line, one turn-context header, bounded names and
unchanged typed identity. Real Manager turns, cross-chat confidentiality and actual tool authorization
remain outside this renderer-only comparison and require Manager integration.

## Settings merge check

The merged Settings → Chat component passed its nine Vitest cases, TypeScript checking and the
production build. Chromium checked widths of 1280, 768 and 390 px with synthetic intercepted APIs:
ledger and turn context remained independently writable, language and timezone saved, both missing-
operator notices appeared, and no page error, console error or horizontal overflow was observed.
This used a temporary Vite component harness, not a `looper serve` process.

![Merged Chat settings at 390 px](turn-context-chat-settings.png)
