import { useEffect, useId, useRef, useState } from "react";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Switch } from "@/components/ui/switch";
import { LABEL, SELECT } from "@/components/registry-ui";
import { fetchManagers, type ManagerInfo } from "@/lib/api";
import {
  deleteChatBinding, fetchChannelChats, fetchChatBindings, putChatBinding, setBoundChatsOnly,
  type Binding, type ChannelChats, type SeenChat,
} from "@/lib/chat-bindings-api";

type Props = { channelId: string; botManagerId: string; botManagerName?: string };
type Snapshot = { pages: ChannelChats[]; bindings: Binding[]; managers: ManagerInfo[] };

function failure(error: unknown): string {
  const code = error && typeof error === "object" && "code" in error ? `${error.code}: ` : "";
  return code + (error instanceof Error ? error.message : "The request failed.");
}

function time(value: string | null): string {
  return value ? new Date(value).toLocaleString() : "Unknown";
}

// Key the entire state lifetime by bot identity, including outstanding writes.
export function ChannelChatBindings(props: Props) {
  return <ChatBindings key={props.channelId} {...props} />;
}

function ChatBindings({ channelId, botManagerId, botManagerName }: Props) {
  const id = useId();
  const [snapshot, setSnapshot] = useState<Snapshot | null>(null);
  const [loading, setLoading] = useState(true);
  const [loadError, setLoadError] = useState("");
  const [errors, setErrors] = useState<Record<string, string>>({});
  const [pending, setPending] = useState<Set<string>>(new Set());
  const alive = useRef(false);
  const generation = useRef(0);
  const controller = useRef<AbortController | null>(null);
  const cursors = useRef<(string | undefined)[]>([undefined]);
  const writes = useRef(new Set<string>());

  const invalidate = () => {
    generation.current += 1;
    controller.current?.abort();
  };

  async function load(after = cursors.current) {
    invalidate();
    const current = generation.current;
    const abort = new AbortController();
    controller.current = abort;
    setLoading(true);
    setLoadError("");
    try {
      const [pages, bindings, managers] = await Promise.all([
        Promise.all(after.map((cursor) => fetchChannelChats(channelId, { limit: 20, ...(cursor ? { after: cursor } : {}) }, abort.signal))),
        fetchChatBindings({ channel_id: channelId }, abort.signal),
        fetchManagers(),
      ]);
      if (!alive.current || current !== generation.current) return;
      cursors.current = after;
      setSnapshot({ pages, bindings, managers });
    } catch (error) {
      if (!alive.current || current !== generation.current) return;
      setLoadError(failure(error));
    } finally {
      if (alive.current && current === generation.current) setLoading(false);
    }
  }

  useEffect(() => {
    alive.current = true;
    void load();
    return () => { alive.current = false; invalidate(); };
    // The keyed parent gives each channel its own lifetime.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  async function mutate(key: string, action: () => Promise<unknown>) {
    if (writes.current.has(key)) return;
    writes.current.add(key);
    setPending(new Set(writes.current));
    setErrors((old) => ({ ...old, [key]: "" }));
    invalidate();
    setLoading(false);
    try {
      await action();
      if (alive.current) await load();
    } catch (error) {
      if (!alive.current) return;
      setErrors((old) => ({ ...old, [key]: failure(error) }));
      // A target may have been deleted since the list was loaded. Refresh the
      // choices without turning the refused write into a successful selection.
      if (error && typeof error === "object" && "status" in error && error.status === 404) {
        const current = generation.current;
        try {
          const managers = await fetchManagers();
          if (alive.current && current === generation.current) setSnapshot((old) => old && ({ ...old, managers }));
        } catch (refreshError) {
          if (alive.current && current === generation.current) setLoadError(failure(refreshError));
        }
      }
    } finally {
      if (alive.current) {
        writes.current.delete(key);
        setPending(new Set(writes.current));
      }
    }
  }

  const policy = snapshot?.pages[0].bound_chats_only ?? false;
  const lastPage = snapshot?.pages.at(-1);
  const bindings = new Map(snapshot?.bindings.map((binding) => [binding.chat_id, binding]));
  const suppressed = new Map(snapshot?.pages[0].unbound_chats.slice(0, 20).map((chat) => [chat.chat_id, chat]));
  const chats = new Map<string, SeenChat>();
  for (const page of snapshot?.pages ?? []) for (const chat of page.chats) chats.set(chat.chat_id, chat);
  // Suppression is historical. Resolve its current binding even when the chat
  // falls outside the loaded catalog pages, with one selector per bot/chat pair.
  for (const chat of suppressed.values()) if (!chats.has(chat.chat_id)) {
    const binding = bindings.get(chat.chat_id) ?? null;
    chats.set(chat.chat_id, {
      chat_id: chat.chat_id, name: chat.name, count: null, last_at: null, binding,
      effective_manager_id: binding?.manager_id ?? (policy ? "" : botManagerId),
      effective_manager_name: binding?.manager_name ?? botManagerName ?? botManagerId,
      effective_manager_enabled: binding?.manager_enabled ?? snapshot?.managers.find((m) => m.id === botManagerId)?.enabled ?? true,
      effective_manager_deleted: binding?.manager_deleted ?? false,
    });
  }

  return (
    <section aria-label="Chat bindings" className="space-y-3 border-t border-border pt-4">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <h4 className="text-sm font-semibold">Chat bindings</h4>
        <Button type="button" variant="outline" size="sm" disabled={loading} onClick={() => void load([undefined])}>Refresh chats</Button>
      </div>
      <p className="text-xs text-muted-foreground">
        Chat observation starts after this upgrade; earlier chats may be missing. Bindings and Only bound chats apply immediately,
        to future messages. Already admitted turns and tasks keep their Manager. Sender allowlists and Lark group @-mentions still apply.
      </p>
      {loading && <p role="status" className="text-xs text-muted-foreground">Loading chats…</p>}
      {loadError && <p role="alert" className="text-xs text-destructive">{loadError} Refresh chats to retry or confirm server state.</p>}
      {snapshot && <>
        <div className="flex items-center justify-between gap-3">
          <div className="space-y-1">
            <p className="text-xs font-medium">Only bound chats</p>
            <p className="text-xs text-muted-foreground">When on, unbound new messages are silent: no Manager, reply, reaction or status. When off, unbound chats use {botManagerName ?? botManagerId}.</p>
          </div>
          <Switch label="Only bound chats" checked={policy} disabled={pending.has("policy")} onChange={(value) => void mutate("policy", () => setBoundChatsOnly(channelId, value))} />
        </div>
        {errors.policy && <p role="alert" className="text-xs text-destructive">{errors.policy}</p>}
        {!chats.size && <p className="text-xs text-muted-foreground">No chats observed or bound yet.</p>}
        <ul className="divide-y divide-border">
          {[...chats.values()].map((chat) => {
            const key = `${channelId}:${chat.chat_id}`;
            const recent = suppressed.get(chat.chat_id);
            const target = chat.binding;
            const deleted = target?.manager_deleted ?? chat.effective_manager_deleted;
            const off = target ? !target.manager_enabled : !chat.effective_manager_enabled;
            const silent = !target && policy;
            const name = chat.name || chat.chat_id;
            return <li key={key} className="space-y-2 py-3">
              <div className="flex flex-wrap items-center gap-2">
                <span className="break-all text-sm font-medium">{name}</span>
                <Badge variant={target ? "success" : "outline"}>{target ? "Bound" : silent ? "Silenced" : "Bot Manager"}</Badge>
              </div>
              {chat.name && <p className="break-all font-mono text-xs text-muted-foreground">{chat.chat_id}</p>}
              <p className="text-xs text-muted-foreground">Seen: {chat.count ?? "Unknown"} · Last seen: {time(chat.last_at)}</p>
              {recent && <p className="text-xs text-muted-foreground">Recent suppressed: {recent.count} · Last suppressed: {time(recent.last_at)} (history)</p>}
              <p className="text-xs text-muted-foreground">Effective Manager: {silent ? "None — silent" : `${target?.manager_name ?? chat.effective_manager_name}${deleted ? " (unavailable)" : off ? " (off)" : ""}`}</p>
              {!silent && (deleted || off) && <p className="text-xs text-muted-foreground">{deleted ? "The bound Manager is unavailable." : "New messages get the existing turned-off response."} No fallback Manager is used.</p>}
              <label htmlFor={`${id}-${chat.chat_id}`} className={LABEL}>Bind {name} to</label>
              <select id={`${id}-${chat.chat_id}`} className={`${SELECT} focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring`}
                value={target?.manager_id ?? ""} disabled={pending.has(key)}
                onChange={(event) => {
                  const value = event.target.value;
                  void mutate(key, () => value ? putChatBinding(channelId, chat.chat_id, value) : deleteChatBinding(channelId, chat.chat_id));
                }}>
                <option value="">Use this bot's Manager (no binding)</option>
                {target && !snapshot.managers.some((m) => m.id === target.manager_id) && <option value={target.manager_id} disabled>{target.manager_name} (unavailable)</option>}
                {snapshot.managers.map((manager) => <option key={manager.id} value={manager.id}>{manager.name}{manager.enabled ? "" : " (off)"}</option>)}
              </select>
              {policy && <p className="text-xs text-muted-foreground">Unbinding silences this chat while Only bound chats is on.</p>}
              {errors[key] && <p role="alert" className="text-xs text-destructive">{errors[key]}</p>}
            </li>;
          })}
        </ul>
        {lastPage?.has_more && lastPage.next_cursor && <Button type="button" variant="outline" size="sm" disabled={loading}
          onClick={() => void load([...cursors.current, lastPage.next_cursor!])}>Load more chats</Button>}
      </>}
    </section>
  );
}
