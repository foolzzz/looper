import { useEffect, useState } from "react";
import { fetchProjectsList, fetchRegistry } from "@/lib/api";
import { startPolling } from "@/lib/polling";
import {
  fetchTaskMetrics,
  type Compared,
  type FigureError,
  type MeanCost,
  type Ratio,
  type Spread,
  type TaskMetrics,
} from "@/lib/task-metrics";
import { formatUsd } from "@/lib/usage";

const WINDOW_DAYS = 7;
const COMPARISON_DAYS = 28;
const REFRESH_MS = 60_000;

const isError = (item: unknown): item is FigureError =>
  typeof item === "object" && item !== null && "error" in item;

const percent = (value: number) => `${Math.round(value * 100)}%`;
const insufficient = (n: number) => `样本不足 (n=${n})`;

/** A ratio: its share, or that the sample is too small (fewer than 20 cases); never a percentage below that. */
export function ratioText(ratio: Ratio): string {
  return ratio.insufficient ? insufficient(ratio.n) : percent(ratio.value);
}

function interval(ratio: Ratio): string | undefined {
  if (ratio.insufficient || !ratio.wilson) return undefined;
  return `95% 区间 ${percent(ratio.wilson[0])}–${percent(ratio.wilson[1])} · n=${ratio.n}`;
}

/** 2 min 5 s style; a dash for no sample. */
export function durationText(ms: number | null): string {
  if (ms === null) return "–";
  const seconds = Math.round(ms / 1000);
  if (seconds < 60) return `${seconds} s`;
  const minutes = Math.floor(seconds / 60);
  if (minutes < 60) return `${minutes} min`;
  return `${Math.floor(minutes / 60)} h ${minutes % 60} min`;
}

/** What a case cost on average; the cases with an unknown cost are named, never counted as 0. */
export function meanCostText(cost: MeanCost): string {
  if (cost.mean_usd === null) return cost.unknown > 0 ? `未知（${cost.unknown} 个）` : "–";
  return cost.unknown > 0 ? `${formatUsd(cost.mean_usd)}（另有 ${cost.unknown} 个未知）` : formatUsd(cost.mean_usd);
}

function Tile({ label, value, detail, previous, alert }: {
  label: string;
  value: string;
  detail?: string;
  previous?: string;
  alert?: string;
}) {
  return (
    <div className="rounded-lg border border-border px-4 py-3">
      <p className="text-xs text-muted-foreground">{label}</p>
      <p className="mt-1 break-words text-xl font-bold tabular-nums">{value}</p>
      {detail && <p className="mt-0.5 text-xs text-muted-foreground">{detail}</p>}
      {previous && <p className="mt-0.5 text-xs text-muted-foreground">前 {COMPARISON_DAYS} 天：{previous}</p>}
      {alert && <p role="alert" className="mt-0.5 text-xs text-destructive">读取失败：{alert}</p>}
    </div>
  );
}

/** A tile for a figure of the answer: its error shows in the tile and the others stay. */
function Figure<T>({ label, item, value, previous, detail }: {
  label: string;
  item: Compared<T> | undefined;
  value: (v: T) => string;
  previous?: (v: T) => string;
  detail?: (v: T) => string | undefined;
}) {
  if (item === undefined || isError(item)) return <Tile label={label} value="—" alert={item?.error ?? "没有返回"} />;
  const before = item.previous;
  return (
    <Tile
      label={label}
      value={value(item)}
      detail={detail?.(item)}
      previous={before && !isError(before) ? (previous ?? value)(before) : undefined}
    />
  );
}

const selectClass =
  "rounded-md border border-border bg-transparent px-2 py-1 text-xs pointer-coarse:min-h-11 pointer-coarse:text-base " +
  "focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring";

type Option = { id: string; name: string };

function Switch({ label, value, options, onChange }: {
  label: string;
  value: string;
  options: Option[];
  onChange: (value: string) => void;
}) {
  return (
    <label className="inline-flex items-center gap-1.5 text-xs text-muted-foreground">
      {label}
      <select aria-label={label} value={value} onChange={(e) => onChange(e.target.value)} className={selectClass}>
        <option value="">全部</option>
        {options.map((o) => (
          <option key={o.id} value={o.id}>{o.name}</option>
        ))}
      </select>
    </label>
  );
}

function Figures({ metrics }: { metrics: TaskMetrics }) {
  const { tasks, steps, reviews } = metrics;
  const spread = (s: Spread) => `${durationText(s.p50_ms)} / ${durationText(s.p95_ms)}`;
  return (
    <div className="grid grid-cols-1 gap-3 sm:grid-cols-2 lg:grid-cols-4">
      <Figure label="任务成功率" item={tasks.success_rate} value={ratioText} detail={interval} />
      <Figure label="一次通过率" item={steps.first_pass_rate} value={ratioText} detail={interval} />
      <Figure
        label="每个被接受步骤的尝试次数"
        item={steps.attempts_per_accepted_step}
        value={(s) => (s.mean === null ? "–" : s.mean.toFixed(2))}
        detail={(s) => `${s.n} 个被接受的步骤`}
      />
      <Figure label="评审捕获率" item={reviews.catch_rate} value={ratioText} detail={interval} />
      <Figure
        label="每个被接受步骤的花费"
        item={steps.cost_per_accepted_step}
        value={(c) => `供应商 ${meanCostText(c.provider_priced.total)}`}
        detail={(c) => `等价 ${meanCostText(c.equivalent.total)} · 首次 ${meanCostText(c.provider_priced.first)} / 重试 ${meanCostText(c.provider_priced.retry)}`}
        previous={(c) => `供应商 ${meanCostText(c.provider_priced.total)} · 等价 ${meanCostText(c.equivalent.total)}`}
      />
      <Figure
        label="每个完成任务的花费"
        item={tasks.cost_per_completed_task}
        value={(c) => `供应商 ${meanCostText(c.provider_priced)}`}
        detail={(c) => `等价 ${meanCostText(c.equivalent)} · ${c.n} 个完成的任务`}
        previous={(c) => `供应商 ${meanCostText(c.provider_priced)} · 等价 ${meanCostText(c.equivalent)}`}
      />
      <Figure
        label="agent 时长 P50 / P95"
        item={tasks.agent_duration}
        value={spread}
        detail={(s) => `n=${s.n}`}
      />
      <Figure label="评审漏检数" item={reviews.misses} value={(m) => String(m.count)} />
    </div>
  );
}

/** The 「任务质量」 group of the dashboard (`metrics.task_outcomes`): the last 7 days against the 28 before, read on its
 * own so that a failed read shows here and leaves the rest of the page alone. */
export function TaskQualitySection() {
  const [project, setProject] = useState("");
  const [profile, setProfile] = useState("");
  const [projects, setProjects] = useState<Option[]>([]);
  const [profiles, setProfiles] = useState<Option[]>([]);
  const [metrics, setMetrics] = useState<TaskMetrics | null>(null);
  const [error, setError] = useState("");

  useEffect(() => {
    let alive = true;
    // The switches' choices are a convenience: without them the group still reads everything.
    fetchProjectsList()
      .then((l) => alive && setProjects((l?.projects ?? []).map((p) => ({ id: p.project_id, name: p.name }))))
      .catch(() => {});
    fetchRegistry()
      .then((r) => alive && setProfiles((r?.profiles ?? []).map((p) => ({ id: p.profile_id, name: p.name }))))
      .catch(() => {});
    return () => {
      alive = false;
    };
  }, []);

  useEffect(() => {
    return startPolling(async (signal) => {
      await fetchTaskMetrics(WINDOW_DAYS, { project, profile }, signal)
        .then((m) => {
          if (signal.aborted) return;
          setMetrics(m);
          setError("");
        })
        .catch((e) => {
          if (!signal.aborted) setError(e instanceof Error ? e.message : String(e));
        });
    }, REFRESH_MS);
  }, [project, profile]);

  const choose = (set: (v: string) => void) => (value: string) => {
    set(value);
    setMetrics(null); // another selection's figures are not these
    setError("");
  };

  return (
    <section aria-label="任务质量" className="rounded-lg border border-border bg-card px-5 py-4">
      <div className="flex flex-wrap items-baseline justify-between gap-x-3 gap-y-2">
        <h2 className="text-sm font-semibold">任务质量</h2>
        <span className="flex flex-wrap items-center gap-x-3 gap-y-1 text-xs text-muted-foreground">
          最近 {WINDOW_DAYS} 天，对照前 {COMPARISON_DAYS} 天
          <Switch label="项目" value={project} options={projects} onChange={choose(setProject)} />
          <Switch label="Profile" value={profile} options={profiles} onChange={choose(setProfile)} />
        </span>
      </div>
      <div className="mt-3 space-y-3">
        {error && (
          <p role="alert" className="text-xs text-destructive">
            读取任务质量失败（{error}）{metrics ? "；显示的是上次读到的数字。" : "。"}
          </p>
        )}
        {!metrics ? (
          !error && <p className="text-sm text-muted-foreground">读取中…</p>
        ) : (
          <>
            <Figures metrics={metrics} />
            <p className="text-xs text-muted-foreground">
              {metrics.coverage_since ? `记录始于 ${new Date(metrics.coverage_since).toLocaleDateString()}，更早的任务没有回填。` : "还没有记录。"}
              {" "}比率不足 20 个样本时不给百分比。
            </p>
            {!!metrics.missing_attempts && (
              <p role="alert" className="text-xs text-warning">
                有 {metrics.missing_attempts} 个 Worker 运行没有记下尝试（写入失败），数字可能偏低。
              </p>
            )}
          </>
        )}
      </div>
    </section>
  );
}
