"""Read-only MR verification with a closed protocol and no message context.

An empty working directory is not a sandbox. The operator must trust the installed
executable and its account permissions before enabling adapter execution.
"""
from __future__ import annotations

import asyncio
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
import json
import os
import re
import signal
import tempfile
from types import MappingProxyType
from typing import Any, Mapping
from urllib.parse import urlsplit, urlunsplit

from .intent_rules_model import validate_adapter

TIMEOUT_SECONDS = 10.0
STDOUT_LIMIT = 64 * 1024
_UNAVAILABLE_REASONS = frozenset({
    "adapter_missing", "adapter_start_failed", "verification_timeout", "verification_output_limit",
    "verification_exit", "verification_protocol", "authentication_failed", "permission_denied",
    "not_readable", "cannot_decide", "upstream_error", "ambiguous_target",
})
_REMOTE_REASONS = frozenset({"authentication_failed", "permission_denied", "not_readable", "cannot_decide", "upstream_error"})
_CREDENTIALS: Mapping[str, str] = MappingProxyType({})


def configure_credentials(credentials: Mapping[str, str]) -> None:
    """Host bootstrap injection for dedicated read-only secrets, never a rule/API operation.

    Only exact configured secret references are resolved. Values are copied in
    memory and never inherited from the environment or exposed by management.
    Passing an empty mapping clears the installed resolver.
    """
    global _CREDENTIALS
    if not isinstance(credentials, Mapping) or any(
        not isinstance(key, str) or not key.startswith("secret:") or not isinstance(value, str)
        or not value or "\x00" in value for key, value in credentials.items()
    ):
        raise ValueError("Invalid dedicated verification credentials")
    _CREDENTIALS = MappingProxyType(dict(credentials))


_SEGMENT = re.compile(r"[A-Za-z0-9._~-]+\Z")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass(frozen=True)
class MRTarget:
    system_id: str
    mr_url: str
    host: str
    route_ids: dict[str, str]

    @property
    def canonical_url(self) -> str:
        return self.mr_url

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> MRTarget:
        if set(value) != {"system_id", "mr_url", "host", "route_ids"}:
            raise ValueError("Invalid prepared MR target")
        if any(not isinstance(value[key], str) or not value[key] for key in ("system_id", "mr_url", "host")):
            raise ValueError("Invalid prepared MR target")
        ids = value["route_ids"]
        if not isinstance(ids, dict) or not ids.get("mr_id") or any(
            not isinstance(key, str) or not isinstance(part, str) or not part for key, part in ids.items()
        ):
            raise ValueError("Invalid prepared MR target")
        return cls(value["system_id"], value["mr_url"], value["host"], dict(ids))


@dataclass(frozen=True)
class VerifiedFact:
    target: MRTarget
    exists: bool
    state: str | None = None
    reviewers: tuple[str, ...] = ()
    repository: str | None = None
    mr_id: str | None = None
    revision: str | None = None
    canonical_url: str | None = None
    verified_at: str = field(default_factory=_now)
    # The configured identity is not supplied by a message or adapter response.
    operator_id: str = ""

    @property
    def identity(self) -> dict[str, str] | None:
        if not self.exists:
            return None
        return {"system_id": self.target.system_id, "repository_id": self.repository,
                "mr_id": self.mr_id, "revision": self.revision, "canonical_url": self.canonical_url}

    def to_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["status"] = "verified"
        value["reviewers"] = list(self.reviewers)
        return value

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> VerifiedFact:
        fields = dict(value)
        if fields.pop("status", None) != "verified":
            raise ValueError("Invalid persisted verification status")
        fields["target"] = MRTarget.from_mapping(fields["target"])
        fields["reviewers"] = tuple(fields["reviewers"])
        if "operator_id" not in fields:
            fields["operator_id"] = ""
        return cls(**fields)


@dataclass(frozen=True)
class VerificationUnavailable:
    reason: str
    verified_at: str = field(default_factory=_now)

    def __post_init__(self) -> None:
        if self.reason not in _UNAVAILABLE_REASONS:
            raise ValueError("Invalid verification reason")

    def to_dict(self) -> dict[str, Any]:
        return {"status": "unavailable", **asdict(self)}

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> VerificationUnavailable:
        if set(value) != {"status", "reason", "verified_at"} or value["status"] != "unavailable":
            raise ValueError("Invalid persisted verification status")
        return cls(value["reason"], value["verified_at"])


VerificationResult = VerifiedFact | VerificationUnavailable


def fact_from_mapping(value: Mapping[str, Any]) -> VerificationResult:
    return (VerifiedFact.from_mapping(value) if value.get("status") == "verified"
            else VerificationUnavailable.from_mapping(value))


def parse_target(mr_url: str, config: Mapping[str, Any] | None) -> MRTarget | None:
    """Normalize only provable aliases; reject unsupported URL syntax before exec."""
    if not isinstance(mr_url, str) or not config or any(ord(char) <= 32 or ord(char) == 127 for char in mr_url):
        return None
    try:
        adapter = validate_adapter(config)
        parsed = urlsplit(mr_url)
        host = parsed.hostname
        if (parsed.scheme != "https" or not host or not host.isascii() or host.endswith(".")
                or parsed.username is not None or parsed.password is not None or parsed.fragment or parsed.query
                or parsed.port not in (None, 443) or "?" in mr_url or "#" in mr_url
                or "\\" in mr_url or "%" in parsed.netloc or "%" in parsed.path
                or parsed.netloc.lower() not in {host.lower(), host.lower() + ":443"}
                or host.lower() not in adapter["allowed_domains"]):
            return None
        parts = parsed.path.split("/")[1:]
        if not parsed.path.startswith("/") or len(parts) != len(adapter["route"]):
            return None
        ids: dict[str, str] = {}
        for segment, expected in zip(parts, adapter["route"]):
            if not _SEGMENT.fullmatch(segment) or segment in {".", ".."}:
                return None
            if expected["kind"] == "literal":
                if segment != expected["value"]:
                    return None
            else:
                ids[expected["name"]] = segment
        canonical = urlunsplit(("https", host.lower(), parsed.path, "", ""))
        return MRTarget(adapter["system_id"], canonical, host.lower(), ids)
    except (ValueError, TypeError, KeyError, AttributeError):
        return None


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, part in pairs:
        if key in value:
            raise ValueError("Duplicate adapter response key")
        value[key] = part
    return value


def _nonempty(value: Any) -> bool:
    return isinstance(value, str) and bool(value) and not any(
        char.isspace() or char == "*" or ord(char) < 32 or ord(char) == 127 for char in value)


def _response(raw: bytes, target: MRTarget, config: Mapping[str, Any]) -> VerificationResult:
    try:
        value = json.loads(raw.decode("utf-8"), object_pairs_hook=_unique_object,
                           parse_constant=lambda _value: (_ for _ in ()).throw(ValueError("Non-JSON constant")))
        if not isinstance(value, dict) or type(value.get("schema_version")) is not int or value["schema_version"] != 1:
            raise ValueError("Invalid protocol version")
        if value.get("status") == "verification_unavailable":
            if set(value) != {"schema_version", "status", "reason"} or value["reason"] not in _REMOTE_REASONS:
                raise ValueError("Invalid unavailable response")
            return VerificationUnavailable(value["reason"])
        if value.get("status") != "verified" or set(value) != {"schema_version", "status", "facts"}:
            raise ValueError("Invalid verification response")
        facts = value["facts"]
        if not isinstance(facts, dict) or type(facts.get("exists")) is not bool:
            raise ValueError("Invalid existence fact")
        if facts["exists"] is False:
            if set(facts) != {"exists"}:
                raise ValueError("Invalid absent MR fact")
            return VerifiedFact(target, False, operator_id=config["operator_id"])
        if set(facts) != {"exists", "state", "reviewers", "repository", "mr_id", "revision", "canonical_url"}:
            raise ValueError("Invalid MR fact fields")
        if (facts["state"] not in {"open", "merged", "closed"} or not isinstance(facts["reviewers"], list)
                or any(not _nonempty(reviewer) for reviewer in facts["reviewers"])
                or any(not _nonempty(facts[key]) for key in ("repository", "mr_id", "revision", "canonical_url"))):
            raise ValueError("Invalid MR fact values")
        canonical = parse_target(facts["canonical_url"], config)
        if (canonical is None or canonical.system_id != target.system_id or canonical.host != target.host
                or canonical.route_ids != target.route_ids or facts["mr_id"] != target.route_ids["mr_id"]
                or ("repository_id" in target.route_ids and facts["repository"] != target.route_ids["repository_id"])):
            return VerificationUnavailable("cannot_decide")
        return VerifiedFact(target, True, facts["state"], tuple(facts["reviewers"]), facts["repository"],
                            facts["mr_id"], facts["revision"], canonical.mr_url, operator_id=config["operator_id"])
    except (ValueError, TypeError, KeyError, UnicodeError):
        return VerificationUnavailable("verification_protocol")


class _OutputLimit(Exception):
    pass


async def _terminate(proc: asyncio.subprocess.Process) -> None:
    # The session leader can exit while descendants still hold the output pipes.
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    await proc.wait()


async def verify(mr_url: str, config: Mapping[str, Any] | None, *,
                 credentials: Mapping[str, str] | None = None) -> VerificationResult:
    """Run a single configured verification; failures never retry or grant authority."""
    try:
        adapter = validate_adapter(config)
    except (ValueError, TypeError):
        return VerificationUnavailable("adapter_missing")
    if adapter is None or not adapter["operator_id"]:
        return VerificationUnavailable("adapter_missing")
    target = parse_target(mr_url, adapter)
    if target is None:
        return VerificationUnavailable("cannot_decide")
    env = dict(adapter["env"])
    for name, reference in adapter["credential_env"].items():
        secret = (credentials if credentials is not None else _CREDENTIALS).get(reference)
        if not isinstance(secret, str) or not secret or "\x00" in secret:
            return VerificationUnavailable("adapter_missing")
        env[name] = secret
    executable = adapter["executable"]
    proc: asyncio.subprocess.Process | None = None

    async def call(cwd: str) -> VerificationResult:
        nonlocal proc
        proc = await asyncio.create_subprocess_exec(
            executable, *adapter["args"], stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE, cwd=cwd, env=env, start_new_session=True,
        )
        output = bytearray()

        async def read_stdout() -> None:
            while chunk := await proc.stdout.read(8192):
                if len(output) + len(chunk) > STDOUT_LIMIT:
                    raise _OutputLimit
                output.extend(chunk)

        async def discard_stderr() -> None:
            while await proc.stderr.read(8192):
                pass

        request = json.dumps({"schema_version": 1, "mr_url": target.mr_url}, separators=(",", ":")).encode()
        proc.stdin.write(request)
        try:
            await proc.stdin.drain()
        except (BrokenPipeError, ConnectionResetError):
            pass
        proc.stdin.close()
        streams = [asyncio.create_task(read_stdout()), asyncio.create_task(discard_stderr())]
        try:
            await asyncio.gather(*streams)
            code = await proc.wait()
            await _terminate(proc)
        finally:
            for task in streams:
                task.cancel()
            await asyncio.gather(*streams, return_exceptions=True)
        if code != 0:
            return VerificationUnavailable("verification_exit")
        return _response(bytes(output), target, adapter)

    with tempfile.TemporaryDirectory(prefix="looper-mr-verify-") as cwd:
        try:
            return await asyncio.wait_for(call(cwd), timeout=TIMEOUT_SECONDS)
        except asyncio.TimeoutError:
            if proc is not None:
                await _terminate(proc)
            return VerificationUnavailable("verification_timeout")
        except _OutputLimit:
            if proc is not None:
                await _terminate(proc)
            return VerificationUnavailable("verification_output_limit")
        except (OSError, ValueError):
            if proc is not None:
                await _terminate(proc)
            return VerificationUnavailable("adapter_start_failed")
        except asyncio.CancelledError:
            if proc is not None:
                await asyncio.shield(_terminate(proc))
            raise
