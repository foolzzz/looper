import { useEffect, useState } from "react";
import { Loader2, Shrink } from "lucide-react";
import { fetchAgentUsage, type AgentTurn, type AgentUsage } from "@/lib/api";
import { compactionChange as change, formatTokens, formatUsd } from "@/lib/usage";

const POLL_MS = 10_000;
const TURNS = 100;
// The sparkline's drawing box; the SVG stretches it to the panel's width.
const W = 600;
const H = 64;

function formatClock(iso: string) {
  const date = new Date(iso);
  return Number.isNaN(date.getTime()) ? iso : date.toLocaleString([], { hour12: false });
}

/**
 * The context at the end of each turn, oldest first, scaled between its lowest and highest value so
 * that growth shows; a gap where a turn made no model request.
 */
function ContextSparkline({ turns }: { turns: AgentTurn[] }) {
  const sizes = turns.map((t) => t.context_tokens).filter((n): n is number => n != null);
  if (!sizes.length) return <p className="text-xs text-muted-foreground">No context recorded yet.</p>;
  const max = Math.max(...sizes);
  const min = Math.min(...sizes);
  const step = turns.length > 1 ? W / (turns.length - 1) : 0;
  const x = (i: number) => (turns.length > 1 ? i * step : W / 2);
  const y = (tokens: number) => (max === min ? H / 2 : H - 4 - ((tokens - min) / (max - min)) * (H - 8));
  let path = "";
  let pen = false;
  turns.forEach((turn, i) => {
    if (turn.context_tokens == null) {
      pen = false;
      return;
    }
    path += `${pen ? "L" : "M"}${x(i).toFixed(1)},${y(turn.context_tokens).toFixed(1)} `;
    pen = true;
  });
  const first = turns.find((t) => t.context_tokens != null)?.context_tokens;
  const last = [...turns].reverse().find((t) => t.context_tokens != null)?.context_tokens;
  const slot = turns.length > 1 ? step : W;
  return (
    <div>
      <div className="flex justify-between font-mono text-[11px] text-muted-foreground">
        <span>
          {formatTokens(min)} – {formatTokens(max)} tokens
        </span>
        <span>context at the end of each turn</span>
      </div>
      <svg
        viewBox={`0 0 ${W} ${H}`}
        preserveAspectRatio="none"
        className="mt-1 h-16 w-full overflow-visible"
        role="img"
        aria-label={`Context over the last ${turns.length} turns: from ${formatTokens(first)} to ${formatTokens(last)} tokens`}
      >
        <line x1={0} x2={W} y1={H} y2={H} className="stroke-border" vectorEffect="non-scaling-stroke" />
        {turns.map((turn, i) =>
          turn.compactions?.length ? (
            <line
              key={`c${turn.id}`}
              x1={x(i)}
              x2={x(i)}
              y1={0}
              y2={H}
              className="stroke-warning"
              strokeDasharray="3 3"
              vectorEffect="non-scaling-stroke"
            />
          ) : null,
        )}
        <path d={path} fill="none" className="stroke-primary" strokeWidth={1.5} vectorEffect="non-scaling-stroke" />
        {turns.map((turn, i) => (
          <rect key={turn.id} x={x(i) - slot / 2} y={0} width={slot} height={H} fill="transparent">
            <title>
              {`Turn ${i + 1} · ${formatClock(turn.created_at)}\ncontext ${formatTokens(turn.context_tokens)} · ${formatUsd(turn.cost_usd)}${
                turn.compactions?.length ? `\ncompacted: ${turn.compactions.map(change).join(", ")}` : ""
              }`}
            </title>
          </rect>
        ))}
      </svg>
      <div className="mt-0.5 flex justify-between font-mono text-[11px] text-muted-foreground">
        <span>{formatClock(turns[0].created_at)}</span>
        <span>{formatClock(turns[turns.length - 1].created_at)}</span>
      </div>
    </div>
  );
}

/**
 * A resident agent's (the Manager's, a Planner's) context over its latest turns, with its compactions,
 * the latest context size, and what its turns cost. Keyed by the agent, so another agent starts empty.
 */
export function ContextUsagePanel({ agentId }: { agentId: string }) {
  const [usage, setUsage] = useState<AgentUsage | null>(null);
  const [error, setError] = useState("");

  useEffect(() => {
    let stopped = false;
    const load = () =>
      fetchAgentUsage(agentId, TURNS)
        .then((u) => {
          if (!stopped) {
            setUsage(u);
            setError("");
          }
        })
        .catch((e) => !stopped && setError(e instanceof Error ? e.message : String(e)));
    load();
    const timer = setInterval(load, POLL_MS);
    return () => {
      stopped = true;
      clearInterval(timer);
    };
  }, [agentId]);

  if (!usage) {
    return (
      <section className="space-y-2">
        <h2 className="text-sm font-semibold">Context &amp; cost</h2>
        {error ? (
          <p className="text-xs text-destructive">Could not load the context and cost: {error}</p>
        ) : (
          <Loader2 className="h-4 w-4 animate-spin text-muted-foreground" />
        )}
      </section>
    );
  }

  const { total, turns } = usage;
  const compacted = turns.flatMap((turn) =>
    (turn.compactions ?? []).map((c, k) => ({ key: `${turn.id}-${k}`, turn, index: turns.indexOf(turn), c })),
  );
  const stats: [string, string, string?][] = [
    ["Latest context", `${formatTokens(usage.latest_context_tokens)} tokens`, "The prompt of the latest turn's last model request"],
    ["Compactions", String(total.compactions), "Context compactions recorded over all of its turns"],
    ["Cost", formatUsd(total.cost_usd), "The sum of its turns' own costs"],
    [
      "Turns",
      `${total.runs}${total.running ? ` (${total.running} running)` : ""}`,
      total.unpriced_runs ? `${total.unpriced_runs} ended without a reported cost` : undefined,
    ],
  ];

  return (
    <section className="space-y-2">
      <h2 className="text-sm font-semibold">Context &amp; cost</h2>
      <div className="space-y-3 rounded-lg border border-border bg-card p-4">
        <dl className="grid grid-cols-2 gap-3 sm:grid-cols-4">
          {stats.map(([name, value, title]) => (
            <div key={name} title={title}>
              <dt className="text-xs text-muted-foreground">{name}</dt>
              <dd className="mt-0.5 font-mono text-sm font-medium">{value}</dd>
            </div>
          ))}
        </dl>
        {turns.length > 0 ? (
          <ContextSparkline turns={turns} />
        ) : (
          <p className="text-xs text-muted-foreground">No turns recorded yet.</p>
        )}
        {compacted.length > 0 && (
          <ul className="space-y-0.5">
            {compacted.map(({ key, turn, index, c }) => (
              <li key={key} className="flex items-center gap-1.5 text-xs text-muted-foreground">
                <Shrink className="h-3 w-3 shrink-0 text-warning" />
                <span>
                  Turn {index + 1} · {formatClock(turn.created_at)} · {c.trigger ?? "?"} compaction · {change(c)}
                </span>
              </li>
            ))}
          </ul>
        )}
        {turns.length > 0 && (
          <details className="text-xs">
            <summary className="cursor-pointer text-muted-foreground hover:text-foreground">
              Turns ({turns.length === total.runs ? "all" : `latest ${turns.length}`})
            </summary>
            <div className="mt-2 max-h-64 overflow-auto">
              <table className="w-full font-mono text-[11px]">
                <thead className="text-left text-muted-foreground">
                  <tr>
                    <th className="py-0.5 pr-3 font-normal">#</th>
                    <th className="py-0.5 pr-3 font-normal">Started</th>
                    <th className="py-0.5 pr-3 text-right font-normal">Context</th>
                    <th className="py-0.5 pr-3 text-right font-normal">Cost</th>
                    <th className="py-0.5 font-normal">Compactions</th>
                  </tr>
                </thead>
                <tbody>
                  {turns.map((turn, i) => (
                    <tr key={turn.id} className="border-t border-border">
                      <td className="py-0.5 pr-3">{i + 1}</td>
                      <td className="py-0.5 pr-3">{formatClock(turn.created_at)}</td>
                      <td className="py-0.5 pr-3 text-right">{formatTokens(turn.context_tokens)}</td>
                      <td className="py-0.5 pr-3 text-right">{turn.status === "running" ? "running" : formatUsd(turn.cost_usd)}</td>
                      <td className="py-0.5">{(turn.compactions ?? []).map(change).join(", ")}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </details>
        )}
      </div>
    </section>
  );
}
