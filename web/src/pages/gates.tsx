import { useCallback, useEffect, useLayoutEffect, useRef, useState } from "react";
import { Loader2 } from "lucide-react";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Switch } from "@/components/ui/switch";
import { GateReply } from "@/components/dashboard-gates";
import { LABEL, SELECT } from "@/components/registry-ui";
import {
  fetchGates, GATES_CHANGED_EVENT, isActiveTask, type GateList, type GateListRow, type GateListState,
} from "@/lib/api";
import { gateLook, gateWaitsOn, isGateWaiting, sinceText } from "@/lib/gates";
import { errorText } from "@/lib/use-action";
import { useProjects } from "@/lib/use-projects";
import { cn } from "@/lib/utils";

/** How often the list refreshes. */
const REFRESH_MS = 10_000;
/** Rows read first and added by each "Show more"; refreshes read through the last loaded gate. */
const PAGE = 50;
const MAX_ROWS = 200;

const linkClass =
  "rounded-sm hover:underline focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring";
// Every row is its own grid, so no track may size to its content: the header and the rows must line up.
const COLUMNS = "xl:grid-cols-[minmax(0,1.2fr)_minmax(0,1.8fr)_9rem_6rem_9rem_8rem_14rem]";

interface Filters {
  state: GateListState;
  waiting: boolean;
  project: string;
}

/** The list's filters as the hash query has them (`state=`, `waiting=user`, `project=<id>`); an unknown state is open. */
function readFilters(query: string): Filters {
  const params = new URLSearchParams(query);
  const s = params.get("state");
  const state: GateListState = s === "closed" || s === "all" ? s : "open";
  return { state, waiting: state === "open" && params.get("waiting") === "user", project: params.get("project") ?? "" };
}

/** The hash query for these filters, the defaults left out; "" for none. */
function filterQuery({ state, waiting, project }: Filters): string {
  const params = new URLSearchParams();
  if (state !== "open") params.set("state", state);
  if (state === "open" && waiting) params.set("waiting", "user");
  if (project) params.set("project", project);
  return params.toString();
}

const EMPTY: Record<GateListState, string> = {
  open: "No gate waits for an answer.",
  closed: "No gate has closed yet.",
  all: "No task has opened a gate yet.",
};

const taskHref = (id: string) => `#tasks/${encodeURIComponent(id)}`;
const rowKey = ({ task, gate }: GateListRow) => `${task.task_id}/${gate.step}`;
const full = (iso: string) => new Date(iso).toLocaleString();

function lastMessageText(row: GateListRow): string {
  const m = row.last_message;
  if (!m) return "—";
  return `${m.author || (m.role === "user" ? "You" : "Planner")} · ${sinceText(m.created_at)}`;
}

function GateRow({ row, answering, onAnswer, onChanged }: {
  row: GateListRow; answering: boolean; onAnswer: () => void; onChanged: () => void;
}) {
  const { task, gate } = row;
  const active = isActiveTask(task);
  const look = gateLook(gate, active);
  const canAnswer = task.group !== "completed" && isGateWaiting(gate) && gateWaitsOn(gate, active) === "user";
  const closed = gate.closed_at && !isGateWaiting(gate);
  return (
    <li className="rounded-lg border border-border bg-card px-4 py-3 text-sm">
      <div className={cn("grid min-w-0 gap-x-4 gap-y-2 xl:items-center", COLUMNS)}>
        <a href={taskHref(task.task_id)}
          title={task.name} className={cn("line-clamp-2 min-w-0 font-medium [overflow-wrap:anywhere]", linkClass)}>{task.name}</a>
        <span className="min-w-0 [overflow-wrap:anywhere]">{gate.topic}</span>
        <span><Badge variant={look.variant}>{look.label}</Badge></span>
        <span className="text-xs text-muted-foreground">
          {gate.opened_at
            ? <time dateTime={gate.opened_at} title={full(gate.opened_at)}>{sinceText(gate.opened_at)}</time>
            : "—"}
          {closed && gate.closed_at && (
            <> · <time dateTime={gate.closed_at} title={full(gate.closed_at)}>closed {sinceText(gate.closed_at)} ago</time></>
          )}
        </span>
        <span className="text-xs text-muted-foreground">{lastMessageText(row)}</span>
        <span className="text-xs text-muted-foreground">
          {row.agenda_open > 0 && `${row.agenda_open} open ${row.agenda_open === 1 ? "question" : "questions"}`}
        </span>
        <span className="flex flex-wrap items-center gap-x-3 gap-y-1 text-xs">
          <a href={`${taskHref(task.task_id)}?gate=${encodeURIComponent(gate.step)}`}
            className={cn("inline-flex min-h-11 items-center text-primary", linkClass)}>Open the discussion</a>
          {canAnswer && (
            <Button type="button" variant="outline" size="sm" aria-expanded={answering} onClick={onAnswer}
              className="min-h-11 focus-visible:ring-2">Answer</Button>
          )}
        </span>
      </div>
      {answering && canAnswer && (
        <div className="mt-3">
          <GateReply task={task} gate={gate} onChanged={onChanged} />
        </div>
      )}
    </li>
  );
}

export function GatesPage({ query, onQuery }: { query: string; onQuery: (query: string) => void }) {
  const filters = readFilters(query);
  const { state, waiting, project } = filters;
  const { projects } = useProjects();
  const [{ rows, answering, cursor: savedCursor }, setView] = useState<{
    rows: GateListRow[]; answering: string; cursor: string | null;
  }>({ rows: [], answering: "", cursor: null });
  const [data, setData] = useState<Pick<GateList, "total" | "counts" | "has_more"> | null>(null);
  const [error, setError] = useState("");
  const latest = useRef(0);
  const loading = useRef(false);
  const cursor = useRef<string | null>(null);

  // Timer reads follow the committed rows' boundary without restarting the refresh effect.
  useLayoutEffect(() => { cursor.current = savedCursor; }, [savedCursor]);

  const read = useCallback(
    (mode: "refresh" | "more" | "retry", force: boolean) => {
      if (loading.current && !force) return Promise.resolve();
      const seq = ++latest.current;
      loading.current = true;
      const more = mode === "more";
      const request = (limit: number, after: string | null, through?: string) => fetchGates({
        state, ...(waiting ? { waitingOn: "user" as const } : {}), ...(project ? { projectId: project } : {}),
        limit, ...(after ? { cursor: after } : {}), ...(through ? { through } : {}),
      });
      // Keysets survive a gate closing between pages. The old boundary keeps new gates from displacing drafts.
      const load = async (): Promise<GateList> => {
        if (more) return request(PAGE, cursor.current);
        const through = mode === "retry" ? undefined : cursor.current ?? undefined;
        let list = await request(through ? MAX_ROWS : PAGE, null, through);
        let next = list;
        while (through && next.gates.length === MAX_ROWS && next.has_more && next.next_cursor) {
          next = await request(MAX_ROWS, next.next_cursor, through);
          if (next.total !== list.total) throw new Error("The gate list changed while loading. Retry the request.");
          if (next.gates.length === 0) break;
          list = { ...next, gates: [...list.gates, ...next.gates] };
        }
        if (new Set(list.gates.map(rowKey)).size !== list.gates.length)
          throw new Error("The gate list changed while loading. Retry the request.");
        return list;
      };
      return load()
        .then((list) => {
          if (seq !== latest.current) return;
          loading.current = false;
          setView((current) => {
            const keys = new Set(list.gates.map(rowKey));
            // A recovered page may omit the editor; keep it mounted until pagination reaches it again.
            const editing = current.rows.find((row) => rowKey(row) === current.answering);
            const rows = more
              ? [...current.rows.filter((row) => !keys.has(rowKey(row))), ...list.gates]
              : [...list.gates];
            if (editing && !rows.some((row) => rowKey(row) === current.answering)) rows.push(editing);
            return { ...current, rows, cursor: more && !list.gates.length ? current.cursor : list.next_cursor };
          });
          setData({ total: list.total, counts: list.counts, has_more: list.has_more });
          setError("");
        })
        .catch((err) => {
          if (seq !== latest.current) return;
          loading.current = false;
          setError(errorText(err));
        });
    },
    [state, waiting, project],
  );

  useEffect(() => {
    // A new filter starts over: the rows of the old one are not shown.
    cursor.current = null;
    setView({ rows: [], answering: "", cursor: null });
    setData(null);
    void read("refresh", true);
    const timer = setInterval(() => void read("refresh", false), REFRESH_MS);
    return () => {
      clearInterval(timer);
      latest.current++;
      loading.current = false;
    };
  }, [read]);

  const change = (next: Partial<Filters>) => onQuery(filterQuery({ ...filters, ...next }));
  const counts = data?.counts;
  const tabs: [GateListState, string, number | undefined][] = [
    ["open", "Open", counts?.open], ["closed", "Closed", counts?.closed], ["all", "All", counts?.all],
  ];
  const onChanged = () => {
    void read("refresh", true);
    window.dispatchEvent(new Event(GATES_CHANGED_EVENT));
  };

  return (
    <div className="space-y-4">
      <div className="space-y-1">
        <h1 className="text-xl font-semibold">User Gates</h1>
        {counts && <p className="text-sm text-muted-foreground">{counts.open} open · {counts.waiting_user} waiting for you</p>}
      </div>
      <div className="flex flex-wrap items-end gap-x-4 gap-y-3">
        <div role="group" aria-label="Gate state" className="flex gap-1">
          {tabs.map(([value, label, n]) => (
            <Button key={value} type="button" size="sm" variant={state === value ? "default" : "outline"}
              aria-pressed={state === value} onClick={() => change({ state: value, waiting: false })}
              className="min-h-11 focus-visible:ring-2">
              {label}{n !== undefined && ` ${n}`}
            </Button>
          ))}
        </div>
        {state === "open" && (
          <label className="flex min-h-11 cursor-pointer items-center gap-2 text-sm">
            <Switch checked={waiting} onChange={(on) => change({ waiting: on })} label="Waiting for you only" />
            <span>Waiting for you only{counts && ` (${counts.waiting_user})`}</span>
          </label>
        )}
        <label className="flex min-w-40 flex-col gap-1">
          <span className={LABEL}>Project</span>
          <select value={project} onChange={(e) => change({ project: e.target.value })}
            className={cn(SELECT, "min-h-11 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring")}>
            <option value="">All projects</option>
            {(projects ?? []).map((p) => <option key={p.project_id} value={p.project_id}>{p.name}</option>)}
          </select>
        </label>
      </div>

      {error && (
        <div role="alert" className="flex flex-wrap items-center gap-3 text-sm text-destructive [overflow-wrap:anywhere]">
          <span>Cannot load gates ({error})</span>
          <Button type="button" variant="outline" size="sm" className="min-h-11" onClick={() => {
            // Only this request omits the saved boundary; commit its replacement on success.
            void read("retry", true);
          }}>
            Retry
          </Button>
        </div>
      )}

      {!data && !error && (
        <p role="status" className="flex items-center gap-2 text-sm text-muted-foreground">
          <Loader2 className="h-4 w-4 animate-spin motion-reduce:animate-none" aria-hidden="true" /> Loading gates…
        </p>
      )}
      {data && rows.length === 0 && !error && (
        <p className="text-sm text-muted-foreground">
          {state === "open" && waiting ? "No gate waits for you." : EMPTY[state]}
        </p>
      )}
      {rows.length > 0 && (
        <div className="space-y-2">
          <div aria-hidden="true"
            className={cn("hidden gap-x-4 px-4 text-xs font-medium text-muted-foreground xl:grid", COLUMNS)}>
            <span>Task</span><span>Topic</span><span>State</span><span>Opened</span>
            <span>Last message</span><span>Questions</span><span>Actions</span>
          </div>
          <ul className="space-y-2">
          {rows.map((row) => (
            <GateRow key={rowKey(row)} row={row} answering={answering === rowKey(row)}
              onAnswer={() => setView((current) => ({
                ...current, answering: current.answering === rowKey(row) ? "" : rowKey(row),
              }))} onChanged={onChanged} />
          ))}
          </ul>
        </div>
      )}
      {data?.has_more && (
        <Button type="button" variant="outline" className="min-h-11" onClick={() => void read("more", true)}>
          Show more
        </Button>
      )}
    </div>
  );
}
