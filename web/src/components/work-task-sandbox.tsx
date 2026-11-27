import { useEffect, useState } from "react";
import { readTaskManagerSandbox, type SandboxRead } from "@/lib/task-manager-sandbox";

/** The scope the server states for the allowlist (RFC task-sessions-v0, section 25.3), in the dashboard's words. */
const SCOPE =
  "白名单只对经沙箱代理、走 HTTP(S) 的命令起作用（如 curl、npm、pip、Python 的常规 HTTP 客户端）。"
  + "Go 编写的命令行工具（如 gh）在 v1 即使域名在名单里也不能用；Node 编写的命令行工具默认不走代理，不保证可用。"
  + "名单里的域名本身也可能被用来传出数据，条目由操作者负责。";

/** The effective network allowlist of the task Manager's sandbox for this task's project. Read only. */
export function WorkTaskSandbox({ projectId }: { projectId: string | null }) {
  const [state, setState] = useState<{ project: string | null; read: SandboxRead | null }>({ project: projectId, read: null });

  useEffect(() => {
    if (!projectId) return;
    const controller = new AbortController();
    readTaskManagerSandbox(projectId, controller.signal)
      .then((read) => setState({ project: projectId, read }))
      .catch(() => undefined);
    return () => controller.abort();
  }, [projectId]);

  const read = state.project === projectId ? state.read : null;
  // An older server has no such operation: show nothing rather than a list that is not the server's.
  if (!projectId || (read && !read.ok && read.reason === "missing")) return null;
  return (
    <section aria-labelledby="work-sandbox-title" className="min-w-0 max-w-full space-y-2 p-4">
      <h2 id="work-sandbox-title" className="text-sm font-semibold">任务管家沙箱网络</h2>
      {!read ? <p role="status" className="text-sm text-muted-foreground">正在读取白名单…</p>
        : !read.ok ? <p role="alert" className="text-sm text-destructive">无法读取白名单：{read.message}</p>
        : read.view.network_allowed_domains.length === 0
          ? <p className="text-sm text-muted-foreground">没有放行任何域名：沙箱里的命令访问不了外部网络。</p>
          : <ul aria-label="已放行的域名" className="space-y-1 font-mono text-xs">
            {read.view.network_allowed_domains.map((domain) => <li key={domain} className="break-all">{domain}</li>)}
          </ul>}
      <p className="text-xs text-muted-foreground">本项目的白名单，由操作者通过 projects.task_manager_sandbox_update 修改。{SCOPE}</p>
    </section>
  );
}
