import { useEffect, useState } from "react";
import { Button } from "@/components/ui/button";
import { Switch } from "@/components/ui/switch";
import { ConfirmDialog } from "@/components/confirm-dialog";
import { leadsChats, leadsSetChat, type LeadChat } from "@/lib/api-leads";
import { relative } from "@/lib/leads-labels";
import { errorText } from "@/lib/use-action";
import { V1Error } from "@/lib/v1";

const KIND_ZH: Record<string, string> = { p2p: "私聊", group: "群", topic: "话题", unknown: "未知" };

/** Which chats a leads subscription reads. Turning one off can also delete what it already extracted. */
export function LeadsChats() {
  const [chats, setChats] = useState<LeadChat[] | null>(null);
  const [error, setError] = useState("");
  const [conflict, setConflict] = useState("");
  const [pending, setPending] = useState<LeadChat | null>(null);
  const [remove, setRemove] = useState(false);
  const [busy, setBusy] = useState(false);
  const [attempt, setAttempt] = useState(0);

  useEffect(() => {
    let live = true;
    leadsChats().then(
      (page) => { if (live) { setChats(page.chats); setError(""); setConflict(""); } },
      (err: unknown) => {
        if (!live) return;
        setChats([]);
        if (err instanceof V1Error && err.code === "conflict") setConflict(err.message);
        else setError(errorText(err));
      },
    );
    return () => { live = false; };
  }, [attempt]);

  const save = async (chat: LeadChat, extraction: "included" | "excluded", deleteExtracted = false) => {
    setBusy(true);
    setError("");
    try {
      await leadsSetChat({
        chat_id: chat.chat_id, extraction, ...(extraction === "excluded" ? { delete_extracted: deleteExtracted } : {}),
      });
      setPending(null);
      setRemove(false);
      setAttempt((n) => n + 1);
    } catch (err) {
      if (err instanceof V1Error && err.code === "conflict") setConflict(err.message);
      else setError(errorText(err));
    } finally {
      setBusy(false);
    }
  };

  return (
    <section aria-labelledby="leads-chats-title" className="space-y-2">
      <h2 id="leads-chats-title" className="text-sm font-medium">整理范围</h2>
      {conflict && <p role="status" data-state="conflict" className="text-sm text-warning">{conflict}</p>}
      {error && <p role="alert" className="text-sm text-destructive">{error}</p>}
      {!chats && !error && !conflict && <p role="status" className="text-sm text-muted-foreground">加载中…</p>}
      {chats && chats.length === 0 && !conflict && <p role="status" className="text-sm text-muted-foreground">没有会话</p>}
      {chats && chats.length > 0 && (
        <div className="overflow-x-auto">
          <table className="w-full min-w-[36rem] text-left text-sm">
            <thead className="text-xs text-muted-foreground">
              <tr>
                <th className="py-2 pr-3 font-medium">会话</th>
                <th className="py-2 pr-3 font-medium">类型</th>
                <th className="py-2 pr-3 font-medium">最后一条</th>
                <th className="py-2 pr-3 font-medium">线索</th>
                <th className="py-2 pr-3 font-medium">未完成意图</th>
                <th className="py-2 font-medium">整理</th>
              </tr>
            </thead>
            <tbody>
              {chats.map((chat) => (
                <tr key={chat.chat_id} className="border-t border-border">
                  <td className="py-2 pr-3">{chat.name}</td>
                  <td className="py-2 pr-3">{KIND_ZH[chat.kind] ?? chat.kind}</td>
                  <td className="py-2 pr-3">{relative(chat.last_message_at)}</td>
                  <td className="py-2 pr-3 tabular-nums">{chat.leads}</td>
                  <td className="py-2 pr-3 tabular-nums">{chat.open_intents}</td>
                  <td className="py-2">
                    <Switch
                      checked={chat.extraction === "included"}
                      disabled={busy}
                      label={`${chat.name} ${chat.extraction === "included" ? "整理" : "不整理"}`}
                      onChange={(on) => { if (on) void save(chat, "included"); else { setRemove(false); setPending(chat); } }}
                    />
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
      {pending && (
        <ConfirmDialog title={`不整理 ${pending.name}`} onClose={() => setPending(null)}>
          <p className="text-sm">之后不再从 {pending.name} 整理线索和意图。</p>
          <label className="flex items-start gap-2 text-sm">
            <input type="checkbox" className="mt-1 h-4 w-4 accent-primary" checked={remove} onChange={(e) => setRemove(e.target.checked)} />
            <span>同时删除这个会话的线索和未建任务的意图（{pending.leads} 条 / {pending.open_intents} 条）</span>
          </label>
          <div className="flex justify-end gap-2">
            <Button type="button" variant="outline" onClick={() => setPending(null)}>取消</Button>
            <Button type="button" disabled={busy} onClick={() => void save(pending, "excluded", remove)}>确认</Button>
          </div>
        </ConfirmDialog>
      )}
    </section>
  );
}
