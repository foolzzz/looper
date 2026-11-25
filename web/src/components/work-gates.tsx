import { GateBadge } from "@/components/task-gates";
import { Badge } from "@/components/ui/badge";
import { isActiveTask, type TaskDetail } from "@/lib/api";
import { GATE_STATES } from "@/lib/gates";

/** Summaries share the page's detail; the conversation owns every gate editor and thread. */
export function WorkGates({ task, onOpenGate }: { task: TaskDetail; onOpenGate: (step: string) => void }) {
  const gates = task.gates ?? [];
  return <section aria-label="关卡" className="space-y-3 p-4">
    <h2 className="text-sm font-semibold">关卡</h2>
    {!gates.length ? <p className="text-sm text-muted-foreground">没有关卡</p> : <ul className="space-y-2">
      {gates.map(gate => <li key={gate.step_id}>
        <button type="button" disabled={!gate.step}
          onClick={() => onOpenGate(gate.step)}
          onKeyDown={event => {
            if ((event.key === "Enter" || event.key === " ") && gate.step) {
              event.preventDefault(); onOpenGate(gate.step);
            }
          }}
          className="w-full space-y-1 rounded-md border border-border p-3 text-left text-sm [overflow-wrap:anywhere] pointer-coarse:min-h-11 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring disabled:opacity-50">
          <span className="flex flex-wrap items-center gap-2">
            <span className="font-mono text-xs text-muted-foreground">{gate.step}</span>
            {Object.hasOwn(GATE_STATES, gate.state) && (gate.state !== "discussing" || gate.waiting_on === "user" || gate.waiting_on === "planner")
              ? <GateBadge gate={gate} active={isActiveTask(task)} /> : <Badge variant="outline">状态未知</Badge>}
          </span>
          <span className="line-clamp-2 block">{gate.topic}</span>
          {!gate.step && <span className="block text-destructive">关卡已更新，请刷新</span>}
        </button>
      </li>)}
    </ul>}
  </section>;
}
