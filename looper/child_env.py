"""The environment Looper starts other programs with: the variables of its own that this module names, never all of
them.

``looper serve`` holds what no child needs: the database URL (from the ops file, or for one release from
``LOOPER_DATABASE_URL``), ``PGPASSWORD`` and whatever else its environment carries. Every child it starts (an
agent's runtime and the MCP servers that runtime starts, a schedule's gate, a probe, a git, ps or lark-cli helper)
starts from ``inherited()``: the variables named here (``NAMES``) that the server's environment sets, and no other.
The caller then adds what that child needs: a provider binding, an agent's own variables, ``LOOPER_AGENT_TOKEN``, a
gate's run. A database backup gets ``backup()``.

No ``LOOPER_*`` or ``LOOPX_*`` variable is passed on: the ones a child reads are set by Looper for that child. An
agent's settings can give it any other variable (``Launch.env``).

This keeps secrets out of the children's environments; it is not a sandbox. A child runs as the server's user, so
an agent with a shell can still read every file that user can read: the ops file, the operator token, a checkout's
``.env``, the service unit.
"""

from __future__ import annotations

import os
from collections.abc import Mapping

# The system's: where programs and the user's files are, temporary files, the shell, locale and time zone; how git
# reaches a remote, which configuration it reads and who commits; Python's import path.
SYSTEM = (
    "PATH", "HOME", "USER", "LOGNAME", "SHELL", "TMPDIR", "TMP", "TEMP", "TERM", "TZ",
    "LANG", "LANGUAGE", "LC_ALL", "LC_CTYPE", "LC_COLLATE", "LC_MESSAGES", "LC_MONETARY", "LC_NUMERIC", "LC_TIME",
    "XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_STATE_HOME", "XDG_CACHE_HOME", "XDG_RUNTIME_DIR", "XDG_CONFIG_DIRS",
    "XDG_DATA_DIRS",
    "SSH_AUTH_SOCK", "GIT_SSH", "GIT_SSH_COMMAND", "GIT_CONFIG_GLOBAL", "GIT_CONFIG_SYSTEM", "GIT_CONFIG_NOSYSTEM",
    "GIT_AUTHOR_NAME", "GIT_AUTHOR_EMAIL", "GIT_COMMITTER_NAME", "GIT_COMMITTER_EMAIL", "PYTHONPATH",
)
# Proxies (local requests bypass them: NO_PROXY), and the certificates a proxy that inspects TLS needs.
NETWORK = (
    "HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "no_proxy", "all_proxy",
    "SSL_CERT_FILE", "SSL_CERT_DIR", "NODE_EXTRA_CA_CERTS", "REQUESTS_CA_BUNDLE", "CURL_CA_BUNDLE",
)
# The runtimes' own: their directories, and the login and endpoint an operator may set for a runtime's own provider.
# A run's provider binding sets or removes these for its provider (``ProviderBinding``). Claude Code: its config
# directory, an API key or token and endpoint, the token of ``claude setup-token`` (CLAUDE_CODE_OAUTH_TOKEN), the
# model names; Codex: CODEX_HOME (its auth.json) and the key ``codex exec`` reads; Grok: GROK_HOME and its keys. omp
# runs with API keys only, through the binding (``LOOPER_PROVIDER_API_KEY``), and its directories are Looper's.
RUNTIMES = (
    "CLAUDE_CONFIG_DIR", "CODEX_HOME", "GROK_HOME", "COPILOT_HOME", "COPILOT_CUSTOM_INSTRUCTIONS_DIRS",
    "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "CLAUDE_CODE_OAUTH_TOKEN", "ANTHROPIC_BASE_URL", "ANTHROPIC_MODEL",
    "ANTHROPIC_DEFAULT_OPUS_MODEL", "ANTHROPIC_DEFAULT_SONNET_MODEL", "ANTHROPIC_DEFAULT_HAIKU_MODEL",
    "ANTHROPIC_SMALL_FAST_MODEL", "CLAUDE_CODE_SUBAGENT_MODEL",
    "CODEX_API_KEY", "CODEX_ACCESS_TOKEN", "XAI_API_KEY", "GROK_CODE_XAI_API_KEY", "GROK_AUTH",
)
NAMES = frozenset((*SYSTEM, *NETWORK, *RUNTIMES))
# What a backup's command may also need: libpq's own variables (PGPASSWORD among them, when the operator gives the
# server its password that way) and, for a command that dumps inside a container, Docker's.
BACKUP_PREFIXES = ("PG",)
BACKUP_NAMES = frozenset({"DOCKER_HOST", "DOCKER_CONTEXT", "DOCKER_CONFIG", "DOCKER_CERT_PATH", "DOCKER_TLS_VERIFY"})

_allowed: set[str] = set(NAMES)


def allow(*names: str) -> None:
    """Pass ``names`` on too, in this process (the test suite's guard passes its own this way)."""
    _allowed.update(names)


def inherited(environ: Mapping[str, str] | None = None) -> dict[str, str]:
    """The variables of ``environ`` (the server's environment) a child starts with."""
    environ = os.environ if environ is None else environ
    return {name: value for name, value in environ.items() if name in _allowed}


def backup(environ: Mapping[str, str] | None = None) -> dict[str, str]:
    """The variables of ``environ`` a database backup starts with: a child's, libpq's and Docker's."""
    environ = os.environ if environ is None else environ
    return {**inherited(environ),
            **{name: value for name, value in environ.items()
               if name in BACKUP_NAMES or name.startswith(BACKUP_PREFIXES)}}
