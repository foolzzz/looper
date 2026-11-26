import { useEffect, useState } from "react";
import { Button } from "@/components/ui/button";
import { errorText } from "@/lib/use-action";
import { callV1, V1Error } from "@/lib/v1";
import { isMissingOperation } from "@/lib/work";

type FileStatus = "added" | "modified" | "deleted" | "renamed" | "copied" | "type_changed" | "conflicted" | "untracked" | "unknown";
type UnavailableReason = "not_git" | "workspace_missing" | "branch_missing" | "branch_changed" | "git_error";
interface TaskChanges {
  task_id: string;
  kind: "ok" | "unavailable";
  scope: "task_branch" | "working_tree" | null;
  files: { path: string; status: FileStatus; old_path: string | null }[];
  truncated: boolean;
  reason: UnavailableReason | null;
}

const STATUS: Record<FileStatus, string> = {
  added: "新增", modified: "修改", deleted: "删除", renamed: "重命名", copied: "复制",
  type_changed: "类型变更", conflicted: "冲突", untracked: "未跟踪", unknown: "状态未知",
};
const REASON: Record<UnavailableReason, string> = {
  not_git: "工作目录不是 Git 仓库", workspace_missing: "工作目录不存在",
  branch_missing: "任务分支不存在", branch_changed: "工作区分支已变更", git_error: "Git 读取失败",
};

async function readChanges(taskId: string, signal: AbortSignal): Promise<TaskChanges> {
  const result = await callV1<TaskChanges>("/api/v1/tasks/changes", { task_id: taskId, limit: 100 }, { signal });
  if ("status" in result) throw new V1Error(202, "unavailable", result.impact);
  return result;
}

function readError(error: unknown): string {
  if (isMissingOperation(error)) return "此服务器尚不支持改动文件";
  if (error instanceof V1Error && error.status === 404) return "任务不存在";
  return `无法读取改动：${errorText(error)}`;
}

/** A bounded metadata read, owned only by the visible Work details section. */
export function WorkChanges({ taskId, visible, pendingWorkspace = false, awaitingWorkspace = false }: {
  taskId: string; visible: boolean; pendingWorkspace?: boolean; awaitingWorkspace?: boolean;
}) {
  const [read, setRead] = useState<{ taskId: string; loading: boolean; result?: TaskChanges; error?: string }>();
  const [revision, setRevision] = useState(0);
  const held = pendingWorkspace || awaitingWorkspace;
  useEffect(() => {
    if (!visible || held) return;
    const ctrl = new AbortController();
    setRead(previous => ({ taskId, loading: true, result: previous?.taskId === taskId ? previous.result : undefined }));
    readChanges(taskId, ctrl.signal).then(result => {
      if (!ctrl.signal.aborted) setRead({ taskId, loading: false, result });
    }).catch((error: unknown) => {
      if (!ctrl.signal.aborted) setRead(previous => ({ ...previous, taskId, loading: false, error: readError(error) }));
    });
    return () => ctrl.abort();
  }, [taskId, visible, held, revision]);

  if (!visible) return null;
  if (held) return <section aria-label="改动文件" className="min-w-0 space-y-3 text-sm">
    <h2 className="font-medium">改动文件</h2>
    <p role="status" className="text-muted-foreground">{pendingWorkspace ? "任务开始后显示改动" : "工作目录就绪后显示改动"}</p>
  </section>;
  // Guard during render too: effects run after a selection has already painted.
  const current = read?.taskId === taskId ? read : undefined;
  const result = current?.result;
  const loading = !current || current.loading;
  const files = result?.kind === "ok" ? result.files : undefined;
  const neutral = !loading && result?.kind === "unavailable" && result.reason === "not_git";
  const error = current?.error ?? (!loading && result?.kind === "unavailable" && !neutral
    ? `无法读取改动：${result.reason ? REASON[result.reason] : "原因未知"}` : undefined);

  return (
    <section aria-label="改动文件" className="min-w-0 space-y-3 text-sm">
      <div className="flex items-center justify-between gap-2">
        <h2 className="font-medium">改动文件</h2>
        <Button type="button" variant="outline" size="sm" disabled={loading} onClick={() => setRevision(n => n + 1)}>
          {error ? "重试" : "刷新"}
        </Button>
      </div>
      {loading && <p role="status" className="text-muted-foreground">{result ? "正在刷新" : "正在读取改动…"}</p>}
      {neutral && !error && <p role="status" className="text-muted-foreground">工作目录不是 Git 仓库，无法展示 Git 改动</p>}
      {error && <p role="alert" className="text-destructive [overflow-wrap:anywhere]">{files ? "刷新失败：" : ""}{error}</p>}
      {files && result && <>
        <p className="text-muted-foreground">{result.scope === "task_branch" ? "任务分支改动" : "当前未提交改动（目录可能由多个任务共用）"}</p>
        {!files.length && !error && !loading && <p>没有改动文件</p>}
        {result.truncated && <p className="text-muted-foreground">仅显示前 {files.length} 个文件</p>}
        <ul className="space-y-2">
          {files.map(file => <li key={file.path} className="min-w-0 [overflow-wrap:anywhere]">
            <span className="text-xs text-muted-foreground">{STATUS[file.status]}</span>{" "}
            <span className="whitespace-pre-wrap font-mono text-xs">{file.old_path !== null && <>{file.old_path}{" → "}</>}{file.path}</span>
          </li>)}
        </ul>
      </>}
    </section>
  );
}
