"""Pure rule decisions and read-only preparation from exactly the cited events."""
from __future__ import annotations

from dataclasses import asdict, dataclass, replace
import hashlib
import json
import re
from typing import Any, Mapping, Sequence

from .intent_rule_verifier import (
    MRTarget, VerificationResult, VerificationUnavailable, VerifiedFact, fact_from_mapping, parse_target, verify,
)

_URL = re.compile(r"https?://[^\s<>\[\]{}\"'，。；！？]+")


@dataclass(frozen=True)
class PreparedSource:
    seq: int
    source_id: str
    chat_id: str
    sender_id: str
    mention_ids: tuple[str, ...]
    dm_recipient_id: str | None
    operator_id: str
    target_urls: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {**asdict(self), "mention_ids": list(self.mention_ids), "target_urls": list(self.target_urls)}

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> PreparedSource:
        fields = dict(value)
        fields["mention_ids"] = tuple(fields["mention_ids"])
        fields["target_urls"] = tuple(fields["target_urls"])
        return cls(**fields)


@dataclass(frozen=True)
class PreparedCandidate:
    index: int
    type_key: str
    sources: tuple[PreparedSource, ...]
    target: MRTarget | None
    first_fact: VerificationResult
    generation: int
    source_fingerprint: str
    flagged: bool = False
    state: str = "pending"
    reason: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {**asdict(self), "sources": [source.to_dict() for source in self.sources],
                "target": self.target.to_dict() if self.target else None, "first_fact": self.first_fact.to_dict()}

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> PreparedCandidate:
        fields = dict(value)
        fields["sources"] = tuple(PreparedSource.from_mapping(part) for part in fields["sources"])
        fields["target"] = MRTarget.from_mapping(fields["target"]) if fields.get("target") else None
        fields["first_fact"] = fact_from_mapping(fields["first_fact"])
        return cls(**fields)


@dataclass(frozen=True)
class PreparedMRBatch:
    candidates: tuple[PreparedCandidate, ...]
    generation: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {"candidates": [candidate.to_dict() for candidate in self.candidates], "generation": self.generation}

    def for_index(self, index: int) -> PreparedCandidate | None:
        return next((item for item in self.candidates if item.index == index), None)


@dataclass(frozen=True)
class RuleDecision:
    action: str
    reason: str
    selection: str = "fallback"
    rule_id: str | None = None
    rule_version: int | None = None
    project_id: str | None = None
    template_key: str | None = None
    conditions: tuple[dict[str, Any], ...] = ()
    # Type/scope skips have no condition kind; keep them separate from closed condition results.
    attempts: tuple[dict[str, Any], ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {**asdict(self), "conditions": [dict(item) for item in self.conditions],
                "attempts": [dict(item) for item in self.attempts]}


def source_fingerprint(sources: Sequence[PreparedSource]) -> str:
    value = [source.to_dict() for source in sorted(sources, key=lambda source: (source.seq, source.source_id))]
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _entity_urls(body: Any) -> list[str]:
    """Read link destinations from structured entities, never their display names."""
    if isinstance(body, str):
        try:
            body = json.loads(body)
        except (ValueError, TypeError):
            return []
    urls: list[str] = []
    if isinstance(body, Mapping):
        if body.get("tag") == "a" or body.get("type") in {"link", "text_link", "url"}:
            for key in ("href", "url"):
                if isinstance(body.get(key), str):
                    urls.append(body[key])
        for key, value in body.items():
            if key not in {"text", "title", "name", "label", "href", "url"}:
                urls.extend(_entity_urls(value))
    elif isinstance(body, list):
        for value in body:
            urls.extend(_entity_urls(value))
    return urls


def _event_urls(event: Mapping[str, Any]) -> tuple[str, ...]:
    entities = _entity_urls(event.get("body")) + _entity_urls(event.get("entities"))
    if entities:
        return tuple(dict.fromkeys(entities))
    text = event.get("text")
    if not isinstance(text, str):
        return ()
    # A closing Markdown delimiter is not part of an URL. All URL syntax is
    # subsequently checked against the installed route before it can be executed.
    return tuple(dict.fromkeys(match.group().rstrip("),;!:") for match in _URL.finditer(text)))


def _source(event: Mapping[str, Any], *, source_id: str, operator_id: str) -> PreparedSource:
    operator = event.get("operator_id") or event.get("user_open_id") or operator_id
    mentions = event.get("mention_ids") or ()
    if isinstance(mentions, str):
        try:
            mentions = json.loads(mentions)
        except ValueError:
            mentions = ()
    if not isinstance(mentions, (tuple, list)) or any(not isinstance(value, str) for value in mentions):
        mentions = ()
    recipient = event.get("dm_recipient_id") or event.get("recipient_id")
    # A trusted p2p event belongs to the event source's mapped operator. The
    # source mapping comes from event_sources, never from the review-system ID.
    if (not recipient and event.get("chat_kind", event.get("kind")) == "p2p"
            and event.get("sender_id") != operator):
        recipient = operator or None
    return PreparedSource(int(event["seq"]), event.get("source_id") or source_id, event.get("chat_id") or "",
                          event.get("sender_id") or "", tuple(mentions), recipient, operator or "", _event_urls(event))


async def prepare_message_candidates(parsed: Any, events: Mapping[int, Mapping[str, Any]] | Sequence[Mapping[str, Any]],
                                     adapter_config: Mapping[str, Any] | None, *, source_id: str = "",
                                     operator_id: str = "", generation: int = 0,
                                     credentials: Mapping[str, str] | None = None,
                                     verify_facts: bool = True) -> PreparedMRBatch:
    """Verify only the current parse's cited events, before any admission transaction."""
    by_seq = dict(events) if isinstance(events, Mapping) else {event["seq"]: event for event in events}
    items = parsed.items if hasattr(parsed, "items") and not isinstance(parsed, Mapping) else parsed.get("items", [])
    flagged = set(getattr(parsed, "instructions_seen_seqs", ()) if not isinstance(parsed, Mapping)
                  else parsed.get("instructions_seen_seqs", ()))
    prepared: list[PreparedCandidate] = []
    # Same canonical target in one batch has one first observation; the final
    # creation check remains a separate adapter call outside the transaction.
    facts: dict[str, VerificationResult] = {}
    for index, item in enumerate(items):
        seqs = list(dict.fromkeys(item.get("sources", ())))
        refs = tuple(_source({**dict(by_seq[seq]), "seq": seq}, source_id=source_id, operator_id=operator_id)
                     for seq in seqs if seq in by_seq)
        if adapter_config:
            refs = tuple(replace(source, target_urls=tuple(
                normalized.mr_url if (normalized := parse_target(url, adapter_config)) else url
                for url in source.target_urls)) for source in refs)
        target = None
        reason = None
        first: VerificationResult = VerificationUnavailable("cannot_decide")
        type_key = item.get("type", item.get("type_key", ""))
        if type_key == "mr_review":
            if len(refs) != len(seqs):
                reason = "source_revoked"
            elif not adapter_config:
                first = VerificationUnavailable("adapter_missing")
                reason = "adapter_missing"
            else:
                urls = {url for source in refs for url in source.target_urls}
                targets = [parse_target(url, adapter_config) for url in urls]
                valid = {candidate.mr_url: candidate for candidate in targets if candidate is not None}
                if len(valid) != 1 or any(candidate is None for candidate in targets):
                    reason = "ambiguous_target"
                    first = VerificationUnavailable("ambiguous_target")
                else:
                    target = next(iter(valid.values()))
                    if verify_facts:
                        if target.mr_url not in facts:
                            facts[target.mr_url] = await verify(target.mr_url, adapter_config, credentials=credentials)
                        first = facts[target.mr_url]
        # Persist only normalized target references. Invalid URLs may contain
        # userinfo or attacker-controlled query material and are never evidence.
        if adapter_config:
            refs = tuple(replace(source, target_urls=tuple(
                normalized.mr_url for url in source.target_urls
                if (normalized := parse_target(url, adapter_config)) is not None)) for source in refs)
        else:
            refs = tuple(replace(source, target_urls=()) for source in refs)
        prepared.append(PreparedCandidate(index, type_key, refs, target, first, generation,
                                          source_fingerprint(refs), bool(set(seqs) & flagged) or bool(item.get("flagged")),
                                          item.get("state", "pending"), reason))
    return PreparedMRBatch(tuple(prepared), generation)


def _condition(kind: str, condition: Mapping[str, Any], candidate: PreparedCandidate,
               facts: VerificationResult) -> tuple[str, str, str]:
    if kind in {"internal_mr_open", "operator_is_reviewer", "mr_repository_in_allowlist"}:
        if isinstance(facts, VerificationUnavailable):
            condition_reason = facts.reason if facts.reason in {
                "authentication_failed", "permission_denied", "not_readable", "cannot_decide", "upstream_error"
            } else "adapter_not_configured" if facts.reason == "adapter_missing" else "verification_unavailable"
            return "unavailable", condition_reason, facts.reason
        if not facts.exists:
            return "failed", "mr_not_found", "mr_missing"
        if facts.state != "open":
            return "failed", "mr_not_open", "mr_not_open"
        if kind == "internal_mr_open":
            allowed = facts.target.host in condition["allowed_domains"]
            return ("passed", "passed", "matched") if allowed else ("failed", "condition_not_met", "condition_failed")
        if kind == "operator_is_reviewer":
            allowed = bool(facts.operator_id) and facts.operator_id in facts.reviewers
            return ("passed", "passed", "matched") if allowed else ("failed", "reviewer_missing", "not_reviewer")
        allowed = facts.repository in condition["repository_ids"]
        return ("passed", "passed", "matched") if allowed else ("failed", "repository_not_allowed", "repository_denied")
    # Every linked requesting event must independently satisfy its local conditions;
    # no union of sender IDs or mentions across unrelated events is an authority.
    linked = [source for source in candidate.sources if candidate.target and candidate.target.mr_url in source.target_urls]
    if kind == "addressed_to_operator":
        allowed = bool(linked) and all(source.operator_id and (
            source.dm_recipient_id == source.operator_id or source.operator_id in source.mention_ids
        ) for source in linked)
        return ("passed", "passed", "matched") if allowed else ("failed", "not_addressed", "not_addressed")
    if kind == "sender_in_allowlist":
        allowed = bool(linked) and all(source.sender_id in condition["sender_ids"] for source in linked)
        return ("passed", "passed", "matched") if allowed else ("failed", "sender_not_allowed", "sender_denied")
    return "unavailable", "cannot_decide", "cannot_decide"


def evaluate(candidate: PreparedCandidate | Mapping[str, Any], ruleset: Sequence[Any] | Mapping[str, Any],
             facts: VerificationResult | Mapping[str, Any] | None = None) -> RuleDecision:
    """The first matching AND rule decides; unavailable stops the entire search."""
    if isinstance(candidate, Mapping):
        candidate = PreparedCandidate.from_mapping(candidate)
    facts = facts or candidate.first_fact
    if isinstance(facts, Mapping):
        facts = fact_from_mapping(facts)
    conditions: list[dict[str, Any]] = []
    attempts: list[dict[str, Any]] = []

    def fallback(reason: str) -> RuleDecision:
        return RuleDecision("push_for_confirmation", reason, conditions=tuple(conditions), attempts=tuple(attempts))

    if candidate.state != "pending":
        return fallback("source_revoked")
    if candidate.flagged:
        return fallback("flagged")
    if candidate.reason and candidate.reason != "adapter_missing":
        return fallback(candidate.reason)
    if isinstance(ruleset, Mapping):
        ruleset = ruleset.get("rules", [])
    normalized = [rule.to_dict() if hasattr(rule, "to_dict") else dict(rule) for rule in ruleset]
    final_reason = "no_match"
    for rule in sorted(normalized, key=lambda value: value["position"]):
        if not rule.get("enabled") or rule.get("id") == "fallback":
            continue
        attempt = {"rule_id": rule["id"], "rule_version": rule["version"], "rule_position": rule["position"]}
        if rule["intent_type"] not in {"any", candidate.type_key}:
            attempts.append({**attempt, "result": "failed", "reason": "type_mismatch"})
            final_reason = "type_mismatch"
            continue
        scope = rule["scope"]
        if scope["kind"] == "named_chats":
            allowed = {(entry["source_id"], entry["chat_id"]) for entry in scope["chats"]}
            if not candidate.sources or any((source.source_id, source.chat_id) not in allowed for source in candidate.sources):
                attempts.append({**attempt, "result": "failed", "reason": "scope_mismatch"})
                final_reason = "scope_mismatch"
                continue
        elif scope["kind"] != "all":
            return fallback("cannot_decide")
        passed = True
        for position, condition in enumerate(rule["conditions"], start=1):
            result, reason, outcome = _condition(condition["kind"], condition, candidate, facts)
            conditions.append({**attempt, "condition_position": position, "kind": condition["kind"],
                               "result": result, "reason": reason,
                               "verified_at": facts.verified_at if condition["kind"] in {
                                   "internal_mr_open", "operator_is_reviewer", "mr_repository_in_allowlist"} else None})
            if result == "unavailable":
                attempts.append({**attempt, "result": "unavailable", "reason": outcome})
                return fallback(outcome)
            if result == "failed":
                attempts.append({**attempt, "result": "failed", "reason": outcome})
                final_reason, passed = outcome, False
                break
        if passed:
            if rule["action"] == "create_task" and not candidate.sources and not any(
                item["kind"] == "operator_is_reviewer" for item in rule["conditions"]
            ):
                attempts.append({**attempt, "result": "failed", "reason": "not_reviewer"})
                final_reason = "not_reviewer"
                continue
            if rule["action"] == "create_task" and (candidate.target is None or not isinstance(facts, VerifiedFact)
                                                      or not facts.exists or facts.state != "open"):
                return fallback("ambiguous_target")
            attempts.append({**attempt, "result": "passed", "reason": "matched"})
            return RuleDecision(rule["action"], "matched", "matched", rule["id"], rule["version"],
                                rule.get("project_id"), rule.get("template_key"), tuple(conditions), tuple(attempts))
    return fallback(final_reason)
