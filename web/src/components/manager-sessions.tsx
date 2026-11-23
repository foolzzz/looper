import { useCallback, useEffect, useState } from "react";
import { ConfirmDialog } from "@/components/confirm-dialog";
import { Button } from "@/components/ui/button";
import { relative } from "@/lib/leads-labels";
import { formatTokens, formatUsd } from "@/lib/usage";
import { callV1 } from "@/lib/v1";

/** One row of `manager_sessions.list`. `context_tokens` stays null until context accounting lands. */
export interface ManagerSessionRow {
  key: string;
  kind: "main" | "group" | "dm";
  name: string;
  resident: boolean;
  last_active_at: string | null;
  context_tokens: number | null;
  cost_usd: number | null;
}

interface SessionList {
  routing_enabled: boolean;
  sessions: ManagerSessionRow[];
}

const KIND: Record<ManagerSessionRow["kind"], string> = { main: "主会话", group: "群", dm: "他人私聊" };
const RESET = "重置这个会话？管家会忘掉这里之前的对话，下一条消息开一个新会话。";

function isList(value: SessionList | { status: string }): value is SessionList {
  return "sessions" in value;
}

/** The default Manager's sessions, under the dashboard's Manager group. Routing off shows main and says so. */
export function ManagerSessions() {
  const [view, setView] = useState<SessionList | null>(null);
  const [error, setError] = useState("");
  const [pending, setPending] = useState<ManagerSessionRow | null>(null);
  const [busy, setBusy] = useState(false);

  const load = useCallback(async (signal?: AbortSignal) => {
    try {
      const result = await callV1<SessionList>("/api/v1/manager_sessions/list", {}, { signal });
      if (signal?.aborted || !isList(result)) return;
      setView(result);
      setError("");
    } catch (caught) {
      if (signal?.aborted) return;
      setError(caught instanceof Error ? caught.message : String(caught));
    }
  }, []);

  useEffect(() => {
    const controller = new AbortController();
    void load(controller.signal);
    return () => controller.abort();
  }, [load]);

  const confirm = async () => {
    if (!pending) return;
    setBusy(true);
    try {
      const result = await callV1<ManagerSessionRow>("/api/v1/manager_sessions/reset", { key: pending.key });
      if (!result || !("key" in result)) return;
      setPending(null);
      await load();
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : String(caught));
    } finally {
      setBusy(false);
    }
  };

  const rows = view == null ? null : view.routing_enabled ? view.sessions : view.sessions.filter((row) => row.kind === "main");

  return (
    <div className="mt-4 space-y-2" aria-label="Manager sessions">
      <h3 className="text-xs font-medium text-muted-foreground">会话</h3>
      {error && <p role="alert" className="text-xs text-destructive">{error}</p>}
      {view && !view.routing_enabled && (
        <p className="text-sm text-muted-foreground">按会话分开是关的，这里只显示主会话。</p>
      )}
      {rows == null ? (
        <p className="text-sm text-muted-foreground">正在读取会话…</p>
      ) : rows.length === 0 ? (
        <p className="text-sm text-muted-foreground">没有会话。</p>
      ) : (
        <>
          <ul className="space-y-3 sm:hidden">
            {rows.map((row) => (
              <li key={row.key} className="space-y-1 border-t border-border pt-3 first:border-t-0 first:pt-0">
                <div className="flex items-start justify-between gap-3">
                  <div className="min-w-0">
                    <p className="truncate font-medium">{row.name}</p>
                    <p className="text-xs text-muted-foreground">{KIND[row.kind]} · {row.resident ? "常驻" : "已回收"}</p>
                  </div>
                  <Button type="button" variant="outline" size="sm" onClick={() => setPending(row)}>重置</Button>
                </div>
                <p className="text-xs text-muted-foreground">
                  {relative(row.last_active_at)} · 上下文 {formatTokens(row.context_tokens)} · {formatUsd(row.cost_usd)}
                </p>
              </li>
            ))}
          </ul>
          <div className="hidden overflow-x-auto sm:block">
            <table className="w-full whitespace-nowrap text-left text-sm">
              <thead className="text-xs text-muted-foreground">
                <tr>
                  <th scope="col" className="py-1.5 pr-3 font-normal">名称</th>
                  <th scope="col" className="py-1.5 pr-3 font-normal">类型</th>
                  <th scope="col" className="py-1.5 pr-3 font-normal">常驻</th>
                  <th scope="col" className="py-1.5 pr-3 font-normal">最近活动</th>
                  <th scope="col" className="py-1.5 pr-3 font-normal">上下文</th>
                  <th scope="col" className="py-1.5 pr-3 font-normal">费用</th>
                  <th scope="col" className="py-1.5 font-normal"><span className="sr-only">操作</span></th>
                </tr>
              </thead>
              <tbody className="divide-y divide-border">
                {rows.map((row) => (
                  <tr key={row.key}>
                    <td className="py-1.5 pr-3">{row.name}</td>
                    <td className="py-1.5 pr-3">{KIND[row.kind]}</td>
                    <td className="py-1.5 pr-3">{row.resident ? "常驻" : "已回收"}</td>
                    <td className="whitespace-nowrap py-1.5 pr-3 tabular-nums">{relative(row.last_active_at)}</td>
                    <td className="py-1.5 pr-3 tabular-nums">{formatTokens(row.context_tokens)}</td>
                    <td className="py-1.5 pr-3 tabular-nums">{formatUsd(row.cost_usd)}</td>
                    <td className="py-1.5 text-right">
                      <Button type="button" variant="outline" size="sm" onClick={() => setPending(row)}>重置</Button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </>
      )}
      {pending && (
        <ConfirmDialog title="重置会话" onClose={() => { if (!busy) setPending(null); }}>
          <p className="text-sm">{RESET}</p>
          <div className="flex justify-end gap-2">
            <Button type="button" variant="outline" disabled={busy} onClick={() => setPending(null)}>取消</Button>
            <Button type="button" disabled={busy} onClick={() => void confirm()}>重置</Button>
          </div>
        </ConfirmDialog>
      )}
    </div>
  );
}
