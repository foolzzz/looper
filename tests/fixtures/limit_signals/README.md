# Limit-signal protocol fixtures

These are synthetic, credential-free examples of verified protocol shapes,
not recordings from a live account or evidence that quota was exhausted.
Times, usage percentages and identifiers are synthetic. No model request or
quota-exhaustion experiment was used to establish the facts below.

## Codex app-server

Verified against `codex-cli 0.169.0` using its generated JSON schema and
the corresponding official source at tag `rust-v0.169.0`
(`a41fe5b443652cbdbee57717fbb9fd78d8d8b32a`):

- The event is actually emitted, not merely declared in the schema:
  [`handle_token_count_event`](https://github.com/openai/codex/blob/a41fe5b443652cbdbee57717fbb9fd78d8d8b32a/codex-rs/app-server/src/bespoke_event_handling.rs#L1653-L1679)
  sends `AccountRateLimitsUpdated` whenever the core token-count event
  includes rate limits. The same file's
  `test_handle_token_count_event_emits_usage_and_rate_limits` checks it.
- `resetsAt` is **Unix epoch seconds**, not milliseconds. The
  [core `RateLimitWindow`](https://github.com/openai/codex/blob/a41fe5b443652cbdbee57717fbb9fd78d8d8b32a/codex-rs/protocol/src/protocol.rs#L2695-L2703)
  documents the unit; the
  [app-server conversion](https://github.com/openai/codex/blob/a41fe5b443652cbdbee57717fbb9fd78d8d8b32a/codex-rs/app-server-protocol/src/protocol/v2/account.rs#L837-L852)
  preserves that value. `windowDurationMins` is minutes and `usedPercent`
  is a percentage (the app-server rounds the core floating value).
- Updates are sparse. The
  [notification documentation](https://github.com/openai/codex/blob/a41fe5b443652cbdbee57717fbb9fd78d8d8b32a/codex-rs/app-server-protocol/src/protocol/v2/account.rs#L735-L744)
  says to merge available values with the previous snapshot; null account
  metadata does not clear earlier observations.
- `account/rateLimits/read` is a non-model backend usage lookup requiring
  ChatGPT authentication, as shown by
  [its handler](https://github.com/openai/codex/blob/a41fe5b443652cbdbee57717fbb9fd78d8d8b32a/codex-rs/app-server/src/request_processors/account_processor.rs#L1191-L1228).
  This source inspection establishes the path, not live availability for
  a particular account. Admission and reset probing are outside this change.

## Claude Code

Verified by inspecting the shipped schema and serializer in the locally
installed **Claude Code 2.1.326** executable (no account traffic). The
`rateLimitType` enum in its rate-limit information schema contains:

`five_hour`, `seven_day`, `seven_day_opus`, `seven_day_sonnet`,
`seven_day_overage_included`, `overage`.

Thus the top-level claim can name model-specific windows. It must not be
restricted to the `seven_day` value observed in earlier examples. The
schema's `unifiedWindows` currently declares `five_hour`, `seven_day` and
`seven_day_overage_included`; the top-level claim carries the limiting
window separately. The schema explicitly documents `resetsAt` as Unix
epoch seconds and utilization as a fraction. It also notes legitimate
utilization above 1, optional per-window status, and that per-window
rejection does not itself establish that requests were refused (overage
can remain available).

The stream serializer constructs `type: "rate_limit_event"` with
`rate_limit_info`. It does not require a tool call in that serialization
path. Whether a particular tool-free processor run receives such an
event still depends on its responses carrying the quota headers; this
was not established through a live request.
