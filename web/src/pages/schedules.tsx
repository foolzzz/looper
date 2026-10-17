import { useCallback, useEffect, useId, useRef, useState } from "react";
import { ArrowRight, Loader2, Play } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Switch } from "@/components/ui/switch";
import { DeleteSchedule, LastRun, NoticeText, ScheduleTime } from "@/components/project-schedules";
import { ScheduleDetail } from "@/components/schedule-detail";
import { LABEL, SELECT } from "@/components/registry-ui";
import { fetchAllSchedules, fetchSchedule, runSchedule, updateSchedule, type ScheduleInfo } from "@/lib/api";
import { lastFiredAt, omit, runNotice, useNotices, type Notice } from "@/lib/schedules";
import { errorText } from "@/lib/use-action";
import { useProjects } from "@/lib/use-projects";
import { formatUsd } from "@/lib/usage";
import { cn } from "@/lib/utils";

/** How often the list (next run, last result, today's spend) refreshes. */
const REFRESH_MS = 10_000;

const linkClass =
  "rounded-sm hover:underline focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring";
const projectHref = (projectId: string) => `#projects/${encodeURIComponent(projectId)}/schedules`;
const scheduleHref = (s: ScheduleInfo) => `#schedules/${encodeURIComponent(s.schedule_id)}`;

interface ProjectGroup {
  projectId: string;
  name: string;
  schedules: ScheduleInfo[];
}

/** The schedules by project, in the server's order (project name, then name). */
function byProject(schedules: ScheduleInfo[]): ProjectGroup[] {
  const groups: ProjectGroup[] = [];
  for (const schedule of schedules) {
    const last = groups[groups.length - 1];
    if (last?.projectId === schedule.project_id) last.schedules.push(schedule);
    else groups.push({ projectId: schedule.project_id, name: schedule.project_name, schedules: [schedule] });
  }
  return groups;
}

const onCount = (schedules: ScheduleInfo[]) => schedules.filter((s) => s.enabled).length;

type Status = "all" | "enabled" | "disabled";

/** The list's filters as the hash query has them (`project=<id>&enabled=true|false`); an unknown status is all. */
function readFilters(query: string): { project: string; status: Status } {
  const params = new URLSearchParams(query);
  const enabled = params.get("enabled");
  return { project: params.get("project") ?? "", status: enabled === "true" ? "enabled" : enabled === "false" ? "disabled" : "all" };
}

/** The hash query for these filters; "" for none. */
function filterQuery(project: string, status: Status): string {
  const params = new URLSearchParams();
  if (project) params.set("project", project);
  if (status !== "all") params.set("enabled", String(status === "enabled"));
  return params.toString();
}

const byLowerName = (a: { name: string }, b: { name: string }) => a.name.toLowerCase().localeCompare(b.name.toLowerCase());

/** When it runs next, in its own zone; a disabled one does not. */
function NextRun({ schedule }: { schedule: ScheduleInfo }) {
  if (!schedule.enabled) return <span>Disabled</span>;
  if (!schedule.next_fire_at) return <span>No next run</span>;
  return (
    <span>
      Next <ScheduleTime iso={schedule.next_fire_at} timeZone={schedule.timezone} className="tabular-nums text-foreground/90" />
    </span>
  );
}

/** One schedule: what it is, when it runs, how its last run went, and its switch, "Run now" and "Delete schedule". Its
 * name opens its detail view (`#schedules/<id>`): its settings and the runs it fired. */
function ScheduleRow({
  schedule,
  enabled,
  switching,
  starting,
  notice,
  onToggle,
  onRunNow,
  onDeleted,
}: {
  schedule: ScheduleInfo;
  /** What the switch shows: the value being saved while it is. */
  enabled: boolean;
  switching: boolean;
  starting: boolean;
  notice: Notice | undefined;
  onToggle: (on: boolean) => void;
  onRunNow: () => void;
  onDeleted: () => void;
}) {
  const run = schedule.last_run;
  const off = !schedule.enabled;
  return (
    <li
      className={cn(
        "rounded-lg border px-4 py-3",
        // A disabled schedule is dimmed: a dashed edge and a muted name, its text still readable.
        off ? "border-dashed border-border" : "border-border bg-card",
      )}
    >
      <div className="flex flex-wrap items-center gap-x-4 gap-y-2">
        <div className="min-w-0 flex-1 basis-72 space-y-1">
          <a
            href={scheduleHref(schedule)}
            className={cn("block truncate text-sm font-semibold", off && "text-muted-foreground", linkClass)}
          >
            {schedule.name}
          </a>
          <p className="truncate text-xs text-muted-foreground">{schedule.description}</p>
          <div className="flex flex-wrap items-center gap-x-2 gap-y-1 text-xs text-muted-foreground">
            <NextRun schedule={schedule} />
            <span aria-hidden="true">·</span>
            <LastRun schedule={schedule} />
            {run && (
              <span className="flex items-center gap-1">
                <ScheduleTime iso={lastFiredAt(run)} timeZone={schedule.timezone} />
                {run.task_id && (
                  <>
                    {" · "}
                    <a href={`#tasks/${encodeURIComponent(run.task_id)}`} className={cn("text-foreground/90", linkClass)}>
                      its task
                    </a>
                  </>
                )}
              </span>
            )}
            {schedule.spent_today_usd > 0 && (
              <span className="tabular-nums">
                {formatUsd(schedule.spent_today_usd)} today
                {schedule.daily_budget_usd != null && ` of ${formatUsd(schedule.daily_budget_usd)}`}
              </span>
            )}
          </div>
        </div>
        <div className="ml-auto flex flex-wrap items-center justify-end gap-2">
          <label className="flex h-11 cursor-pointer items-center gap-2 text-xs text-muted-foreground sm:h-9">
            <Switch checked={enabled} onChange={onToggle} disabled={switching} label={`Enable ${schedule.name}`} />
            <span className="w-6" aria-hidden="true">{enabled ? "On" : "Off"}</span>
          </label>
          <Button
            type="button"
            variant="outline"
            size="sm"
            onClick={onRunNow}
            disabled={starting}
            aria-label={`Run now: ${schedule.name}`}
            className="h-11 sm:h-8"
          >
            {starting ? (
              <Loader2 className="h-3.5 w-3.5 animate-spin" aria-hidden="true" />
            ) : (
              <Play className="h-3.5 w-3.5" aria-hidden="true" />
            )}
            Run now
          </Button>
          <DeleteSchedule schedule={schedule} onDeleted={onDeleted} />
        </div>
        {notice && <NoticeText notice={notice} />}
      </div>
    </li>
  );
}

/** `#schedules`: every schedule, filtered by the hash query (`?project=<id>&enabled=true|false`); `#schedules/<id>`:
 * one, with the runs it fired. `onNavigate` changes the address. */
export function SchedulesPage({
  sub,
  query,
  onNavigate,
}: {
  sub: string;
  query: string;
  onNavigate: (sub: string, query?: string) => void;
}) {
  const scheduleId = decodeURIComponent(sub.split("/")[0] ?? "");
  return scheduleId ? (
    <ScheduleDetail key={scheduleId} scheduleId={scheduleId} onDeleted={() => onNavigate("")} />
  ) : (
    <ScheduleList query={query} onFilter={(next) => onNavigate("", next)} />
  );
}

/**
 * Every project's schedules, the disabled ones too, by project: each with its switch, "Run now" and "Delete schedule",
 * its name opening its detail view; it is edited on its project's Schedules tab. A project and a status filter, kept
 * in the address (`query`), narrow the list; `onFilter` sets them.
 */
function ScheduleList({ query, onFilter }: { query: string; onFilter: (query: string) => void }) {
  const id = useId();
  const [schedules, setSchedules] = useState<ScheduleInfo[] | null>(null);
  const [error, setError] = useState("");
  const { projects } = useProjects();
  /** The enabled value being saved, by schedule. */
  const [switching, setSwitching] = useState<Record<string, boolean>>({});
  /** Schedules whose "Run now" is on its way. */
  const [starting, setStarting] = useState<Record<string, boolean>>({});
  const { notices, notify } = useNotices();
  const latest = useRef(0);

  const load = useCallback(() => {
    // A slow poll must not overwrite a newer answer, nor one this page just wrote.
    const mine = ++latest.current;
    fetchAllSchedules()
      .then((next) => {
        if (mine !== latest.current) return;
        setSchedules(next);
        setError("");
      })
      .catch((err) => mine === latest.current && setError(errorText(err)));
  }, []);

  useEffect(() => {
    load();
    const timer = setInterval(load, REFRESH_MS);
    return () => clearInterval(timer);
  }, [load]);

  /** Put the server's answer for one schedule into the list. */
  const put = (next: ScheduleInfo) => {
    latest.current++;
    setSchedules((list) => list && list.map((s) => (s.schedule_id === next.schedule_id ? next : s)));
  };

  const toggle = async (schedule: ScheduleInfo, enabled: boolean) => {
    const sid = schedule.schedule_id;
    setSwitching((s) => ({ ...s, [sid]: enabled }));
    notify(sid, null);
    try {
      put(await updateSchedule(sid, { enabled }));
      notify(sid, { text: enabled ? "Turned on" : "Turned off", tone: "ok" });
    } catch (err) {
      notify(sid, { text: `Could not turn it ${enabled ? "on" : "off"}: ${errorText(err)}`, tone: "error" });
    } finally {
      setSwitching((s) => omit(s, sid));
    }
  };

  /** Take a deleted schedule out of the list; a poll already on its way cannot bring it back. */
  const drop = (sid: string) => {
    latest.current++;
    setSchedules((list) => list && list.filter((s) => s.schedule_id !== sid));
  };

  const runNow = async (schedule: ScheduleInfo) => {
    const sid = schedule.schedule_id;
    setStarting((s) => ({ ...s, [sid]: true }));
    notify(sid, null);
    try {
      notify(sid, runNotice(await runSchedule(sid)));
      fetchSchedule(sid).then(put, () => {});
    } catch (err) {
      notify(sid, { text: errorText(err), tone: "error" });
    } finally {
      setStarting((s) => omit(s, sid));
    }
  };

  const groups = byProject(schedules ?? []);
  const total = schedules?.length ?? 0;
  // Every project is a choice (its schedules' groups until the project list has loaded); an unknown one is all.
  const options = projects
    ? projects.map((p) => ({ projectId: p.project_id, name: p.name })).sort(byLowerName)
    : groups.map((g) => ({ projectId: g.projectId, name: g.name }));
  const wanted = readFilters(query);
  const project = options.some((o) => o.projectId === wanted.project) ? wanted.project : "";
  const status = wanted.status;
  const filtered = project !== "" || status !== "all";
  const matches = (s: ScheduleInfo) => status === "all" || s.enabled === (status === "enabled");
  // A group keeps all its schedules for its "N of M on", and shows those that match.
  const shown = groups
    .filter((g) => !project || g.projectId === project)
    .map((g) => ({ ...g, visible: g.schedules.filter(matches) }))
    .filter((g) => g.visible.length > 0);
  const shownCount = shown.reduce((n, g) => n + g.visible.length, 0);

  return (
    <div className="space-y-6 p-6">
      <div className="flex flex-wrap items-end justify-between gap-3">
        <div className="min-w-0">
          <h1 className="text-2xl font-bold">Schedules</h1>
          <p className="mt-1 text-sm text-muted-foreground">
            {total > 0 &&
              `${onCount(schedules ?? [])} of ${total} on, across ${groups.length} ${groups.length === 1 ? "project" : "projects"}. `}
            Turn a schedule on or off or run it now; open one to see the runs it fired.
          </p>
        </div>
      </div>

      {schedules && (
        <div role="group" aria-label="Filters" className="flex flex-wrap items-end gap-3">
          <div className="w-full space-y-1 sm:w-56">
            <label htmlFor={`${id}-project`} className={LABEL}>
              Project
            </label>
            <select
              id={`${id}-project`}
              value={project}
              onChange={(e) => onFilter(filterQuery(e.target.value, status))}
              className={SELECT}
            >
              <option value="">All projects</option>
              {options.map((o) => (
                <option key={o.projectId} value={o.projectId}>
                  {o.name}
                </option>
              ))}
            </select>
          </div>
          <div className="w-full space-y-1 sm:w-44">
            <label htmlFor={`${id}-status`} className={LABEL}>
              Status
            </label>
            <select
              id={`${id}-status`}
              value={status}
              onChange={(e) => onFilter(filterQuery(project, e.target.value as Status))}
              className={SELECT}
            >
              <option value="all">All</option>
              <option value="enabled">Enabled</option>
              <option value="disabled">Disabled</option>
            </select>
          </div>
          {filtered && (
            <div className="flex min-h-9 items-center gap-3 text-xs text-muted-foreground">
              <span>
                Showing {shownCount} of {total}
              </span>
              <Button type="button" variant="ghost" size="sm" onClick={() => onFilter("")} className="h-11 sm:h-8">
                Clear filters
              </Button>
            </div>
          )}
        </div>
      )}

      {!schedules ? (
        error ? (
          <div className="flex flex-wrap items-center gap-3">
            <p role="alert" className="text-sm text-destructive">Could not load the schedules: {error}</p>
            <Button type="button" variant="outline" size="sm" onClick={load}>
              Retry
            </Button>
          </div>
        ) : (
          <div className="flex h-24 items-center justify-center">
            <Loader2 className="h-5 w-5 animate-spin text-muted-foreground" aria-label="Loading" />
          </div>
        )
      ) : (
        <div className="space-y-6">
          {error && <p role="alert" className="text-xs text-destructive">Could not refresh the schedules: {error}</p>}
          {total === 0 && (
            <p className="rounded-lg border border-dashed border-border px-5 py-6 text-center text-sm text-muted-foreground">
              No schedules yet. A schedule belongs to a project: add one on the project&apos;s Schedules tab.
            </p>
          )}
          {total > 0 && shownCount === 0 && (
            <div className="flex flex-col items-center gap-3 rounded-lg border border-dashed border-border px-5 py-6 text-center text-sm text-muted-foreground">
              <p>No schedules match these filters.</p>
              <Button type="button" variant="outline" size="sm" onClick={() => onFilter("")}>
                Clear filters
              </Button>
            </div>
          )}
          {shown.map((group) => (
            <section key={group.projectId} aria-labelledby={`${id}-${group.projectId}`} className="space-y-2">
              <div className="flex flex-wrap items-baseline justify-between gap-x-3 gap-y-1">
                <h2 id={`${id}-${group.projectId}`} className="min-w-0 truncate text-sm font-semibold">
                  {group.name}
                </h2>
                <span className="flex items-center gap-3 text-xs text-muted-foreground">
                  {onCount(group.schedules)} of {group.schedules.length} on
                  <a
                    href={projectHref(group.projectId)}
                    className={cn("flex min-h-11 items-center gap-1 text-primary sm:min-h-0", linkClass)}
                  >
                    Add or edit on the project
                    <ArrowRight className="h-3 w-3" aria-hidden="true" />
                  </a>
                </span>
              </div>
              <ul className="space-y-2">
                {group.visible.map((schedule) => {
                  const sid = schedule.schedule_id;
                  return (
                    <ScheduleRow
                      key={sid}
                      schedule={schedule}
                      enabled={switching[sid] ?? schedule.enabled}
                      switching={sid in switching}
                      starting={!!starting[sid]}
                      notice={notices[sid]}
                      onToggle={(on) => toggle(schedule, on)}
                      onRunNow={() => runNow(schedule)}
                      onDeleted={() => drop(sid)}
                    />
                  );
                })}
              </ul>
            </section>
          ))}
        </div>
      )}
    </div>
  );
}
