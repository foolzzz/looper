import { useCallback, useEffect, useId, useRef, useState } from "react";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { FormFooter, LABEL, Row, SELECT } from "@/components/registry-ui";
import type { ManagerInfo } from "@/lib/api";
import { ChatBindingConfirmation, confirmChatBinding, deleteChatBinding, deleteOverride, fetchChannelChats, fetchChatBindings,
  listCapabilitySets, listOverrides, putChatBinding, putDefaultScope, putOverride,
  type Binding, type CapabilitySet, type ChannelChats, type ScopeOverride, type SenderPolicy } from "@/lib/chat-bindings-api";
import { errorText } from "@/lib/use-action";
import { V1Error, type V1PendingConfirmation } from "@/lib/v1";

const bindingError = (err: unknown) => err instanceof V1Error ? `${err.code}: ${err.message}` : errorText(err);

/** A catalog or override read. Empty is a successful read with nothing to offer, distinct from still loading or failed. */
export type RemoteRead<T> =
  | { status: "loading" }
  | { status: "ready"; rows: T[] }
  | { status: "empty" }
  | { status: "failed"; message: string };

function settled<T>(rows: T[]): RemoteRead<T> {
  return rows.length ? { status: "ready", rows } : { status: "empty" };
}

/** One section-wide snapshot; a write invalidates every earlier read before it starts. */
export function useManagerChatBindings() {
  const [bindings, setBindings] = useState<Binding[] | null>(null);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const [loading, setLoading] = useState(false);
  const [setsRead, setSetsRead] = useState<RemoteRead<CapabilitySet>>({ status: "loading" });
  const [catalogTick, setCatalogTick] = useState(0);
  const request = useRef<AbortController | null>(null);
  const setsRequest = useRef<AbortController | null>(null);
  const setsAttempt = useRef(0);
  const version = useRef(0);
  const writing = useRef(false);
  const mounted = useRef(true);
  const invalidate = useCallback(() => {
    version.current++;
    request.current?.abort();
  }, []);
  useEffect(() => {
    mounted.current = true;
    return () => { mounted.current = false; invalidate(); setsRequest.current?.abort(); };
  }, [invalidate]);

  const read = useCallback(async () => {
    invalidate();
    const current = version.current;
    const controller = new AbortController();
    request.current = controller;
    setLoading(true);
    try {
      const rows = await fetchChatBindings({}, controller.signal);
      if (!mounted.current || current !== version.current) return false;
      setBindings(rows);
      setError("");
      return true;
    } catch (err) {
      if (!mounted.current || current !== version.current) return false;
      setError(bindingError(err));
      throw err;
    } finally {
      if (mounted.current && current === version.current) setLoading(false);
    }
  }, [invalidate]);
  const readSets = useCallback(async () => {
    setsRequest.current?.abort();
    const controller = new AbortController();
    setsRequest.current = controller;
    const attempt = ++setsAttempt.current;
    setSetsRead({ status: "loading" });
    try {
      const rows = await listCapabilitySets(controller.signal);
      if (!mounted.current || attempt !== setsAttempt.current) return;
      setSetsRead(settled(rows));
    } catch (err) {
      if (!mounted.current || attempt !== setsAttempt.current || controller.signal.aborted) return;
      setSetsRead({ status: "failed", message: bindingError(err) });
    }
  }, []);
  const retrySets = useCallback(() => readSets(), [readSets]);
  const refresh = useCallback(async () => {
    if (!writing.current) {
      setCatalogTick((n) => n + 1);
      await Promise.all([read().catch(() => {}), readSets()]);
    }
  }, [read, readSets]);
  const mutate = useCallback(async (write: () => Promise<unknown>) => {
    if (writing.current) return;
    writing.current = true;
    invalidate();
    setBusy(true);
    setLoading(false);
    try {
      await write();
      if (!mounted.current) return;
      // Keep the last confirmed snapshot until server readback succeeds.
      try {
        await read();
      } catch (err) {
        const message = `Change saved; readback failed. ${errorText(err)}`;
        throw err instanceof V1Error ? new V1Error(err.status, err.code, message, err.details) : new Error(message);
      }
    } finally {
      writing.current = false;
      if (mounted.current) setBusy(false);
    }
  }, [invalidate, read]);
  const deleted = new Set(bindings?.filter((b) => b.manager_deleted).map((b) => b.manager_id));
  return { bindings, loaded: bindings !== null, error, busy, loading, refresh, mutate, deleted,
    setsRead, retrySets, catalogTick };
}

type Snapshot = Omit<ReturnType<typeof useManagerChatBindings>, "bindings">;

export function ManagerChatBindings({ manager, managers, bindings, snapshot, reload }: {
  manager: ManagerInfo; managers: ManagerInfo[]; bindings: Binding[];
  snapshot: Snapshot; reload: () => Promise<void>;
}) {
  const id = useId();
  const sets = snapshot.setsRead;
  return (
    <section role="region" aria-label={`Chats for ${manager.name}`} className="mt-2 rounded-md border border-border">
      <h4 id={id} tabIndex={-1} className="px-3 py-2 text-sm font-medium">Chats</h4>
      <p className="px-3 pb-2 text-[11px] text-muted-foreground">Scope applies once scoped routing is on for this Manager.</p>
      {bindings.length > 0 && sets.status === "loading" && (
        <p role="status" className="px-3 pb-2 text-xs text-muted-foreground">Loading capability sets…</p>
      )}
      {bindings.length > 0 && sets.status === "empty" && (
        <p className="px-3 pb-2 text-xs text-muted-foreground">No capability sets.</p>
      )}
      {bindings.length > 0 && sets.status === "failed" && (
        <div role="alert" className="flex flex-wrap items-center gap-2 px-3 pb-2">
          <p className="text-xs text-destructive">Capability sets could not be loaded. {sets.message}</p>
          <Button type="button" size="sm" variant="outline" onClick={() => void snapshot.retrySets()}>Retry capability sets</Button>
        </div>
      )}
      {bindings.length ? <ul aria-labelledby={id} className="divide-y divide-border border-t border-border">
        {bindings.map((b) => <ChatBindingRow key={JSON.stringify([b.channel_id, b.chat_id])}
          binding={b} managers={managers} snapshot={snapshot} reload={reload}
          onRemoved={() => document.getElementById(id)?.focus()} />)}
      </ul> : <p className="px-3 pb-3 text-xs text-muted-foreground">
        {!snapshot.loaded ? "Explicit chat bindings have not loaded." : "No explicit chat bindings."}
      </p>}
    </section>
  );
}

function ChatBindingRow({ binding: b, managers, snapshot, reload, onRemoved }: {
  binding: Binding; managers: ManagerInfo[]; snapshot: Snapshot;
  reload: () => Promise<void>; onRemoved: () => void;
}) {
  const id = useId();
  const senderId = useId();
  const senderPolicyId = useId();
  const scopeId = useId();
  const overrideKindId = useId();
  const overrideScopeId = useId();
  const [target, setTarget] = useState(b.manager_id);
  const [senderPolicy, setSenderPolicy] = useState<SenderPolicy>(b.sender_policy);
  const [confirmation, setConfirmation] = useState<V1PendingConfirmation | null>(null);
  useEffect(() => { setSenderPolicy(b.sender_policy); }, [b.sender_policy]);
  const [senders, setSenders] = useState(b.allowed_senders.join(", "));
  useEffect(() => { setSenders(b.allowed_senders.join(", ")); }, [b.allowed_senders]);
  const ids = senders.split(/[,\s]+/).map((value) => value.trim()).filter(Boolean);
  const sendersUnchanged = ids.length === b.allowed_senders.length
    && ids.every((value, index) => value === b.allowed_senders[index]);
  const [policy, setPolicy] = useState<ChannelChats | null>(null);
  const [preparing, setPreparing] = useState(false);
  const [error, setError] = useState("");
  const [scopeValue, setScopeValue] = useState(b.default_scope ?? "");
  const [scopeAsk, setScopeAsk] = useState<{ scope: string | null } | null>(null);
  const [stopNow, setStopNow] = useState(false);
  const [scopeConfirmation, setScopeConfirmation] = useState<V1PendingConfirmation | null>(null);
  const [overrides, setOverrides] = useState<RemoteRead<ScopeOverride>>({ status: "loading" });
  const [overrideAttempt, setOverrideAttempt] = useState(0);
  const [kind, setKind] = useState<"person" | "bot">("person");
  const [primaryId, setPrimaryId] = useState("");
  const [secondaryId, setSecondaryId] = useState("");
  const [overrideScope, setOverrideScope] = useState("");
  const [overrideError, setOverrideError] = useState("");
  const [overrideConfirm, setOverrideConfirm] = useState<V1PendingConfirmation | null>(null);
  const overrideGeneration = useRef(0);
  useEffect(() => { setScopeValue(b.default_scope ?? ""); }, [b.default_scope]);
  const active = useRef(true);
  const request = useRef<AbortController | null>(null);
  useEffect(() => {
    active.current = true;
    return () => { active.current = false; request.current?.abort(); overrideGeneration.current++; };
  }, []);
  useEffect(() => {
    const current = ++overrideGeneration.current;
    const controller = new AbortController();
    setOverrides({ status: "loading" });
    listOverrides(b, controller.signal).then((rows) => {
      if (active.current && current === overrideGeneration.current) setOverrides(settled(rows));
    }).catch((err: unknown) => {
      if (!active.current || current !== overrideGeneration.current || controller.signal.aborted) return;
      setOverrides({ status: "failed", message: bindingError(err) });
    });
    return () => { controller.abort(); overrideGeneration.current++; };
  }, [b, snapshot.catalogTick, overrideAttempt]);
  const busy = snapshot.busy || preparing;
  const name = b.chat_name || b.chat_id;
  const options = managers.filter((m) => !snapshot.deleted.has(m.id));
  const run = async (write: () => Promise<unknown>) => {
    setError("");
    setConfirmation(null);
    try {
      await snapshot.mutate(write);
      onRemoved();
    } catch (err) {
      if (active.current && err instanceof ChatBindingConfirmation) setConfirmation(err.confirmation);
      else if (active.current) setError(bindingError(err));
      if (err instanceof V1Error && err.code === "not_found") await reload();
    }
  };
  const prepareUnbind = async () => {
    setPreparing(true);
    setPolicy(null);
    setError("");
    const controller = new AbortController();
    request.current = controller;
    try {
      const result = await fetchChannelChats(b.channel_id, { limit: 1 }, controller.signal);
      if (active.current) setPolicy(result);
    } catch (err) {
      if (active.current) setError(bindingError(err));
    } finally {
      if (active.current) setPreparing(false);
    }
  };
  const setsReady = snapshot.setsRead.status === "ready";
  const catalog = snapshot.setsRead.status === "ready" ? snapshot.setsRead.rows : [];
  const groupScopes = catalog.filter((set) => set.id !== "full" && (set.id !== "legacy" || b.legacy_allowed));
  const overrideScopes = catalog.filter((set) => set.id !== "full" && set.id !== "legacy");
  const scopeOffered = setsReady && (scopeValue === "" || groupScopes.some((set) => set.id === scopeValue));
  const overridePicked = overrideScopes.some((set) => set.id === overrideScope);
  const overrideCount = overrides.status === "ready" ? overrides.rows.length : overrides.status === "empty" ? 0 : null;
  const openScope = (scope: string | null) => {
    setStopNow(false); setScopeAsk({ scope }); setScopeConfirmation(null); setError("");
  };
  const wrote = async (write: () => Promise<unknown>, done: () => Promise<void> | void,
    setPending: (value: V1PendingConfirmation | null) => void, setLocal: (value: string) => void) => {
    setLocal("");
    try {
      await snapshot.mutate(write);
      if (active.current) await done();
    } catch (err) {
      if (!active.current) return;
      if (err instanceof ChatBindingConfirmation) setPending(err.confirmation);
      else setLocal(bindingError(err));
    }
  };
  const reloadOverrides = async () => {
    const current = ++overrideGeneration.current;
    try {
      const rows = await listOverrides(b);
      if (active.current && current === overrideGeneration.current) setOverrides(settled(rows));
    } catch (err) {
      if (active.current && current === overrideGeneration.current) {
        setOverrides({ status: "failed", message: bindingError(err) });
      }
      throw err;
    }
  };
  const saveScope = () => scopeAsk && wrote(() => putDefaultScope(b, scopeAsk.scope, stopNow), () => setScopeAsk(null),
    (pending) => { setScopeAsk(null); setScopeConfirmation(pending); }, setError);
  const confirmSavedScope = () => scopeConfirmation && wrote(() => confirmChatBinding(scopeConfirmation),
    () => setScopeConfirmation(null), setScopeConfirmation, setError);
  const addOverride = () => {
    if (!overridePicked) return;
    const senderIds = [primaryId.trim(), ...(kind === "bot" ? [secondaryId.trim()] : [])].filter(Boolean);
    setOverrideConfirm(null);
    return wrote(() => putOverride(b, { principal_kind: kind, sender_ids: senderIds, scope_id: overrideScope, stop_now: false }),
      async () => { setPrimaryId(""); setSecondaryId(""); await reloadOverrides(); }, setOverrideConfirm, setOverrideError);
  };
  const removeOverride = (principalId: string) => {
    setOverrideConfirm(null);
    return wrote(() => deleteOverride(b, principalId, false), reloadOverrides, setOverrideConfirm, setOverrideError);
  };
  const confirmOverride = () => overrideConfirm && wrote(() => confirmChatBinding(overrideConfirm), async () => {
    setOverrideConfirm(null); setPrimaryId(""); setSecondaryId(""); await reloadOverrides();
  }, setOverrideConfirm, setOverrideError);
  const fallback = policy && managers.find((m) => m.id === policy.bot_manager_id);
  return <Row title={<span className="break-all text-sm font-medium">{name}</span>}
    actions={<Button size="sm" variant="outline" disabled={busy} onClick={() => void prepareUnbind()}>Unbind</Button>}
    below={<div className="space-y-2">
      <label htmlFor={id} className={LABEL}>Move to Manager</label>
      <select id={id} aria-label={`Move ${name} to Manager`} className={SELECT} value={target}
        disabled={busy} onChange={(e) => { setTarget(e.target.value); setConfirmation(null); }}>
        {!options.some((m) => m.id === target) && <option value={target} disabled>Unavailable Manager</option>}
        {options.map((m) => <option key={m.id} value={m.id}>{m.name}{!m.enabled ? " · Off" : ""}</option>)}
      </select>
      <div className="flex flex-wrap items-center gap-2">
        <Button size="sm" variant="outline" disabled={busy || target === b.manager_id || !options.some((m) => m.id === target)}
          onClick={() => void run(() => putChatBinding(b.channel_id, b.chat_id, target))}>Move</Button>
        {target !== b.manager_id && <p className="text-xs text-muted-foreground">Moving clears this chat's extra senders and resets its policy to listed senders.</p>}
      </div>
      <div className="space-y-2">
        <label htmlFor={senderId} className={LABEL}>Extra senders in this chat</label>
        <Input id={senderId} aria-label={`Extra senders for ${name}`} aria-describedby={`${senderId}-help`}
          value={senders} disabled={busy} onChange={(event) => { setSenders(event.target.value); setConfirmation(null); }}
          placeholder={b.channel_type === "telegram" ? "123456789, …" : "ou_…, cli_…"} />
        <p id={`${senderId}-help`} className="text-[11px] text-muted-foreground">
          Allowed here besides the bot's allowed senders, in this chat only: a direct message or another chat still refuses them.
          {b.channel_type === "telegram" ? " Telegram user IDs, comma-separated." : " Lark user open_ids (ou_…) or bot app IDs (cli_…), comma-separated."}
        </p>
        {b.channel_type !== "telegram" && <>
          <label htmlFor={senderPolicyId} className={LABEL}>Who can reach this Manager</label>
          <select id={senderPolicyId} aria-label={`Sender policy for ${name}`} aria-describedby={`${senderPolicyId}-help`}
            className={SELECT} value={senderPolicy} disabled={busy}
            onChange={(event) => { setSenderPolicy(event.target.value as SenderPolicy); setConfirmation(null); }}>
            <option value="listed">Listed senders</option>
            <option value="chat">Everyone in this chat</option>
          </select>
          <p id={`${senderPolicyId}-help`} className="text-[11px] text-muted-foreground">
            {senderPolicy === "chat"
              ? "Anyone who can post in this chat, including other bots, can reach this Manager by mentioning the bot; the bot itself is excluded."
              : "Only the bot's allowed senders and this chat's extra senders can reach this Manager."}
          </p>
        </>}
        <Button size="sm" variant="outline" disabled={busy || (sendersUnchanged && senderPolicy === b.sender_policy)}
          onClick={() => void run(() => b.channel_type === "telegram"
            ? putChatBinding(b.channel_id, b.chat_id, b.manager_id, ids)
            : putChatBinding(b.channel_id, b.chat_id, b.manager_id, ids, senderPolicy))}>Save</Button>
        {confirmation && <div className="space-y-2">
          <p className="text-xs text-warning">{confirmation.impact}</p>
          <FormFooter error="" busy={busy} submit="Open to everyone" onCancel={() => setConfirmation(null)}
            onSubmit={() => void run(() => confirmChatBinding(confirmation))} />
        </div>}
      </div>
      <div className="space-y-2">
        <label htmlFor={scopeId} className={LABEL}>Group default scope</label>
        <select id={scopeId} aria-label={`Scope for ${name}`} className={SELECT} value={scopeValue} disabled={busy || !setsReady}
          onChange={(event) => { setScopeValue(event.target.value); setScopeAsk(null); setScopeConfirmation(null); }}>
          <option value="">System default (chat only)</option>
          {scopeValue !== "" && !groupScopes.some((set) => set.id === scopeValue) && (
            <option value={scopeValue} disabled>{scopeValue}</option>
          )}
          {groupScopes.map((set) => <option key={set.id} value={set.id}>{set.name}</option>)}
        </select>
        <Button size="sm" variant="outline" disabled={busy || !scopeOffered || scopeValue === (b.default_scope ?? "")}
          onClick={() => openScope(scopeValue === "" ? null : scopeValue)}>Save scope</Button>
        {scopeAsk && <div className="space-y-2">
          <label className="flex items-center gap-2 text-xs pointer-coarse:min-h-11">
            <input type="checkbox" className="h-4 w-4 accent-primary pointer-coarse:h-11 pointer-coarse:w-11"
              aria-label="Apply now, interrupting a running reply" checked={stopNow} disabled={busy}
              onChange={(event) => setStopNow(event.target.checked)} />
            Apply now, interrupting a running reply
          </label>
          <FormFooter error="" busy={busy} submit="Confirm scope" onCancel={() => setScopeAsk(null)} onSubmit={() => void saveScope()} />
        </div>}
        {scopeConfirmation && <div className="space-y-2">
          <p className="text-xs text-warning">{scopeConfirmation.impact}</p>
          <FormFooter error="" busy={busy} submit="Confirm" onCancel={() => setScopeConfirmation(null)} onSubmit={() => void confirmSavedScope()} />
        </div>}
        {b.default_scope === "legacy" && <div className="space-y-2">
          <p className="text-xs text-warning">Legacy: everyone admitted here shares a session that can run commands with this machine's account.</p>
          {b.suggested_scope && <Button size="sm" variant="outline" disabled={busy} onClick={() => openScope(b.suggested_scope)}>Adopt {b.suggested_scope}</Button>}
        </div>}
        <div className="space-y-2">
          <p className={LABEL}>Scope overrides</p>
          {overrides.status === "loading" && <p role="status" className="text-xs text-muted-foreground">Loading scope overrides…</p>}
          {overrides.status === "empty" && <p className="text-xs text-muted-foreground">No scope overrides.</p>}
          {overrides.status === "failed" && (
            <div role="alert" className="flex flex-wrap items-center gap-2">
              <p className="text-xs text-destructive">Scope overrides could not be loaded. {overrides.message}</p>
              <Button type="button" size="sm" variant="outline" onClick={() => setOverrideAttempt((n) => n + 1)}>Retry scope overrides</Button>
            </div>
          )}
          {overrides.status === "ready" && <ul className="space-y-1">
            {overrides.rows.map((row) => <li key={row.principal_id} className="flex flex-wrap items-center gap-2 text-xs">
              <span>{row.principal_kind}</span>
              <span className="break-all">{row.sender_ids.join(", ")}</span>
              <span>{row.scope_id}</span>
              <Button size="sm" variant="outline" disabled={busy} aria-label={`Remove override ${row.principal_id}`}
                onClick={() => void removeOverride(row.principal_id)}>Remove</Button>
            </li>)}
          </ul>}
          <div className="flex flex-wrap items-center gap-2">
            <label htmlFor={overrideKindId} className="sr-only">Override kind</label>
            <select id={overrideKindId} aria-label={`Override kind for ${name}`} className={SELECT} value={kind} disabled={busy}
              onChange={(event) => setKind(event.target.value as "person" | "bot")}>
              <option value="person">Person</option>
              <option value="bot">Bot</option>
            </select>
            <Input aria-label={`Override id for ${name}`} value={primaryId} disabled={busy} placeholder="id"
              onChange={(event) => setPrimaryId(event.target.value)} />
            {kind === "bot" && <Input aria-label={`Override second id for ${name}`} value={secondaryId} disabled={busy}
              placeholder="second id" onChange={(event) => setSecondaryId(event.target.value)} />}
            <label htmlFor={overrideScopeId} className="sr-only">Override scope</label>
            <select id={overrideScopeId} aria-label={`Override scope for ${name}`} className={SELECT} value={overrideScope}
              disabled={busy || !setsReady} onChange={(event) => setOverrideScope(event.target.value)}>
              <option value="">Choose a scope</option>
              {overrideScopes.map((set) => <option key={set.id} value={set.id}>{set.name}</option>)}
            </select>
            <Button size="sm" variant="outline" disabled={busy || !primaryId.trim() || !overridePicked}
              onClick={() => void addOverride()}>Add override</Button>
          </div>
          {overrideConfirm && <div className="space-y-2">
            <p className="text-xs text-warning">{overrideConfirm.impact}</p>
            <FormFooter error="" busy={busy} submit="Confirm override" onCancel={() => setOverrideConfirm(null)}
              onSubmit={() => void confirmOverride()} />
          </div>}
          {overrideError && <p role="alert" className="break-words text-xs text-destructive">{overrideError}</p>}
        </div>
      </div>
      {policy && <div className="space-y-2">
        <p className="text-xs text-muted-foreground">{policy.bound_chats_only
          ? "After unbinding, this bot will stay silent in this chat (bound chats only)."
          : `After unbinding, this chat returns to bot default: ${fallback?.name ?? policy.bot_manager_id}${fallback && !fallback.enabled ? " · Off" : ""}.`}</p>
        <FormFooter error="" busy={busy} submit="Confirm unbind" onCancel={() => setPolicy(null)}
          onSubmit={() => void run(() => deleteChatBinding(b.channel_id, b.chat_id))} />
      </div>}
      {error && <p role="alert" className="break-words text-xs text-destructive">{error} Retry the action or refresh chats.</p>}
    </div>}>
    <p className="break-all text-xs text-muted-foreground">
      {{ telegram: "Telegram", lark: "Lark", lark_bot: "Lark Bot" }[b.channel_type]} · {b.bot_name} · {b.channel_id}
    </p>
    <p className="break-all text-xs text-muted-foreground">Chat ID: {b.chat_id} · Bound to {b.manager_name}{b.manager_deleted ? " · Deleted" : !b.manager_enabled ? " · Off" : ""}{b.allowed_senders.length > 0 ? ` · ${b.allowed_senders.length} extra sender${b.allowed_senders.length === 1 ? "" : "s"}` : ""}{b.sender_policy === "chat" ? " · Everyone in this chat" : ""}{` · Scope: ${b.effective_default_scope}`}{overrideCount != null ? ` · ${overrideCount} override${overrideCount === 1 ? "" : "s"}` : ""}</p>
  </Row>;
}
