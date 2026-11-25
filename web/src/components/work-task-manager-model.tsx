import { useEffect, useId, useRef, useState } from "react";
import { Button } from "@/components/ui/button";
import {
  readTaskManagerProfile,
  setTaskManagerProfile,
  type ProfileSetResult,
  type TaskManagerChoice,
  type TaskManagerProfileView,
  type TaskManagerResume,
} from "@/lib/task-manager-profile";

const SOURCE_GLOSS: Record<string, string> = {
  builtin: "内置默认",
  role: "角色配置",
  task: "本任务",
};

type Phase = "loading" | "ready" | "empty" | "missing" | "error";
type Pending = "write" | "read" | null;
type Outcome =
  | { kind: "saving" }
  | { kind: "busy" }
  | { kind: "runtime"; message: string; requested: string | null; supported: string | null }
  | { kind: "invalid"; message: string }
  | { kind: "failure"; message: string }
  | { kind: "uncertain" }
  | { kind: "mismatch"; readId: string }
  | { kind: "readback-failed" }
  | { kind: "success" }
  | null;

type Props =
  | { mode: "task"; taskId: string }
  | { mode: "create"; onChange: (profileId: string | null) => void };

function sourceLabel(source: string): string {
  if (!Object.hasOwn(SOURCE_GLOSS, source)) return source;
  return `${source}（${SOURCE_GLOSS[source]}）`;
}

function blockReason(choice: TaskManagerChoice): string | null {
  if (typeof choice.problem === "string" && choice.problem) return choice.problem;
  if (choice.compatible === false) return "此 Profile 与任务管家不兼容";
  return null;
}

function viewMatches(requested: string | null, profile: TaskManagerProfileView): boolean {
  if (!profile.effective) return false;
  if (requested === null) return profile.selected_profile_id === null;
  return profile.effective.profile_id === requested && profile.selected_profile_id === requested;
}

function isAbort(err: unknown): boolean {
  return typeof err === "object" && err !== null && "name" in err && (err as { name: string }).name === "AbortError";
}

function continuity(resume: TaskManagerResume | undefined): string | null {
  if (!resume) return null;
  if (resume.session_fresh && typeof resume.session_fresh.reason === "string") {
    return `原会话续不上，已用摘要重开 ${resume.session_fresh.reason}`;
  }
  if (resume.resumed === true && typeof resume.session_id === "string" && resume.session_id && !resume.session_fresh) {
    return `读回：会话 ${resume.session_id} 续接`;
  }
  if ("resumed" in resume || "session_id" in resume || "session_fresh" in resume) return "服务端未说明会话是否续接";
  return null;
}

function historyText(history: unknown): string {
  return typeof history === "string" ? history : JSON.stringify(history);
}

/** Profile choices for one task, or a draft id for a task that is not created yet. */
export function WorkTaskManagerModel(props: Props) {
  const taskId = props.mode === "task" ? props.taskId : null;
  const identity = taskId ?? "create";
  const group = useId();
  const epoch = useRef(0);
  const abortRef = useRef<AbortController | null>(null);
  const writingRef = useRef(false);
  const [tracked, setTracked] = useState(identity);
  const [reload, setReload] = useState(0);
  const [phase, setPhase] = useState<Phase>("loading");
  const [view, setView] = useState<TaskManagerProfileView | null>(null);
  const [selection, setSelection] = useState("");
  const [pending, setPending] = useState<Pending>(null);
  const [outcome, setOutcome] = useState<Outcome>(null);

  if (tracked !== identity) {
    abortRef.current?.abort();
    epoch.current += 1;
    writingRef.current = false;
    setTracked(identity);
    setPhase("loading");
    setView(null);
    setSelection("");
    setPending(null);
    setOutcome(null);
  }

  useEffect(() => {
    const token = ++epoch.current;
    const controller = new AbortController();
    abortRef.current = controller;
    void (async () => {
      setPhase("loading");
      try {
        const result = await readTaskManagerProfile(taskId, controller.signal);
        if (token !== epoch.current) return;
        if (!result.ok) {
          setView(null);
          setPhase(result.reason === "missing" ? "missing" : "error");
          return;
        }
        setView(result.view);
        setSelection(result.view.selected_profile_id ?? "");
        setOutcome(null);
        setPhase(result.view.choices.length ? "ready" : "empty");
      } catch (err) {
        if (token !== epoch.current || controller.signal.aborted || isAbort(err)) return;
        setPhase("error");
      }
    })();
    return () => {
      epoch.current += 1;
      controller.abort();
    };
  }, [identity, reload, taskId]);

  const requested = selection === "" ? null : selection;
  const selectedChoice = view?.choices.find((choice) => choice.profile_id === selection);
  const submittable = phase === "ready" && !!view && pending === null && (
    (requested === null && view.inherit) || (!!selectedChoice && !blockReason(selectedChoice))
  );

  function pick(profileId: string | null, blocked: boolean) {
    if (pending || blocked) return;
    setSelection(profileId ?? "");
    if (props.mode === "create") props.onChange(profileId);
  }

  function save() {
    if (props.mode !== "task" || !submittable || writingRef.current) return;
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
          if (set.reason === "missing") setPhase("missing");
          else setOutcome(outcomeFrom(set));
          return;
        }
        let read;
        try {
          read = await readTaskManagerProfile(currentTask);
        } catch (err) {
          if (token !== epoch.current || isAbort(err)) return;
          setOutcome({ kind: "readback-failed" });
          return;
        }
        if (token !== epoch.current) return;
        if (!read.ok) {
          if (read.reason === "missing") setPhase("missing");
          else setOutcome({ kind: "readback-failed" });
          return;
        }
        if (!viewMatches(wanted, read.view)) {
          setOutcome({ kind: "mismatch", readId: read.view.effective?.profile_id ?? "" });
          return;
        }
        setView(read.view);
        setSelection(read.view.selected_profile_id ?? "");
        setPhase(read.view.choices.length ? "ready" : "empty");
        setOutcome({ kind: "success" });
      } catch (err) {
        if (token !== epoch.current || isAbort(err)) return;
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
    if (props.mode !== "task" || writingRef.current) return;
    const currentTask = props.taskId;
    const wanted = requested;
    const token = epoch.current;
    writingRef.current = true;
    setPending("read");
    void (async () => {
      try {
        const read = await readTaskManagerProfile(currentTask);
        if (token !== epoch.current) return;
        if (!read.ok) {
          if (read.reason === "missing") setPhase("missing");
          return;
        }
        if (viewMatches(wanted, read.view)) {
          setView(read.view);
          setSelection(read.view.selected_profile_id ?? "");
          setPhase(read.view.choices.length ? "ready" : "empty");
          setOutcome({ kind: "success" });
          return;
        }
        setView(read.view);
        setOutcome({ kind: "mismatch", readId: read.view.effective?.profile_id ?? "" });
      } catch (err) {
        if (token !== epoch.current || isAbort(err)) return;
      } finally {
        if (token === epoch.current) {
          writingRef.current = false;
          setPending(null);
        }
      }
    })();
  }

  const effective = view?.effective;
  const draftDiffers = !!effective && requested !== effective.profile_id;
  const resume = view?.resume;
  const continued = continuity(resume);

  return (
    <section aria-label="任务管家模型" className="space-y-3 border-b border-border p-4">
      <h2 className="text-sm font-semibold">任务管家模型</h2>
      {phase === "loading" && <p role="status">正在读取任务管家模型</p>}
      {phase === "missing" && <p role="status">此服务器尚不支持任务模型切换</p>}
      {phase === "error" && (
        <div role="alert" className="space-y-2">
          <p className="text-sm text-destructive">模型读取失败</p>
          <Button type="button" variant="outline" onClick={() => setReload((value) => value + 1)}>重试</Button>
        </div>
      )}
      {view && phase !== "loading" && phase !== "missing" && phase !== "error" && (
        <>
          {phase === "empty" && <p className="text-sm">没有可用的任务管家模型</p>}
          {phase === "ready" && (
            <fieldset disabled={pending !== null} className="min-w-0 space-y-2 border-0 p-0">
              <legend className="sr-only">任务管家 Profile</legend>
              {view.inherit && (
                <label data-choice="inherit" className="block rounded-md border border-border p-3 text-sm pointer-coarse:min-h-11 has-[:focus-visible]:ring-2 has-[:focus-visible]:ring-ring">
                  <span className="flex items-center gap-2">
                    <input
                      type="radio"
                      name={group}
                      value=""
                      checked={selection === ""}
                      onChange={() => pick(null, false)}
                      className="focus-visible:outline-none"
                    />
                    <span>跟随角色默认</span>
                  </span>
                </label>
              )}
              {view.choices.map((choice) => {
                const reason = blockReason(choice);
                return (
                  <label key={choice.profile_id} data-choice={choice.profile_id} className="block rounded-md border border-border p-3 text-sm pointer-coarse:min-h-11 has-[:focus-visible]:ring-2 has-[:focus-visible]:ring-ring">
                    <span className="flex items-start gap-2">
                      <input
                        type="radio"
                        name={group}
                        value={choice.profile_id}
                        checked={selection === choice.profile_id}
                        disabled={!!reason}
                        onChange={() => pick(choice.profile_id, !!reason)}
                        className="mt-1 focus-visible:outline-none"
                      />
                      <span className="min-w-0 space-y-1 [overflow-wrap:anywhere]">
                        <span className="block font-medium">{choice.name}</span>
                        <span className="block font-mono text-xs text-muted-foreground">{choice.profile_id}</span>
                        <span className="block">来源 {sourceLabel(choice.source)}</span>
                        <span className="block">模型 {choice.model}</span>
                        <span className="block">运行时 {choice.runtime}</span>
                        <span className="block">Provider {choice.provider}</span>
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
              <p>Profile <span data-field="profile-id">{effective.profile_id}</span></p>
              <p>模型 <span data-field="model">{effective.model}</span></p>
              <p>运行时 <span data-field="runtime">{effective.runtime}</span></p>
              <p>Provider <span data-field="provider">{effective.provider}</span></p>
              <p>来源 <span data-field="source">{sourceLabel(effective.source)}</span></p>
            </div>
          )}
          {resume && (
            <div className="space-y-1 text-sm [overflow-wrap:anywhere]">
              {continued && <p>{continued}</p>}
              {typeof resume.session_id === "string" && <p>会话 <span data-field="session-id">{resume.session_id}</span></p>}
              {"history" in resume && <p>历史 <span data-field="history">{historyText(resume.history)}</span></p>}
              {typeof resume.cost_usd === "number" && <p>费用 <span data-field="cost">{resume.cost_usd}</span></p>}
              {resume.background_roles?.planner !== undefined && <p>后台 Planner <span data-field="planner">{String(resume.background_roles.planner)}</span></p>}
              {resume.background_roles?.worker !== undefined && <p>后台 Worker <span data-field="worker">{String(resume.background_roles.worker)}</span></p>}
              {resume.background_roles?.reviewer !== undefined && <p>后台 Reviewer <span data-field="reviewer">{String(resume.background_roles.reviewer)}</span></p>}
            </div>
          )}
          {draftDiffers && (
            <p className="text-sm">当前选择与有效配置不同。当前选择 {requested ?? "跟随角色默认"}。有效配置 {effective?.profile_id}。</p>
          )}
          {props.mode === "task" && (
            <Button type="button" variant="outline" disabled={!submittable} onClick={save}>切换模型</Button>
          )}
          {outcome?.kind === "saving" && <p role="status">正在切换…</p>}
          {outcome?.kind === "busy" && <p role="alert" className="text-sm text-destructive">请先停止当前回复再切换模型</p>}
          {outcome?.kind === "runtime" && (
            <p role="alert" className="text-sm text-destructive">
              {outcome.message}
              {outcome.requested ? ` requested runtime ${outcome.requested}` : ""}
              {outcome.supported ? ` supported runtime ${outcome.supported}` : ""}
            </p>
          )}
          {outcome?.kind === "invalid" && <p role="alert" className="text-sm text-destructive">{outcome.message}</p>}
          {outcome?.kind === "failure" && <p role="alert" className="text-sm text-destructive">切换失败 {outcome.message}</p>}
          {outcome?.kind === "uncertain" && (
            <div role="alert" className="space-y-2">
              <p className="text-sm text-destructive">切换结果尚未确认</p>
              <Button type="button" variant="outline" onClick={reread} disabled={pending !== null}>重新读取</Button>
            </div>
          )}
          {outcome?.kind === "mismatch" && <p role="alert" className="text-sm text-destructive">读回的 Profile 是 {outcome.readId}</p>}
          {outcome?.kind === "readback-failed" && (
            <div role="alert" className="space-y-2">
              <p className="text-sm text-destructive">模型读取失败</p>
              <Button type="button" variant="outline" onClick={reread} disabled={pending !== null}>重新读取</Button>
            </div>
          )}
          {outcome?.kind === "success" && <p role="status">已按读回更新</p>}
        </>
      )}
    </section>
  );
}

function outcomeFrom(result: ProfileSetResult): Outcome {
  if (result.ok) return { kind: "success" };
  if (result.reason === "busy") return { kind: "busy" };
  if (result.reason === "runtime") {
    return { kind: "runtime", message: result.message, requested: result.requested, supported: result.supported };
  }
  if (result.reason === "invalid") return { kind: "invalid", message: result.message };
  if (result.reason === "failure") return { kind: "failure", message: result.message };
  if (result.reason === "uncertain") return { kind: "uncertain" };
  return { kind: "failure", message: "模型读取失败" };
}
