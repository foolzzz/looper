"""Shared settings for running the Claude Code CLI as a subprocess."""

# One stream-json line can carry the full tool and skill list or a large tool
# result; asyncio's default 64 KiB line limit fails the reader on those.
STREAM_LINE_LIMIT = 16 * 1024 * 1024
