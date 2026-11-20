import { useEffect, useState } from "react";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import { Input } from "@/components/ui/input";
import { ConfirmDialog } from "@/components/confirm-dialog";
import { LeadSources } from "@/components/lead-sources";
import { LeadsChats } from "@/components/leads-chats";
import { leadsDelete, leadsMark, leadsSearch, type Lead, type LeadsSearch } from "@/lib/api-leads";
import { CONFIDENCE_ZH, LEAD_KIND_ZH, LEAD_STATE_ZH, LEADS_NOTICE, label, relative } from "@/lib/leads-labels";
import { errorText } from "@/lib/use-action";
import { V1Error } from "@/lib/v1";
import { SELECT } from "@/components/registry-ui";

const KINDS = Object.keys(LEAD_KIND_ZH);
const STATES = ["active", "expired", "superseded", "promoted", "inaccurate", "all"];

/** The 线索 tab: search leads, mark or delete one, and choose which chats are read. */
export function LeadsTab() {
  const [query, setQuery] = useState("");
  const [chat, setChat] = useState("");
  const [person, setPerson] = useState("");
  const [days, setDays] = useState("");
  const [kind, setKind] = useState("");
  const [state, setState] = useState("active");
  const [page, setPage] = useState<LeadsSearch | null>(null);
  const [error, setError] = useState("");
  const [conflict, setConflict] = useState("");
  const [open, setOpen] = useState<string | null>(null);
  const [marking, setMarking] = useState<Lead | null>(null);
  const [note, setNote] = useState("");
  const [deleting, setDeleting] = useState<Lead | null>(null);
  const [busy, setBusy] = useState(false);
  const [attempt, setAttempt] = useState(0);

  const dayCount = Number(days);
  const args = {
    state,
    ...(query.trim() ? { query: query.trim() } : {}),
    ...(chat.trim() ? { chat: chat.trim() } : {}),
    ...(person.trim() ? { person: person.trim() } : {}),
    ...(Number.isInteger(dayCount) && dayCount >= 1 ? { days: dayCount } : {}),
    ...(kind ? { kind } : {}),
  };

  useEffect(() => {
    let live = true;
    leadsSearch(args).then(
      (found) => { if (live) { setPage(found); setError(""); setConflict(""); } },
      (err: unknown) => {
        if (!live) return;
        setPage(null);
        if (err instanceof V1Error && err.code === "conflict") setConflict(err.message);
        else setError(errorText(err));
      },
    );
    return () => { live = false; };
    // The object is rebuilt each render; the fields are the real inputs.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [query, chat, person, days, kind, state, attempt]);

  const after = () => { setMarking(null); setDeleting(null); setNote(""); setAttempt((n) => n + 1); };
  const act = async (action: () => Promise<unknown>) => {
    setBusy(true);
    setError("");
    try {
      await action();
      after();
    } catch (err) {
      if (err instanceof V1Error && err.code === "conflict") setConflict(err.message);
      else setError(errorText(err));
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="space-y-5">
      <p className="text-sm text-muted-foreground">{page?.notice || LEADS_NOTICE}</p>
      <form className="grid gap-3 sm:grid-cols-2 lg:grid-cols-3" onSubmit={(event) => event.preventDefault()}>
        <Input aria-label="搜索" value={query} onChange={(e) => setQuery(e.target.value)} placeholder="搜索" />
        <Input aria-label="会话" value={chat} onChange={(e) => setChat(e.target.value)} placeholder="会话" />
        <Input aria-label="人" value={person} onChange={(e) => setPerson(e.target.value)} placeholder="人" />
        <Input aria-label="天数" inputMode="numeric" value={days} onChange={(e) => setDays(e.target.value)} placeholder="天数" />
        <select aria-label="种类" value={kind} onChange={(e) => setKind(e.target.value)} className={SELECT}>
          <option value="">全部种类</option>
          {KINDS.map((item) => <option key={item} value={item}>{label(LEAD_KIND_ZH, item)}</option>)}
        </select>
        <select aria-label="状态" value={state} onChange={(e) => setState(e.target.value)} className={SELECT}>
          {STATES.map((item) => <option key={item} value={item}>{label(LEAD_STATE_ZH, item)}</option>)}
        </select>
      </form>
      {conflict && <p role="status" data-state="conflict" className="text-sm">{conflict}</p>}
      {error && <p role="alert" className="text-sm text-destructive">{error} <Button type="button" variant="outline" size="sm" onClick={() => setAttempt((n) => n + 1)}>重试</Button></p>}
      {!page && !error && !conflict && <p role="status" className="text-sm text-muted-foreground">加载中…</p>}
      {page && page.leads.length === 0 && !conflict && <p role="status" className="text-sm text-muted-foreground">没有符合条件的线索</p>}
      <ul className="space-y-3">
        {page?.leads.map((lead) => (
          <li key={lead.id} className="space-y-2 rounded-lg border border-border p-4">
            <div className="flex flex-wrap items-center gap-2 text-sm">
              <span className="font-mono text-xs">{lead.id}</span>
              <Badge variant="outline">{label(LEAD_KIND_ZH, lead.kind)}</Badge>
              <span>{label(CONFIDENCE_ZH, lead.confidence)}</span>
              <Badge variant={lead.state === "inaccurate" ? "destructive" : lead.state === "active" ? "success" : "outline"}>
                {label(LEAD_STATE_ZH, lead.state)}
              </Badge>
              <span className="text-muted-foreground">有效至 {lead.valid_until ? lead.valid_until.slice(0, 10) : "—"}（{relative(lead.valid_until)}）</span>
            </div>
            <p className="text-sm">{lead.summary}</p>
            {open === lead.id && lead.detail && <p className="whitespace-pre-wrap text-sm text-muted-foreground">{lead.detail}</p>}
            <LeadSources sources={lead.sources} total={lead.sources_total} />
            {lead.superseded_by && <p className="text-sm">已被推翻 → {lead.superseded_by}</p>}
            <div className="flex flex-wrap gap-2">
              {lead.detail && <Button type="button" variant="ghost" size="sm" onClick={() => setOpen(open === lead.id ? null : lead.id)}>{open === lead.id ? "收起" : "展开"}</Button>}
              <Button type="button" variant="outline" size="sm" onClick={() => { setNote(""); setMarking(lead); }}>标为不准确</Button>
              <Button type="button" variant="outline" size="sm" onClick={() => setDeleting(lead)}>删除</Button>
            </div>
          </li>
        ))}
      </ul>
      {page != null && page.more > 0 && <p className="text-xs text-muted-foreground">另有 {page.more} 条</p>}
      <LeadsChats />
      {marking && (
        <ConfirmDialog title={`标为不准确 ${marking.id}`} onClose={() => setMarking(null)}>
          <label htmlFor="lead-note" className="text-xs text-muted-foreground">备注（可选）</label>
          <textarea id="lead-note" value={note} onChange={(e) => setNote(e.target.value)} rows={3} maxLength={500}
            className="w-full rounded-md border border-input bg-transparent p-3 text-sm focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring" />
          <div className="flex justify-end gap-2">
            <Button type="button" variant="outline" onClick={() => setMarking(null)}>取消</Button>
            <Button type="button" disabled={busy} onClick={() => void act(() => leadsMark({ lead_id: marking.id, ...(note.trim() ? { note: note.trim() } : {}) }))}>标为不准确</Button>
          </div>
        </ConfirmDialog>
      )}
      {deleting && (
        <ConfirmDialog title={`删除 ${deleting.id}`} onClose={() => setDeleting(null)}>
          <p className="text-sm">删除后这条线索不再出现在搜索里。</p>
          <div className="flex justify-end gap-2">
            <Button type="button" variant="outline" onClick={() => setDeleting(null)}>取消</Button>
            <Button type="button" variant="destructive" disabled={busy} onClick={() => void act(() => leadsDelete({ lead_id: deleting.id }))}>删除</Button>
          </div>
        </ConfirmDialog>
      )}
    </div>
  );
}
