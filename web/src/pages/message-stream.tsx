import { useCallback, useEffect, useId, useState } from "react";
import { Loader2 } from "lucide-react";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Switch } from "@/components/ui/switch";
import { LABEL, SELECT } from "@/components/registry-ui";
import {
  configureMessageStream, fetchMessageStream, fetchMessageStreamRuns,
  type MessageStreamRun, type MessageStreamSource,
} from "@/lib/api";
import { sinceText } from "@/lib/gates";
import { startPolling } from "@/lib/polling";
import { errorText } from "@/lib/use-action";
import { formatBytes } from "@/lib/utils";

const REFRESH_MS = 10_000;

const HEALTH: Record<MessageStreamSource["health"], { label: string; variant: "success" | "warning" | "outline" | "destructive"; note: string }> = {
  healthy: { label: "Healthy", variant: "success", note: "The last pass finished and the stream is current." },
  degraded: { label: "Degraded", variant: "warning", note: "The last pass did not finish every file; its error is in the run list." },
  stale: { label: "Stale", variant: "destructive", note: "No pass has finished for over an hour." },
  off: { label: "Off", variant: "outline", note: "Nothing is being read. What was stored stays." },
};
const RUN_STATUS: Record<string, "success" | "warning" | "destructive" | "outline"> = {
  ok: "success", running: "outline", partial: "warning", interrupted: "warning",
};

const when = (iso: string | null) => (iso ? `${sinceText(iso) || "0 s"} ago` : "never");
const duration = (run: MessageStreamRun) => {
  if (!run.finished_at) return "";
  const seconds = Math.max(0, Math.round((Date.parse(run.finished_at) - Date.parse(run.started_at)) / 1000));
  return seconds < 60 ? `${seconds} s` : `${Math.floor(seconds / 60)} min ${seconds % 60} s`;
};

function Fact({ label, value, hint }: { label: string; value: React.ReactNode; hint?: string }) {
  return (
    <div className="min-w-0">
      <dt className={LABEL}>{label}</dt>
      <dd className="mt-0.5 text-lg font-semibold tabular-nums">{value}</dd>
      {hint && <p className="text-xs text-muted-foreground">{hint}</p>}
    </div>
  );
}

function SetupForm({ source, busy, onStart }: {
  source: MessageStreamSource | null; busy: boolean;
  onStart: (fields: { bridge_dir: string; start_from: "end" | "beginning"; user_open_id: string | null }) => void;
}) {
  const id = useId();
  const [dir, setDir] = useState(source?.bridge_dir ?? "");
  const [openId, setOpenId] = useState(source?.user_open_id ?? "");
  const [startFrom, setStartFrom] = useState<"end" | "beginning">(source?.start_from ?? "end");
  return (
    <form
      className="space-y-4"
      onSubmit={(event) => {
        event.preventDefault();
        if (dir.trim()) onStart({ bridge_dir: dir.trim(), start_from: startFrom, user_open_id: openId.trim() || null });
      }}
    >
      <p className="text-sm text-muted-foreground">
        Looper can read the per-chat files of a collector that already runs on this machine, and keep the messages in
        its own database. It only reads those files; it does not call Lark.
      </p>
      <div className="space-y-1.5">
        <label htmlFor={`${id}-dir`} className={LABEL}>Collector directory (the one that holds chats/)</label>
        <Input id={`${id}-dir`} className="font-mono" placeholder="/path/to/collector" value={dir}
          onChange={(e) => setDir(e.target.value)} disabled={busy} />
      </div>
      <div className="space-y-1.5">
        <label htmlFor={`${id}-from`} className={LABEL}>Start from</label>
        <select id={`${id}-from`} value={startFrom} disabled={busy} onChange={(e) => setStartFrom(e.target.value as "end" | "beginning")}
          className={SELECT}>
          <option value="end">Now: only messages that arrive from here on</option>
          <option value="beginning">The beginning: import everything the files hold</option>
        </select>
        <p className="text-xs text-muted-foreground">
          Importing a large history takes a while and keeps raw messages in the database (a bot's after 30 days as text only).
        </p>
      </div>
      <div className="space-y-1.5">
        <label htmlFor={`${id}-open`} className={LABEL}>Your Lark open id (optional)</label>
        <Input id={`${id}-open`} className="font-mono" placeholder="ou_…" value={openId}
          onChange={(e) => setOpenId(e.target.value)} disabled={busy} />
        <p className="text-xs text-muted-foreground">Lets Looper mark which messages mention you.</p>
      </div>
      <Button type="submit" disabled={busy || !dir.trim()} className="min-h-11">
        {busy && <Loader2 className="h-4 w-4 animate-spin motion-reduce:animate-none" aria-hidden="true" />}
        Start reading
      </Button>
    </form>
  );
}

function StatusCard({ source, busy, onSwitch }: { source: MessageStreamSource; busy: boolean; onSwitch: (on: boolean) => void }) {
  const health = HEALTH[source.health];
  const on = source.ingest_mode !== "off";
  return (
    <Card>
      <CardHeader className="flex-row flex-wrap items-center gap-3">
        <CardTitle className="text-base text-foreground">{source.name}</CardTitle>
        <Badge variant={health.variant}>{health.label}</Badge>
        <span className="flex-1" />
        <label className="flex min-h-11 cursor-pointer items-center gap-2 text-sm">
          <Switch checked={on} disabled={busy} onChange={onSwitch} label={`Read ${source.name}`} />
          <span>{on ? "Reading" : "Not reading"}</span>
        </label>
      </CardHeader>
      <CardContent className="space-y-4">
        <p className="text-sm text-muted-foreground">
          {health.note} Mode: {source.ingest_mode}, every pass on <span className="font-mono">{source.cron}</span> ({source.timezone}).
        </p>
        <dl className="grid grid-cols-2 gap-x-6 gap-y-4 sm:grid-cols-4">
          <Fact label="Messages stored" value={source.events.toLocaleString()} hint={`${source.events_last_hour.toLocaleString()} in the last hour`} />
          <Fact label="Newest message" value={when(source.newest_sent_at)} />
          <Fact label="Last good pass" value={when(source.last_ok_at)} />
          <Fact label="Chats" value={source.chats.toLocaleString()}
            hint={`${source.chats_excluded} excluded · ${source.chats_failing} failing`} />
        </dl>
        <p className="break-all font-mono text-xs text-muted-foreground">{source.bridge_dir}</p>
        <p className="text-xs text-muted-foreground">
          Raw messages take {formatBytes(source.body_bytes)}. This page shows counts and states only, never message text.
        </p>
      </CardContent>
    </Card>
  );
}

function RunList({ runs }: { runs: MessageStreamRun[] }) {
  if (runs.length === 0) return <p className="text-sm text-muted-foreground">No pass has run yet.</p>;
  return (
    <ul className="divide-y divide-border rounded-lg border border-border bg-card">
      {runs.map((run) => (
        <li key={run.run_id} className="space-y-1 px-4 py-3 text-sm">
          <div className="flex flex-wrap items-center gap-x-3 gap-y-1">
            <Badge variant={RUN_STATUS[run.status] ?? "destructive"}>{run.status}</Badge>
            <span className="tabular-nums" title={run.started_at}>{when(run.started_at)}</span>
            <span className="text-muted-foreground">{run.mode}{run.trigger === "manual" ? " · manual" : ""}{duration(run) && ` · ${duration(run)}`}</span>
            <span className="flex-1" />
            <span className="tabular-nums">
              {run.events_new} new · {run.events_seen} already stored · {run.events_skipped} skipped
              {run.files_rescanned ? ` · ${run.files_rescanned} file${run.files_rescanned === 1 ? "" : "s"} read again` : ""}
            </span>
          </div>
          {run.error && <p className="text-xs text-destructive [overflow-wrap:anywhere]">{run.error}</p>}
        </li>
      ))}
    </ul>
  );
}

/** The message stream: one source, whether it is being read, how current it is, and its recent passes. */
export function MessageStreamPage() {
  const [sources, setSources] = useState<MessageStreamSource[] | null>(null);
  const [runs, setRuns] = useState<MessageStreamRun[]>([]);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const [attempt, setAttempt] = useState(0);

  useEffect(() => {
    return startPolling(async (signal) => {
      try {
        const { sources: found } = await fetchMessageStream(signal);
        const first = found[0];
        const read = first ? (await fetchMessageStreamRuns(first.source_id, signal)).runs : [];
        setSources(found);
        setRuns(read);
        setError("");
      } catch (err) {
        if (!signal.aborted) setError(errorText(err));
      }
    }, REFRESH_MS);
  }, [attempt]);

  const apply = useCallback(async (fields: Parameters<typeof configureMessageStream>[0]) => {
    setBusy(true);
    setError("");
    try {
      await configureMessageStream(fields);
      setAttempt((n) => n + 1);
    } catch (err) {
      setError(errorText(err));
    } finally {
      setBusy(false);
    }
  }, []);

  const source = sources?.[0] ?? null;
  return (
    <div className="mx-auto max-w-4xl space-y-5">
      <div className="space-y-1">
        <h1 className="text-xl font-semibold">Message stream</h1>
        <p className="text-sm text-muted-foreground">Messages Looper has read into its own database, for subscriptions to use.</p>
      </div>
      {error && (
        <div role="alert" className="flex flex-wrap items-center gap-3 text-sm text-destructive [overflow-wrap:anywhere]">
          <span>{sources ? "The last action failed" : "Cannot load the message stream"} ({error})</span>
          <Button type="button" variant="outline" size="sm" className="min-h-11" onClick={() => setAttempt((n) => n + 1)}>Retry</Button>
        </div>
      )}
      {!sources && !error && (
        <p role="status" className="flex items-center gap-2 text-sm text-muted-foreground">
          <Loader2 className="h-4 w-4 animate-spin motion-reduce:animate-none" aria-hidden="true" /> Loading…
        </p>
      )}
      {sources && !source && (
        <Card><CardHeader><CardTitle className="text-base text-foreground">Set up a source</CardTitle></CardHeader>
          <CardContent><SetupForm source={null} busy={busy} onStart={(f) => void apply({ ingest_mode: "bridge", ...f })} /></CardContent></Card>
      )}
      {source && (
        <>
          <StatusCard source={source} busy={busy}
            onSwitch={(on) => void apply({ source_id: source.source_id, ingest_mode: on ? "bridge" : "off" })} />
          {source.ingest_mode === "off" && !source.bridge_dir && (
            <Card><CardContent className="pt-5"><SetupForm source={source} busy={busy}
              onStart={(f) => void apply({ source_id: source.source_id, ingest_mode: "bridge", ...f })} /></CardContent></Card>
          )}
          <section aria-labelledby="runs-title" className="space-y-2">
            <h2 id="runs-title" className="text-sm font-medium text-muted-foreground">Recent passes</h2>
            <RunList runs={runs} />
          </section>
        </>
      )}
    </div>
  );
}
