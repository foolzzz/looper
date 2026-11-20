import { useCallback, useEffect, useId, useState } from "react";
import { AlertTriangle, ExternalLink, Loader2, Plus } from "lucide-react";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { LABEL, SELECT } from "@/components/registry-ui";
import {
  actOnLedgerItem, addLedgerItem, fetchLedgerItem, fetchLedgerItems, fetchLedgerState,
  type LedgerAction, type LedgerEvent, type LedgerItem, type LedgerKind, type LedgerState,
} from "@/lib/ledger-api";
import { sinceText } from "@/lib/gates";
import { startPolling } from "@/lib/polling";
import { errorText } from "@/lib/use-action";
import { cn } from "@/lib/utils";

const REFRESH_MS = 15_000;
const RECENT_DECISION_DAYS = 30;
const GROUP_LIMIT = 10;

const KIND_LABEL: Record<LedgerKind, string> = { pending: "To-do", decision: "Decision", work_item: "Work" };
const SOURCE_LABEL = { gate: "Gate", confirmation: "Confirmation", task: "Task" } as const;
const STATUS_LABEL: Record<string, string> = {
  open: "Open", done: "Done", cancelled: "Cancelled", expired: "Expired", superseded: "Superseded",
};
const STATUS_FILTERS = ["open", "done", "cancelled", "expired", "superseded", "all"] as const;

interface View {
  needsYou: LedgerItem[];
  working: LedgerItem[];
  decisions: LedgerItem[];
  flat: LedgerItem[] | null;
  more: number;
}

const unconfirmed = (item: LedgerItem) => item.kind === "decision" && item.assurance !== "user" && item.status === "open";

/** Overdue first, then by due date, then newest. */
function byUrgency(a: LedgerItem, b: LedgerItem) {
  if (a.overdue !== b.overdue) return a.overdue ? -1 : 1;
  const due = (a.due_at ?? "9999").localeCompare(b.due_at ?? "9999");
  return due || b.number - a.number;
}

function dueText(item: LedgerItem): string | null {
  if (!item.due_at) return null;
  if (item.overdue) return `Overdue ${sinceText(item.due_at) || "0 s"}`;
  const days = Math.ceil((Date.parse(item.due_at) - Date.now()) / 86_400_000);
  return days <= 0 ? "Due today" : `Due in ${days} d`;
}

/** An open decision is in force, not "open". */
function statusText(item: LedgerItem): string {
  return item.kind === "decision" && item.status === "open" ? "Active" : STATUS_LABEL[item.status ?? "open"];
}

function sourceText(item: LedgerItem): string {
  return item.source ? SOURCE_LABEL[item.source.kind] : item.added_via === "cli" ? "Command line" : "Manual";
}

function ownerText(item: LedgerItem): string {
  return item.owner === "user" ? "You" : item.owner.startsWith("task:") ? `Task ${item.owner.slice(5)}` : item.owner;
}

function Row({ item, selected, onSelect }: { item: LedgerItem; selected: boolean; onSelect: () => void }) {
  const due = dueText(item);
  const waiting = unconfirmed(item);
  return (
    <li>
      <button
        type="button"
        onClick={onSelect}
        aria-current={selected ? "true" : undefined}
        className={cn(
          "flex w-full min-w-0 flex-col gap-1 rounded-md border px-3 py-2 pointer-coarse:py-3 text-left transition-colors",
          "focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring",
          selected ? "border-primary/40 bg-primary/5" : "border-border hover:bg-accent",
        )}
      >
        <span className="flex min-w-0 items-baseline gap-2">
          <span className="shrink-0 font-mono text-xs text-muted-foreground">{item.id}</span>
          <span className="min-w-0 break-words text-sm font-medium">{item.title}</span>
        </span>
        <span className="flex flex-wrap items-center gap-x-2 gap-y-1 text-xs text-muted-foreground">
          <span>{KIND_LABEL[item.kind]}</span>
          <span aria-hidden="true">·</span>
          <span>{sourceText(item)}</span>
          <span aria-hidden="true">·</span>
          <span>{statusText(item)}</span>
          <span aria-hidden="true">·</span>
          <span>{ownerText(item)}</span>
          {item.project_id && <span>· {item.project_id}</span>}
          {waiting && <Badge variant="warning">Relayed, not confirmed</Badge>}
          {due && <Badge variant={item.overdue ? "destructive" : "outline"}>{due}</Badge>}
        </span>
      </button>
    </li>
  );
}

function Group({ title, items, selected, onSelect, hint }: {
  title: string; items: LedgerItem[]; selected: string | null; onSelect: (id: string) => void; hint?: string;
}) {
  const [all, setAll] = useState(false);
  if (!items.length) return null;
  const shown = all ? items : items.slice(0, GROUP_LIMIT);
  return (
    <section aria-label={title} className="space-y-2">
      <div>
        <h2 className="text-sm font-semibold">{title} <span className="font-normal text-muted-foreground">({items.length})</span></h2>
        {hint && <p className="text-xs text-muted-foreground">{hint}</p>}
      </div>
      <ul className="space-y-2">
        {shown.map((item) => <Row key={item.id} item={item} selected={selected === item.id} onSelect={() => onSelect(item.id)} />)}
      </ul>
      {items.length > GROUP_LIMIT && (
        <Button variant="ghost" size="sm" onClick={() => setAll(!all)}>
          {all ? "Show fewer" : `${items.length - GROUP_LIMIT} more`}
        </Button>
      )}
    </section>
  );
}

function who(event: LedgerEvent): string {
  if (event.actor_kind === "user") return "You";
  if (event.actor_kind === "system") return "System";
  return `Relayed · ${event.relayed_by ?? "someone"}`;
}

function via(event: LedgerEvent): string {
  return event.via === "cli" ? `Command line · ${event.session_label ?? ""}` : event.via;
}

function Detail({ id, onChanged, onClose }: { id: string; onChanged: () => void; onClose: () => void }) {
  const field = useId();
  const [data, setData] = useState<{ item: LedgerItem; events: LedgerEvent[] } | null>(null);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const [note, setNote] = useState("");
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState({ title: "", detail: "", due: "" });

  const load = useCallback((signal?: AbortSignal) => fetchLedgerItem(id, signal)
    .then((value) => { setData(value); setError(""); })
    .catch((err: unknown) => { if (!signal?.aborted) setError(errorText(err)); }), [id]);
  useEffect(() => {
    setData(null);
    const controller = new AbortController();
    void load(controller.signal);
    return () => controller.abort();
  }, [load]);

  const act = async (action: LedgerAction, extra: Parameters<typeof actOnLedgerItem>[2] = {}) => {
    setBusy(true);
    setError("");
    try {
      await actOnLedgerItem(id, action, extra);
      setNote("");
      setEditing(false);
      await load();
      onChanged();
    } catch (err) {
      setError(errorText(err));
    } finally {
      setBusy(false);
    }
  };

  const item = data?.item;
  const own = item?.source === null;
  const open = item?.status === "open";
  const startEdit = () => {
    if (!item) return;
    setDraft({ title: item.title, detail: item.detail, due: item.due_at ? item.due_at.slice(0, 10) : "" });
    setEditing(true);
  };
  const goTo = item?.source
    ? item.source.kind === "task" ? `#tasks/${item.source.ref}` : item.source.kind === "gate" ? "#gates" : null
    : null;

  return (
    <Card className="min-w-0 space-y-4 p-4" role="region" aria-label={`Details of ${id}`}>
      <div className="flex items-start justify-between gap-2">
        <h2 className="min-w-0 break-words text-base font-semibold">{item ? item.title : id}</h2>
        <Button variant="ghost" size="sm" onClick={onClose}>Close</Button>
      </div>
      {error && <p role="alert" className="break-words text-sm text-destructive">{error}</p>}
      {!item ? (
        error ? <Button variant="outline" onClick={() => void load()}>Retry</Button>
          : <p role="status" className="text-sm text-muted-foreground"><Loader2 className="mr-1 inline h-3 w-3 animate-spin" />Loading…</p>
      ) : (
        <>
          <dl className="grid grid-cols-2 gap-x-3 gap-y-2 text-sm">
            <div><dt className={LABEL}>Kind</dt><dd>{KIND_LABEL[item.kind]}</dd></div>
            <div><dt className={LABEL}>Status</dt><dd>{statusText(item)}</dd></div>
            <div><dt className={LABEL}>Source</dt><dd>{sourceText(item)}{item.source?.state ? ` (${item.source.state})` : ""}</dd></div>
            <div><dt className={LABEL}>Owner</dt><dd>{ownerText(item)}</dd></div>
            {item.project_id && <div><dt className={LABEL}>Project</dt><dd className="break-words">{item.project_id}</dd></div>}
            {item.due_at && <div><dt className={LABEL}>Due</dt><dd>{new Date(item.due_at).toLocaleDateString()} · {dueText(item)}</dd></div>}
            {item.superseded_by && <div><dt className={LABEL}>Replaced by</dt><dd>{item.superseded_by}</dd></div>}
          </dl>
          {unconfirmed(item) && (
            <p className="flex gap-2 rounded-md bg-warning/10 p-2 text-xs text-warning">
              <AlertTriangle className="mt-0.5 h-3 w-3 shrink-0" />
              Someone else said this. It stays unconfirmed until you confirm it.
            </p>
          )}
          {item.detail && <p className="whitespace-pre-wrap break-words text-sm text-muted-foreground">{item.detail}</p>}

          <div className="flex flex-wrap gap-2">
            {goTo && <a href={goTo} className="inline-flex h-8 pointer-coarse:h-11 items-center gap-1 rounded-md border border-border px-3 text-xs font-medium hover:bg-accent"><ExternalLink className="h-3 w-3" />Go to {sourceText(item).toLowerCase()}</a>}
            {own && unconfirmed(item) && <Button size="sm" disabled={busy} onClick={() => void act("confirm")}>Confirm</Button>}
            {own && open && item.kind !== "decision" && <Button size="sm" disabled={busy} onClick={() => void act("close")}>Done</Button>}
            {own && open && <Button size="sm" variant="outline" disabled={busy} onClick={() => void act("cancel")}>Cancel it</Button>}
            {own && !open && <Button size="sm" variant="outline" disabled={busy} onClick={() => void act("reopen")}>Reopen</Button>}
            <Button size="sm" variant="outline" disabled={busy || editing} onClick={startEdit}>Edit</Button>
          </div>

          {editing && (
            <form className="space-y-2" onSubmit={(event) => {
              event.preventDefault();
              void act("amend", { fields: {
                ...(own ? { title: draft.title.trim(), detail: draft.detail } : {}),
                due: draft.due ? draft.due : null } });
            }}>
              {own && <>
                <label htmlFor={`${field}-title`} className={LABEL}>Title</label>
                <Input id={`${field}-title`} value={draft.title} maxLength={200} required
                       onChange={(e) => setDraft({ ...draft, title: e.target.value })} />
                <label htmlFor={`${field}-detail`} className={LABEL}>Detail</label>
                <textarea id={`${field}-detail`} value={draft.detail} maxLength={4000} rows={3}
                          onChange={(e) => setDraft({ ...draft, detail: e.target.value })}
                          className="flex w-full rounded-md border border-input bg-transparent px-3 py-2 text-sm pointer-coarse:text-base" />
              </>}
              <label htmlFor={`${field}-due`} className={LABEL}>Due</label>
              <Input id={`${field}-due`} type="date" value={draft.due} onChange={(e) => setDraft({ ...draft, due: e.target.value })} />
              <div className="flex gap-2">
                <Button type="submit" size="sm" disabled={busy}>Save</Button>
                <Button type="button" size="sm" variant="ghost" onClick={() => setEditing(false)}>Cancel</Button>
              </div>
            </form>
          )}

          <form className="flex gap-2" onSubmit={(event) => { event.preventDefault(); if (note.trim()) void act("note", { note: note.trim() }); }}>
            <label htmlFor={`${field}-note`} className="sr-only">Note</label>
            <Input id={`${field}-note`} value={note} maxLength={2000} placeholder="Add a note"
                   onChange={(e) => setNote(e.target.value)} />
            <Button type="submit" size="sm" variant="outline" disabled={busy || !note.trim()}>Note</Button>
          </form>

          <section aria-label="History" className="space-y-2">
            <h3 className={LABEL}>History</h3>
            <ol className="space-y-2">
              {data.events.map((event) => (
                <li key={event.id} className="rounded-md border border-border p-2 text-xs">
                  <p className="flex flex-wrap gap-x-2">
                    <span className="font-medium">{event.action}</span>
                    <span className={event.actor_kind === "relayed" ? "text-warning" : undefined}>{who(event)}</span>
                    <span className="text-muted-foreground">via {via(event)}</span>
                    <span className="text-muted-foreground">{sinceText(event.created_at) || "0 s"} ago</span>
                  </p>
                  {event.quote && <p className="mt-1 break-words text-muted-foreground">Said: “{event.quote}”</p>}
                  {event.note && <p className="mt-1 break-words text-muted-foreground">Note: {event.note}</p>}
                </li>
              ))}
            </ol>
          </section>
        </>
      )}
    </Card>
  );
}

function AddForm({ onAdded }: { onAdded: () => void }) {
  const field = useId();
  const [open, setOpen] = useState(false);
  const [kind, setKind] = useState<LedgerKind>("pending");
  const [title, setTitle] = useState("");
  const [due, setDue] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  if (!open) return <Button size="sm" variant="outline" onClick={() => setOpen(true)}><Plus className="h-3 w-3" />Add</Button>;
  return (
    <form className="w-full space-y-2 rounded-md border border-border p-3" onSubmit={async (event) => {
      event.preventDefault();
      setBusy(true);
      setError("");
      try {
        await addLedgerItem({ kind, title: title.trim(), ...(due ? { due } : {}) });
        setTitle(""); setDue(""); setOpen(false);
        onAdded();
      } catch (err) {
        setError(errorText(err));
      } finally {
        setBusy(false);
      }
    }}>
      <div className="grid gap-2 sm:grid-cols-[10rem_1fr_10rem]">
        <div>
          <label htmlFor={`${field}-kind`} className={LABEL}>Kind</label>
          <select id={`${field}-kind`} className={SELECT} value={kind} onChange={(e) => setKind(e.target.value as LedgerKind)}>
            {(Object.keys(KIND_LABEL) as LedgerKind[]).map((value) => <option key={value} value={value}>{KIND_LABEL[value]}</option>)}
          </select>
        </div>
        <div>
          <label htmlFor={`${field}-title`} className={LABEL}>Title</label>
          <Input id={`${field}-title`} value={title} maxLength={200} required onChange={(e) => setTitle(e.target.value)} />
        </div>
        <div>
          <label htmlFor={`${field}-due`} className={LABEL}>Due</label>
          <Input id={`${field}-due`} type="date" value={due} onChange={(e) => setDue(e.target.value)} />
        </div>
      </div>
      {error && <p role="alert" className="break-words text-sm text-destructive">{error}</p>}
      <div className="flex gap-2">
        <Button type="submit" size="sm" disabled={busy || !title.trim()}>Add</Button>
        <Button type="button" size="sm" variant="ghost" onClick={() => setOpen(false)}>Cancel</Button>
      </div>
    </form>
  );
}

export function LedgerPage({ query, onQuery }: { query: string; onQuery: (query: string) => void }) {
  const params = new URLSearchParams(query);
  const status = (STATUS_FILTERS as readonly string[]).includes(params.get("status") ?? "") ? params.get("status")! : "open";
  const kind = (["pending", "decision", "work_item"] as const).find((value) => value === params.get("kind")) ?? null;
  const selected = params.get("item");
  const filtered = status !== "open" || kind !== null;

  const [state, setState] = useState<LedgerState | null>(null);
  const [view, setView] = useState<View | null>(null);
  const [error, setError] = useState("");
  const [version, setVersion] = useState(0);

  const update = (changes: Record<string, string | null>) => {
    const next = new URLSearchParams(query);
    for (const [key, value] of Object.entries(changes)) value === null ? next.delete(key) : next.set(key, value);
    onQuery(next.toString());
  };

  useEffect(() => startPolling(async (signal) => {
    try {
      const current = await fetchLedgerState(signal);
      setState(current);
      if (!current.enabled) { setView(null); setError(""); return; }
      if (filtered) {
        const found = await fetchLedgerItems({ status: status === "all" ? "all" : status, ...(kind ? { kind } : {}), limit: 50 }, signal);
        setView({ needsYou: [], working: [], decisions: [], flat: found.items, more: found.more });
      } else {
        const [needs, working, decisions] = await Promise.all([
          fetchLedgerItems({ status: "open", needs_you: true, limit: 50 }, signal),
          fetchLedgerItems({ status: "open", kind: "work_item", limit: 50 }, signal),
          fetchLedgerItems({ status: "all", kind: "decision", limit: 50 }, signal),
        ]);
        const cutoff = Date.now() - RECENT_DECISION_DAYS * 86_400_000;
        setView({
          needsYou: needs.items.sort(byUrgency), working: working.items,
          decisions: decisions.items.filter((item) => Date.parse(item.updated_at) >= cutoff && !unconfirmed(item)),
          flat: null, more: needs.more + working.more,
        });
      }
      setError("");
    } catch (err) {
      if (!signal.aborted) setError(errorText(err));
    }
  }, REFRESH_MS), [status, kind, filtered, version]);

  const reload = () => setVersion((n) => n + 1);
  const select = (id: string | null) => update({ item: id });

  const empty = view && !view.flat && !view.needsYou.length && !view.working.length && !view.decisions.length;
  return (
    <div className="mx-auto max-w-6xl space-y-4">
      <header className="flex flex-wrap items-end justify-between gap-3">
        <div>
          <h1 className="text-lg font-semibold tracking-tight">To-do</h1>
          <p className="text-sm text-muted-foreground">What waits for you, what was decided, and who is doing what.</p>
        </div>
        {state?.enabled && <AddForm onAdded={reload} />}
      </header>

      {state && !state.enabled && (
        <Card className="space-y-2 p-5">
          <p className="text-sm font-medium">The ledger is off.</p>
          <p className="text-sm text-muted-foreground">Turn it on in Settings → Chat to list gates, confirmations, tasks and decisions here.</p>
          <a href="#settings/chat" className="inline-flex h-9 pointer-coarse:h-11 items-center rounded-md border border-border px-4 text-sm font-medium hover:bg-accent">Open Settings → Chat</a>
        </Card>
      )}

      {state?.enabled && state.operator_unset && (
        <p className="flex gap-2 rounded-md bg-warning/10 p-3 text-sm text-warning">
          <AlertTriangle className="mt-0.5 h-4 w-4 shrink-0" />
          A chat bot has no My ID set, so decisions made in its direct chat are recorded as relayed. Set it in Settings → Channels.
        </p>
      )}

      {error && (
        <div role="alert" className="space-y-2 rounded-md border border-destructive/40 p-3">
          <p className="break-words text-sm text-destructive">Could not read the ledger: {error}</p>
          <Button size="sm" variant="outline" onClick={reload}>Retry</Button>
        </div>
      )}

      {state?.enabled && (
        <div className="flex flex-wrap gap-3">
          <div className="w-40">
            <label htmlFor="ledger-status" className={LABEL}>Status</label>
            <select id="ledger-status" className={SELECT} value={status} onChange={(e) => update({ status: e.target.value === "open" ? null : e.target.value })}>
              {STATUS_FILTERS.map((value) => <option key={value} value={value}>{value === "all" ? "All" : STATUS_LABEL[value]}</option>)}
            </select>
          </div>
          <div className="w-40">
            <label htmlFor="ledger-kind" className={LABEL}>Kind</label>
            <select id="ledger-kind" className={SELECT} value={kind ?? ""} onChange={(e) => update({ kind: e.target.value || null })}>
              <option value="">Any</option>
              {(Object.keys(KIND_LABEL) as LedgerKind[]).map((value) => <option key={value} value={value}>{KIND_LABEL[value]}</option>)}
            </select>
          </div>
        </div>
      )}

      {state?.enabled && !view && !error && <p role="status" className="text-sm text-muted-foreground"><Loader2 className="mr-1 inline h-3 w-3 animate-spin" />Loading…</p>}

      {view && (
        <div className={cn("grid gap-4", selected && "lg:grid-cols-[minmax(0,1fr)_24rem]")}>
          <div className="min-w-0 space-y-5">
            {empty && <p className="text-sm text-muted-foreground">Nothing is waiting for you.</p>}
            {view.flat ? (
              view.flat.length ? (
                <>
                  <ul className="space-y-2">{view.flat.map((item) => <Row key={item.id} item={item} selected={selected === item.id} onSelect={() => select(item.id)} />)}</ul>
                  {view.more > 0 && <p className="text-xs text-muted-foreground">{view.more} more not shown: narrow the filter.</p>}
                </>
              ) : <p className="text-sm text-muted-foreground">No items match.</p>
            ) : (
              <>
                <Group title="Needs you" items={view.needsYou} selected={selected} onSelect={select}
                       hint="Open to-dos, questions and decisions only someone else said." />
                <Group title="In progress" items={view.working} selected={selected} onSelect={select} />
                <Group title={`Recent decisions`} items={view.decisions} selected={selected} onSelect={select}
                       hint={`The last ${RECENT_DECISION_DAYS} days.`} />
              </>
            )}
          </div>
          {selected && <Detail key={selected} id={selected} onChanged={reload} onClose={() => select(null)} />}
        </div>
      )}
    </div>
  );
}
