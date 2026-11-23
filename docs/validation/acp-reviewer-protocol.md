# ACP Reviewer v1 protocol boundary

The client follows the public [ACP v1 schema](https://agentclientprotocol.com/protocol/v1/schema)
and uses one process, one session and one prompt. It does not import a vendor ACP
client. `tests/fake_acp.py` exercises the transport without calling a model.

## Fields checked

The official v1 schema was read on 2026-11-22, including the Markdown source at
`https://agentclientprotocol.com/protocol/v1/schema.md`.
The fetched Markdown had 7,535 lines and SHA-256
`3839149fa2489916c5305b3eb1b66fe7302500d8cced348afae84309e795fbb3`.
Its relevant headings were `PromptResponse` (line 638), `Cost` (2702),
`SessionConfigOptionCategory` (5222), `ToolKind` (7407) and `UsageUpdate` (7503).

| Input | Fields used | Behavior |
|---|---|---|
| `initialize` response | `protocolVersion` | Only integer `1` is accepted. |
| `session/new` response | `sessionId` | A nonempty string is required. |
| `session/update` | `sessionId`, `update.sessionUpdate` | Updates from another session are ignored. |
| Message chunks | `content.type`, `content.text` | Text and thoughts become internal assistant events. |
| Tool calls | `toolCallId`, `title`, `kind`, `rawInput`, `status`, `content`, `rawOutput` | Internal assistant tool uses and user tool results. |
| Permission requests | `toolCall.kind`, `options[].kind`, `options[].optionId` | Only `read` and `search` may select `allow_once`; other kinds select `reject_once` or cancel. Titles and descriptions never authorize a request. |
| `session/prompt` response | `stopReason` | Only `end_turn` with streamed final text succeeds. A response containing just `messageId` fails. |
| `usage_update` | `cost.amount`, `cost.currency` | Finite nonnegative USD is retained only as `metadata.runtime_reported_usd`. |

Initialization and session creation have independent deadlines of 30 and 60
seconds. On failure or cancellation, the channel queues `session/cancel` if the
session exists and stdin is writable, then closes stdin. Process-tree cleanup
remains with `ProcessRun` and its existing termination grace period.

## Usage limitation and task-card conflict

The current documented
[PromptResponse](https://agentclientprotocol.com/protocol/v1/schema#promptresponse)
has `stopReason` and `_meta`; it has no billable input/output/cache token fields.
[UsageUpdate](https://agentclientprotocol.com/protocol/v1/schema#usageupdate)
has context `used`, context `size`, and optional session `cost`. Context size is
not billable token usage. `_meta` is explicitly extensible and is ignored.

The task card also requests preservation of billable tokens and a reported model
name. No documented v1 source for those fields was found in the checked schema.
Its model category in `configOptions` is a UI category, and `currentValue` is an
opaque option value identifier; neither establishes the actual model name.
The implementation therefore does not invent `usage.inputTokens` or infer a model
from option labels. `usage_complete` is always false, token columns and
`cost_usd` stay NULL, including when a vendor supplies token-looking extensions.
The recorder still records the model declared by the Profile and command
settings. This follows the RFC's requirement to read only documented fields.

`tests/test_acp_channel.py` fixes this boundary with a stand-in that sends both
vendor `_meta.usage` and an undocumented top-level usage object. Neither is used
for billing. A documented USD figure also cannot fill `cost_usd` without tokens.
Future support for documented billable usage needs a protocol source and a new
test before it can make usage complete.

## Pending real-model validation

No real CLI handshake or model request is part of this validation. A maintainer
may run one `grok agent stdio` review in a throwaway worktree to check the v1
handshake, structured permission kinds and actual usage messages. Proposed
budget: at most $0.10, subject to confirmation; the RFC cites approximately
$0.074 for a cold ACP Manager turn, not a Reviewer upper bound. The four-task
comparison remains a separate change.
