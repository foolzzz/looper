import { useCallback, useEffect, useId, useRef, useState } from "react";
import { Button } from "@/components/ui/button";
import { FormFooter, LABEL, Row, SELECT } from "@/components/registry-ui";
import type { ManagerInfo } from "@/lib/api";
import { deleteChatBinding, fetchChannelChats, fetchChatBindings, putChatBinding,
  type Binding, type ChannelChats } from "@/lib/chat-bindings-api";
import { errorText } from "@/lib/use-action";
import { V1Error } from "@/lib/v1";

const bindingError = (err: unknown) => err instanceof V1Error ? `${err.code}: ${err.message}` : errorText(err);

/** One section-wide snapshot; a write invalidates every earlier read before it starts. */
export function useManagerChatBindings() {
  const [bindings, setBindings] = useState<Binding[] | null>(null);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const [loading, setLoading] = useState(false);
  const request = useRef<AbortController | null>(null);
  const version = useRef(0);
  const writing = useRef(false);
  const mounted = useRef(true);
  const invalidate = useCallback(() => {
    version.current++;
    request.current?.abort();
  }, []);
  useEffect(() => {
    mounted.current = true;
    return () => { mounted.current = false; invalidate(); };
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
  const refresh = useCallback(async () => {
    if (!writing.current) await read().catch(() => {});
  }, [read]);
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
  return { bindings, loaded: bindings !== null, error, busy, loading, refresh, mutate, deleted };
}

type Snapshot = Omit<ReturnType<typeof useManagerChatBindings>, "bindings">;

export function ManagerChatBindings({ manager, managers, bindings, snapshot, reload }: {
  manager: ManagerInfo; managers: ManagerInfo[]; bindings: Binding[];
  snapshot: Snapshot; reload: () => Promise<void>;
}) {
  const id = useId();
  return (
    <section role="region" aria-label={`Chats for ${manager.name}`} className="mt-2 rounded-md border border-border">
      <h4 id={id} tabIndex={-1} className="px-3 py-2 text-sm font-medium">Chats</h4>
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
  const [target, setTarget] = useState(b.manager_id);
  const [policy, setPolicy] = useState<ChannelChats | null>(null);
  const [preparing, setPreparing] = useState(false);
  const [error, setError] = useState("");
  const active = useRef(true);
  const request = useRef<AbortController | null>(null);
  useEffect(() => {
    active.current = true;
    return () => { active.current = false; request.current?.abort(); };
  }, []);
  const busy = snapshot.busy || preparing;
  const name = b.chat_name || b.chat_id;
  const options = managers.filter((m) => !snapshot.deleted.has(m.id));
  const run = async (write: () => Promise<unknown>) => {
    setError("");
    try {
      await snapshot.mutate(write);
      onRemoved();
    } catch (err) {
      if (active.current) setError(bindingError(err));
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
  const fallback = policy && managers.find((m) => m.id === policy.bot_manager_id);
  return <Row title={<span className="break-all text-sm font-medium">{name}</span>}
    actions={<Button size="sm" variant="outline" disabled={busy} onClick={() => void prepareUnbind()}>Unbind</Button>}
    below={<div className="space-y-2">
      <label htmlFor={id} className={LABEL}>Move to Manager</label>
      <select id={id} aria-label={`Move ${name} to Manager`} className={SELECT} value={target}
        disabled={busy} onChange={(e) => setTarget(e.target.value)}>
        {!options.some((m) => m.id === target) && <option value={target} disabled>Unavailable Manager</option>}
        {options.map((m) => <option key={m.id} value={m.id}>{m.name}{!m.enabled ? " · Off" : ""}</option>)}
      </select>
      <Button size="sm" variant="outline" disabled={busy || target === b.manager_id || !options.some((m) => m.id === target)}
        onClick={() => void run(() => putChatBinding(b.channel_id, b.chat_id, target))}>Move</Button>
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
    <p className="break-all text-xs text-muted-foreground">Chat ID: {b.chat_id} · Bound to {b.manager_name}{b.manager_deleted ? " · Deleted" : !b.manager_enabled ? " · Off" : ""}</p>
  </Row>;
}
