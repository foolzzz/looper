import { useRef, useState } from "react";
import { Button } from "@/components/ui/button";
import {
  setTaskManagerProfile,
  type ProfileSetResult,
} from "@/lib/task-manager-profile";

const SOURCE_GLOSS: Record<string, string> = {
  builtin: "内置默认",
  default: "内置默认",
  role: "角色配置",
  task: "本任务",
};

export interface TaskManagerChoice {
  profile_id: string;
  name: string;
  model: string;
  runtime: string;
  provider: string;
  source: string;
  problem?: string | null;
  compatible?: boolean;
}

export interface TaskManagerEffective {
  profile_id: string;
  name: string;
  model: string;
  runtime: string;
  provider: string;
  source: string;
}

/** Choices and the effective profile. The parent supplies them; this control does not read. */
export interface TaskManagerSnapshot {
  choices: TaskManagerChoice[];
  effective: TaskManagerEffective | null;
  inherit: boolean;
  selectedProfileId: string | null;
}

type Outcome =
  | { kind: "saving" }
  | { kind: "busy"; message: string }
  | { kind: "invalid"; message: string }
  | { kind: "failure"; message: string }
  | { kind: "uncertain" }
  | { kind: "mismatch"; readId: string }
  | { kind: "readback-failed" }
  | { kind: "success" }
  | null;

type Pending = "write" | "read" | null;

type Props = {
  mode: "task";
  taskId: string;
  snapshot: TaskManagerSnapshot | null;
  onReread: () => Promise<TaskManagerSnapshot>;
};

function sourceLabel(source: string): string {
  if (!Object.hasOwn(SOURCE_GLOSS, source)) return source;
  return `${source}（${SOURCE_GLOSS[source]}）`;
}

function blockReason(choice: TaskManagerChoice): string | null {
  if (typeof choice.problem === "string" && choice.problem) return choice.problem;
  if (choice.compatible === false) return "此配置与任务管家不兼容";
  return null;
}

function viewMatches(requested: string | null, profile: TaskManagerSnapshot): boolean {
  if (!profile.effective) return false;
  if (requested === null) return profile.selectedProfileId === null;
  return profile.effective.profile_id === requested && profile.selectedProfileId === requested;
}

function outcomeFrom(result: ProfileSetResult): Outcome {
  if (result.ok || result.reason === "missing") return null;
  if (result.reason === "busy") return { kind: "busy", message: result.message };
  if (result.reason === "invalid") return { kind: "invalid", message: result.message };
  if (result.reason === "failure") return { kind: "failure", message: result.message };
  return { kind: "uncertain" };
}

/** Profile choices for one task. A null snapshot means the server has no read for this yet. */
export function WorkTaskManagerModel(props: Props) {
  const taskKey = props.taskId;
  const epoch = useRef(0);
  const writingRef = useRef(false);
  const [tracked, setTracked] = useState(taskKey);
  const [selection, setSelection] = useState(props.snapshot?.selectedProfileId ?? "");
  const [pending, setPending] = useState<Pending>(null);
  const [outcome, setOutcome] = useState<Outcome>(null);
  const [unavailable, setUnavailable] = useState(false);

  if (tracked !== taskKey) {
    epoch.current += 1;
    writingRef.current = false;
    setTracked(taskKey);
    setSelection(props.snapshot?.selectedProfileId ?? "");
    setPending(null);
    setOutcome(null);
    setUnavailable(false);
  }

  const snapshot = unavailable ? null : props.snapshot;
  const requested = selection === "" ? null : selection;
  const selectedChoice = snapshot?.choices.find((choice) => choice.profile_id === selection);
  const unconfirmed = outcome?.kind === "uncertain" || outcome?.kind === "readback-failed";
  const writesLocked = pending !== null || unconfirmed;
  const submittable = !!snapshot && snapshot.choices.length > 0 && !writesLocked && (
    (requested === null && snapshot.inherit) || (!!selectedChoice && !blockReason(selectedChoice))
  );

  function pick(profileId: string | null, blocked: boolean) {
    if (writesLocked || blocked) return;
    setSelection(profileId ?? "");
  }

  function save() {
    if (!snapshot || !submittable || writesLocked || writingRef.current) return;
    const currentTask = props.taskId;
    const wanted = requested;
    const token = epoch.current;
    writingRef.current = true;
    setPending("write");
    setOutcome({ kind: "saving" });
    void (async () => {
      try {
        const set = await setTaskManagerProfile(currentTask, wanted);
        if (token !== epoch.current) return;
        if (!set.ok) {
          if (set.reason === "missing") setUnavailable(true);
          setOutcome(outcomeFrom(set));
          return;
        }
        try {
          const next = await props.onReread();
          if (token !== epoch.current) return;
          if (viewMatches(wanted, next)) setOutcome({ kind: "success" });
          else setOutcome({ kind: "mismatch", readId: next.effective?.profile_id ?? "" });
        } catch {
          if (token !== epoch.current) return;
          setOutcome({ kind: "readback-failed" });
        }
      } catch {
        if (token !== epoch.current) return;
        setOutcome({ kind: "uncertain" });
      } finally {
        if (token === epoch.current) {
          writingRef.current = false;
          setPending(null);
        }
      }
    })();
  }

  function reread() {
    if (writingRef.current) return;
    const wanted = requested;
    const token = epoch.current;
    writingRef.current = true;
    setPending("read");
    void (async () => {
      try {
        const next = await props.onReread();
        if (token !== epoch.current) return;
        if (viewMatches(wanted, next)) setOutcome({ kind: "success" });
        else setOutcome({ kind: "mismatch", readId: next.effective?.profile_id ?? "" });
      } catch {
        if (token !== epoch.current) return;
      } finally {
        if (token === epoch.current) {
          writingRef.current = false;
          setPending(null);
        }
      }
    })();
  }

  const effective = snapshot?.effective;
  const draftDiffers = !!effective && requested !== effective.profile_id;

  if (!snapshot) return null;

  return (
    <section aria-label="任务管家模型" className="space-y-3 border-b border-border p-4">
      <h2 className="text-sm font-semibold">任务管家模型</h2>
      {snapshot && snapshot.choices.length === 0 && <p className="text-sm">没有可用的任务管家模型</p>}
      {snapshot && snapshot.choices.length > 0 && (
        <fieldset disabled={writesLocked} className="min-w-0 space-y-2 border-0 p-0">
          <legend className="sr-only">任务管家配置</legend>
          {snapshot.inherit && (
            <label data-choice="inherit" className="block rounded-md border border-border p-3 text-sm pointer-coarse:min-h-11 has-[:focus-visible]:ring-2 has-[:focus-visible]:ring-ring">
              <span className="flex items-center gap-2">
                <input
                  type="radio"
                  name={taskKey}
                  value=""
                  checked={selection === ""}
                  disabled={writesLocked}
                  onChange={() => pick(null, false)}
                  className="focus-visible:outline-none"
                />
                <span>跟随角色默认</span>
              </span>
            </label>
          )}
          {snapshot.choices.map((choice) => {
            const reason = blockReason(choice);
            return (
              <label key={choice.profile_id} data-choice={choice.profile_id} className="block rounded-md border border-border p-3 text-sm pointer-coarse:min-h-11 has-[:focus-visible]:ring-2 has-[:focus-visible]:ring-ring">
                <span className="flex items-start gap-2">
                  <input
                    type="radio"
                    name={taskKey}
                    value={choice.profile_id}
                    checked={selection === choice.profile_id}
                    disabled={!!reason || writesLocked}
                    onChange={() => pick(choice.profile_id, !!reason)}
                    className="mt-1 focus-visible:outline-none"
                  />
                  <span className="min-w-0 space-y-1 [overflow-wrap:anywhere]">
                    <span className="block font-medium">{choice.name}</span>
                    <span className="block font-mono text-xs text-muted-foreground">{choice.profile_id}</span>
                    {choice.source ? <span className="block">来源 {sourceLabel(choice.source)}</span> : null}
                    <span className="block">模型 {choice.model}</span>
                    <span className="block">运行时 {choice.runtime}</span>
                    {choice.provider ? <span className="block">提供商 {choice.provider}</span> : null}
                    {reason && <span className="block text-destructive">{reason}</span>}
                  </span>
                </span>
              </label>
            );
          })}
        </fieldset>
      )}
      {effective && (
        <div className="space-y-1 text-sm [overflow-wrap:anywhere]">
          <p>配置 <span data-field="profile-id">{effective.profile_id}</span></p>
          <p>模型 <span data-field="model">{effective.model}</span></p>
          <p>运行时 <span data-field="runtime">{effective.runtime}</span></p>
          <p>提供商 <span data-field="provider">{effective.provider}</span></p>
          <p>来源 <span data-field="source">{sourceLabel(effective.source)}</span></p>
        </div>
      )}
      {draftDiffers && (
        <p className="text-sm">当前选择与有效配置不同。当前选择 {requested ?? "跟随角色默认"}。有效配置 {effective?.profile_id}。</p>
      )}
      {snapshot && (
        <Button type="button" variant="outline" disabled={!submittable} onClick={save}>切换模型</Button>
      )}
      {outcome?.kind === "saving" && <p role="status">正在切换…</p>}
      {outcome?.kind === "busy" && <p role="alert" className="text-sm text-destructive">请先停止当前回复再切换模型 {outcome.message}</p>}
      {outcome?.kind === "invalid" && <p role="alert" className="text-sm text-destructive">{outcome.message}</p>}
      {outcome?.kind === "failure" && <p role="alert" className="text-sm text-destructive">切换失败 {outcome.message}</p>}
      {(outcome?.kind === "uncertain" || outcome?.kind === "readback-failed") && (
        <div role="alert" className="space-y-2">
          <p className="text-sm text-destructive">{outcome.kind === "uncertain" ? "切换结果尚未确认" : "模型读取失败"}</p>
          <Button type="button" variant="outline" onClick={reread} disabled={pending !== null}>重新读取</Button>
        </div>
      )}
      {outcome?.kind === "mismatch" && <p role="alert" className="text-sm text-destructive">读回的配置是 {outcome.readId}</p>}
      {outcome?.kind === "success" && <p role="status">已按读回更新</p>}
    </section>
  );
}
