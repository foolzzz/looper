import { CirclePause, Hourglass, ShieldCheck, Terminal, type LucideIcon } from "lucide-react";
import { GateBadge } from "@/components/task-gates";
import { WaitBadge } from "@/components/task-waits";
import { isActiveTask, type TaskDetail, type TaskStep, type UserGate } from "@/lib/api";

/** The task page's Steps summary: verified is done, completed stays in review, and a skipped step or a cancelled
 * gate stays out of the denominator. Status is matched exactly. */
const WORK_STATUS: Record<string, string> = {
  pending: "未开始",
  dispatched: "已派出",
  running: "运行中",
  deciding: "待决定",
  waiting_user: "等你处理",
  consulting: "请教中",
  completed: "评审中",
  verified: "已完成",
  failed: "失败",
  skipped: "已跳过",
};

const KIND_ICON: Record<TaskStep["kind"], LucideIcon> = {
  work: Terminal,
  gate: CirclePause,
  wait: Hourglass,
  review: ShieldCheck,
};

const PREVIEW = 80;

function workStatusText(status: string): string {
  return Object.hasOwn(WORK_STATUS, status) ? WORK_STATUS[status] : `未知状态 ${status}`;
}

function cancelledGate(step: TaskStep, gates: Map<string, UserGate>): boolean {
  return step.kind === "gate" && gates.get(step.id)?.state === "cancelled";
}

function progressText(steps: TaskStep[], gates: Map<string, UserGate>): string {
  const planned = steps.filter((step) => step.status !== "skipped" && !cancelledGate(step, gates));
  const done = planned.filter((step) => step.status === "verified").length;
  const inReview = planned.filter((step) => step.status === "completed").length;
  const dropped = steps.filter((step) => step.status === "skipped").length;
  const cancelled = steps.filter((step) => cancelledGate(step, gates)).length;
  return [
    `${done}/${planned.length} 已完成`,
    inReview > 0 && `${inReview} 评审中`,
    dropped > 0 && `${dropped} 已跳过`,
    cancelled > 0 && `${cancelled} 个关卡已取消`,
  ].filter(Boolean).join(" · ");
}

/** The pipeline card the task page already opens for a step. The path keeps the task id; `step` is that page's
 * step id. The visible name is the step key. */
export function workStepHref(taskId: string, stepId: string): string {
  const query = new URLSearchParams({ tab: "pipeline", step: stepId });
  return `#tasks/${encodeURIComponent(taskId)}?${query}`;
}

function KindMark({ kind }: { kind: string }) {
  const Icon = Object.hasOwn(KIND_ICON, kind) ? KIND_ICON[kind as TaskStep["kind"]] : null;
  return (
    <span className="inline-flex shrink-0 items-center gap-1 text-xs text-muted-foreground">
      {Icon && <Icon className="h-4 w-4" aria-hidden="true" />}
      <span>{kind}</span>
    </span>
  );
}

function ExpandableInstruction({ text }: { text: string }) {
  const long = text.length > PREVIEW || text.includes("\n");
  const body = "whitespace-pre-wrap break-words [overflow-wrap:anywhere]";
  if (!long) return <p className={`mt-1 text-sm ${body}`}>{text}</p>;
  const preview = `${text.slice(0, PREVIEW).replace(/\s+/g, " ").trimEnd()}…`;
  return (
    <details data-instruction className="group mt-1 min-w-0">
      <summary className="cursor-pointer text-sm focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring">
        <span className={body}>{preview}</span>
        <span className="ml-1 text-xs text-primary group-open:hidden">展开</span>
      </summary>
      <p data-instruction-full className={`mt-1 text-sm ${body}`}>{text}</p>
    </details>
  );
}

const CONTROL = "max-w-full text-left font-mono text-sm text-primary underline-offset-2 hover:underline focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring pointer-coarse:inline-flex pointer-coarse:min-h-11 pointer-coarse:items-center [overflow-wrap:anywhere]";

/** Compact plan for the Work column. It only reads the task it is given. */
export function WorkSteps({ task, onOpenGate }: {
  task: TaskDetail;
  onOpenGate: (step: TaskStep) => void;
}) {
  const steps = [...(task.steps ?? [])].sort((a, b) => a.step_index - b.step_index);
  const gates = new Map((task.gates ?? []).map((gate) => [gate.step_id, gate]));
  const waits = new Map((task.waits ?? []).map((wait) => [wait.step_id, wait]));
  const byId = new Map(steps.map((step) => [step.id, step]));
  const active = isActiveTask(task);

  return (
    <section id="work-steps" aria-labelledby="work-steps-title" data-work-steps="" className="min-w-0 max-w-full overflow-x-auto p-4">
      <h2 id="work-steps-title" className="text-sm font-semibold">计划与步骤</h2>
      {steps.length === 0 ? (
        <p className="mt-2 text-sm text-muted-foreground">尚无步骤</p>
      ) : (
        <>
          <p data-step-progress className="mt-1 text-xs text-muted-foreground">{progressText(steps, gates)}</p>
          <ol className="mt-3 min-w-0 divide-y divide-border overflow-x-auto rounded-lg border border-border bg-card">
            {steps.map((step) => {
              const gate = step.kind === "gate" ? gates.get(step.id) : undefined;
              const name = step.step_key || step.id;
              return (
                <li key={step.id} data-step-key={step.step_key} data-kind={step.kind} className="min-w-0 px-3 py-2">
                  <div className="flex min-w-0 flex-wrap items-center gap-x-2 gap-y-1">
                    <KindMark kind={step.kind} />
                    {step.kind === "gate" ? (
                      <button type="button" className={CONTROL} onClick={() => onOpenGate(step)}>{name}</button>
                    ) : (
                      <a className={CONTROL} href={workStepHref(task.task_id, step.id)}>{name}</a>
                    )}
                    {step.kind === "wait" ? <WaitBadge state={waits.get(step.id)?.state ?? "pending"} /> :
                      gate ? <GateBadge gate={gate} active={active} /> :
                        <span data-status-text className="text-xs">{workStatusText(step.status)}</span>}
                  </div>
                  <ExpandableInstruction text={step.instruction ?? ""} />
                  {(step.acceptance_criteria ?? []).length > 0 && (
                    <details data-acceptance className="mt-1 min-w-0">
                      <summary className="cursor-pointer text-xs text-muted-foreground focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring">验收</summary>
                      <ol className="mt-1 list-decimal space-y-1 pl-4 text-sm">
                        {(step.acceptance_criteria ?? []).map((item, index) => (
                          <li key={index} className="whitespace-pre-wrap break-words [overflow-wrap:anywhere]">{item}</li>
                        ))}
                      </ol>
                    </details>
                  )}
                  {(step.target_files ?? []).length > 0 && (
                    <ul className="mt-1 flex min-w-0 flex-wrap gap-1">
                      {(step.target_files ?? []).map((file, index) => (
                        <li key={`${index}:${file}`} data-target-file="" className="max-w-full rounded bg-muted px-1.5 py-0.5 font-mono text-xs text-muted-foreground [overflow-wrap:anywhere]">{file}</li>
                      ))}
                    </ul>
                  )}
                  <p className="mt-1 text-xs text-muted-foreground [overflow-wrap:anywhere]">
                    {(step.depends_on ?? []).length === 0 ? "无依赖" : `依赖 ${(step.depends_on ?? []).map((id) => byId.get(id)?.step_key || id).join("、")}`}
                  </p>
                </li>
              );
            })}
          </ol>
        </>
      )}
    </section>
  );
}
