import { useEffect, useId, useState } from "react";
import { AlertTriangle, CheckCircle2, CircleOff, Loader2, XCircle } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Card } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Switch } from "@/components/ui/switch";
import { ConfirmDialog } from "@/components/confirm-dialog";
import { LABEL, SELECT } from "@/components/registry-ui";
import { fetchChannelConfigs, type ChannelConfig } from "@/lib/api";
import {
  memoryCheck, memoryConfirmSave, memorySettingsGet, memorySettingsPut, MemoryNeedsConfirmation,
  type MemoryCheck, type MemorySettings, type MemorySettingsView, type MemoryTarget,
} from "@/lib/memory";
import { errorText } from "@/lib/use-action";
import type { V1PendingConfirmation } from "@/lib/v1";

const KINDS: [string, string][] = [
  ["preference", "偏好"], ["feedback", "反馈"], ["decision", "决定"], ["lesson", "经验"], ["fact", "事实"],
  ["commitment", "承诺"], ["person", "人物"], ["project", "项目"],
];
const SOURCES: [keyof MemorySettings["sources"], string][] = [
  ["manager", "和 Manager 的对话"], ["tasks", "任务的结束总结"], ["leads", "消息线索"],
];
const CHECK_ZH: Record<string, string> = {
  setting: "设置", git: "git 仓库", branch: "分支", operation: "主检出里没有进行中的操作", ignored: "目标没被 git 忽略",
  worktree_root: "worktree 目录", push_channel: "推送目标",
};

interface TargetDraft { shape: "" | "note" | "entry"; folders: string; index: string; files: string }
interface Draft {
  enabled: boolean; root: string; rules: string; frontmatter: string;
  targets: Record<string, TargetDraft>; sources: MemorySettings["sources"];
  pushChannel: string; pendingDays: string; draftBudget: string; dailyBudget: string;
}

const list = (text: string) => text.split(",").map((part) => part.trim()).filter(Boolean);

function toDraft(settings: Partial<MemorySettings> | null): Draft {
  const targets: Record<string, TargetDraft> = {};
  for (const [kind] of KINDS) {
    const target: MemoryTarget | undefined = settings?.targets?.[kind];
    targets[kind] = { shape: target?.shape ?? "", folders: (target?.folders ?? []).join(", "), index: target?.index ?? "", files: (target?.files ?? []).join(", ") };
  }
  return {
    enabled: settings?.enabled ?? false, root: settings?.root ?? "", rules: (settings?.rules_files ?? []).join(", "),
    frontmatter: (settings?.required_frontmatter ?? []).join(", "), targets,
    sources: { manager: false, tasks: false, leads: false, ...settings?.sources },
    pushChannel: settings?.push_channel ?? "", pendingDays: String(settings?.pending_days ?? 7),
    draftBudget: String(settings?.draft_budget_usd ?? 0.15), dailyBudget: String(settings?.daily_budget_usd ?? 1),
  };
}

/** The whole setting as `memory.settings_put` takes it. The branch is the main checkout's: the server fills it. */
function toBody(draft: Draft, stored: Partial<MemorySettings> | null): Record<string, unknown> {
  const targets: Record<string, unknown> = {};
  for (const [kind, target] of Object.entries(draft.targets)) {
    if (target.shape === "note") targets[kind] = { shape: "note", folders: list(target.folders), index: target.index.trim() || null };
    else if (target.shape === "entry") targets[kind] = { shape: "entry", files: list(target.files) };
  }
  return {
    enabled: draft.enabled, root: draft.root.trim(), rules_files: list(draft.rules), targets,
    required_frontmatter: list(draft.frontmatter), sources: draft.sources,
    worktree_root: stored?.worktree_root ?? null, commit_prefix: stored?.commit_prefix ?? null,
    push_channel: draft.pushChannel || null, pending_days: Number(draft.pendingDays),
    draft_budget_usd: Number(draft.draftBudget), daily_budget_usd: Number(draft.dailyBudget),
  };
}

/** Settings → Knowledge vault, 记忆写入: what may be proposed, where it goes, and the preflight check. */
export function MemorySettingsSection() {
  const id = useId();
  const [view, setView] = useState<MemorySettingsView | null>(null);
  const [draft, setDraft] = useState<Draft>(() => toDraft(null));
  const [loadError, setLoadError] = useState("");
  const [attempt, setAttempt] = useState(0);
  const [bots, setBots] = useState<ChannelConfig[]>([]);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState("");
  const [saved, setSaved] = useState(false);
  const [asking, setAsking] = useState<V1PendingConfirmation | null>(null);
  const [checking, setChecking] = useState(false);
  const [check, setCheck] = useState<MemoryCheck | null>(null);
  const [checkError, setCheckError] = useState("");

  const apply = (value: MemorySettingsView) => {
    setView(value);
    setDraft(toDraft(value.settings));
  };

  useEffect(() => {
    let live = true;
    memorySettingsGet().then((value) => { if (live) apply(value); }, (err: unknown) => { if (live) setLoadError(errorText(err)); });
    // The push target is a bot that has the operator's id; a failed read only leaves the choice empty.
    fetchChannelConfigs().then((all) => { if (live) setBots(all); }, () => {});
    return () => { live = false; };
  }, [attempt]);

  const update = (patch: Partial<Draft>) => { setDraft((now) => ({ ...now, ...patch })); setSaved(false); };
  const target = (kind: string, patch: Partial<TargetDraft>) => update({ targets: { ...draft.targets, [kind]: { ...draft.targets[kind], ...patch } } });

  const finish = (promise: Promise<MemorySettingsView>) => {
    setSaving(true);
    setError("");
    setSaved(false);
    promise.then((value) => { apply(value); setSaved(true); setAsking(null); }, (err: unknown) => {
      if (err instanceof MemoryNeedsConfirmation) setAsking(err.pending);
      else { setError(errorText(err)); setAsking(null); }
    }).finally(() => setSaving(false));
  };

  const save = () => {
    const body = toBody(draft, view?.settings ?? null);
    for (const key of ["pending_days", "draft_budget_usd", "daily_budget_usd"]) {
      if (!Number.isFinite(body[key])) { setError("天数和预算要填数字。"); return; }
    }
    finish(memorySettingsPut(body));
  };

  const runCheck = async () => {
    setChecking(true);
    setCheckError("");
    try { setCheck(await memoryCheck()); } catch (err) { setCheck(null); setCheckError(errorText(err)); } finally { setChecking(false); }
  };

  const pushBots = bots.filter((bot) => ["lark", "lark_bot", "telegram"].includes(bot.channel_type)
    && typeof bot.config?.operator_sender_id === "string" && bot.config.operator_sender_id.trim() !== "");
  const stored = view?.settings;
  const Icon = view?.problem ? AlertTriangle : view?.enabled ? CheckCircle2 : CircleOff;
  const status = view?.problem ? `设置现在有问题，写入会受阻：${view.problem}`
    : !view?.configured ? "没有配置：不会提议，也不会写任何记忆。"
    : view.enabled ? "已打开：你确认的记忆会作为提交写进知识库。" : "已配置，但没有打开。";

  return (
    <Card className="min-w-0 p-5">
      <h2 className="mb-3 text-base font-semibold">记忆写入</h2>
      {loadError ? (
        <div className="space-y-3">
          <p role="alert" className="break-words text-sm text-destructive">{loadError}</p>
          <Button variant="outline" onClick={() => { setLoadError(""); setAttempt((n) => n + 1); }}>重试</Button>
        </div>
      ) : !view ? (
        <p role="status" className="text-sm text-muted-foreground">加载中…</p>
      ) : (
        <form className="space-y-5" onSubmit={(event) => { event.preventDefault(); if (!saving) save(); }}>
          <p role={view.problem ? "alert" : "status"} className={`flex items-start gap-2 break-words text-sm ${view.problem ? "text-destructive" : "text-muted-foreground"}`}>
            <Icon aria-hidden="true" className="h-4 w-4 shrink-0" /><span className="min-w-0">{status}</span>
          </p>

          <div className="flex items-center gap-3">
            <Switch label="打开记忆写入" checked={draft.enabled} disabled={saving} onChange={(enabled) => update({ enabled })} />
            <span className="text-sm">打开记忆写入</span>
            <span className="text-xs text-muted-foreground">打开记忆写入，或改推送目标时，会先列出影响，由你确认。</span>
          </div>

          <div className="grid gap-4 sm:grid-cols-2">
            <div className="space-y-1.5">
              <label htmlFor={`${id}-root`} className={LABEL}>记忆区（相对知识库）</label>
              <Input id={`${id}-root`} value={draft.root} disabled={saving} placeholder="codex-mem" onChange={(e) => update({ root: e.target.value })} />
              <p className="text-xs text-muted-foreground">留空表示整个知识库目录；不能和可写文件夹重叠。</p>
            </div>
            <div className="space-y-1.5">
              <label htmlFor={`${id}-rules`} className={LABEL}>规则文件（逗号分隔，最多 3 个）</label>
              <Input id={`${id}-rules`} value={draft.rules} disabled={saving} placeholder="AGENTS.md" onChange={(e) => update({ rules: e.target.value })} />
            </div>
            <div className="space-y-1.5">
              <label htmlFor={`${id}-fm`} className={LABEL}>新笔记必须有的 frontmatter 键</label>
              <Input id={`${id}-fm`} value={draft.frontmatter} disabled={saving} placeholder="kind, created" onChange={(e) => update({ frontmatter: e.target.value })} />
            </div>
            <div className="space-y-1.5">
              <label htmlFor={`${id}-push`} className={LABEL}>推送到哪个机器人</label>
              <select id={`${id}-push`} className={SELECT} value={draft.pushChannel} disabled={saving} onChange={(e) => update({ pushChannel: e.target.value })}>
                <option value="">不推送，只在记忆页看</option>
                {draft.pushChannel && !pushBots.some((bot) => bot.channel_id === draft.pushChannel) && (
                  <option value={draft.pushChannel}>{draft.pushChannel}（现在不可用）</option>
                )}
                {pushBots.map((bot) => <option key={bot.channel_id} value={bot.channel_id}>{bot.display_name || bot.channel_id}</option>)}
              </select>
              <p className="text-xs text-muted-foreground">只列出填了「我的 ID」的飞书和 Telegram 机器人。</p>
            </div>
          </div>

          <fieldset className="space-y-2">
            <legend className="text-sm font-semibold">能提议的类型和写到哪里</legend>
            <p className="text-xs text-muted-foreground">没配置的类型不会被提议。笔记型：每条一篇笔记，放在文件夹里并加一行索引；条目型：往已有页面里加一行。路径相对记忆区。</p>
            <div className="space-y-3">
              {KINDS.map(([kind, name]) => {
                const item = draft.targets[kind];
                return (
                  <div key={kind} className="grid items-end gap-2 sm:grid-cols-[6rem_8rem_minmax(0,1fr)_minmax(0,1fr)]">
                    <span className="text-sm">{name}</span>
                    <select aria-label={`${name}的写法`} className={SELECT} value={item.shape} disabled={saving} onChange={(e) => target(kind, { shape: e.target.value as TargetDraft["shape"] })}>
                      <option value="">不配置</option><option value="note">笔记型</option><option value="entry">条目型</option>
                    </select>
                    {item.shape === "note" && (
                      <>
                        <Input aria-label={`${name}的文件夹`} placeholder="文件夹，逗号分隔" value={item.folders} disabled={saving} onChange={(e) => target(kind, { folders: e.target.value })} />
                        <Input aria-label={`${name}的索引文件`} placeholder="索引文件" value={item.index} disabled={saving} onChange={(e) => target(kind, { index: e.target.value })} />
                      </>
                    )}
                    {item.shape === "entry" && (
                      <Input aria-label={`${name}的页面`} className="sm:col-span-2" placeholder="页面，逗号分隔" value={item.files} disabled={saving} onChange={(e) => target(kind, { files: e.target.value })} />
                    )}
                  </div>
                );
              })}
            </div>
          </fieldset>

          <fieldset className="space-y-2">
            <legend className="text-sm font-semibold">从哪里提议</legend>
            {SOURCES.map(([key, name]) => (
              <div key={key} className="flex items-center gap-3">
                <Switch label={name} checked={draft.sources[key]} disabled={saving} onChange={(on) => update({ sources: { ...draft.sources, [key]: on } })} />
                <span className="text-sm">{name}</span>
              </div>
            ))}
          </fieldset>

          <div className="grid gap-4 sm:grid-cols-3">
            <div className="space-y-1.5">
              <label htmlFor={`${id}-days`} className={LABEL}>待确认保留天数（1–90）</label>
              <Input id={`${id}-days`} inputMode="numeric" value={draft.pendingDays} disabled={saving} onChange={(e) => update({ pendingDays: e.target.value })} />
            </div>
            <div className="space-y-1.5">
              <label htmlFor={`${id}-draft`} className={LABEL}>每次起草的预算（美元）</label>
              <Input id={`${id}-draft`} inputMode="decimal" value={draft.draftBudget} disabled={saving} onChange={(e) => update({ draftBudget: e.target.value })} />
            </div>
            <div className="space-y-1.5">
              <label htmlFor={`${id}-daily`} className={LABEL}>每天的预算（美元）</label>
              <Input id={`${id}-daily`} inputMode="decimal" value={draft.dailyBudget} disabled={saving} onChange={(e) => update({ dailyBudget: e.target.value })} />
            </div>
          </div>

          <dl className="grid gap-x-6 gap-y-1 text-sm sm:grid-cols-[auto_minmax(0,1fr)]">
            <dt className="text-muted-foreground">分支</dt>
            <dd className="break-all font-mono text-xs">{stored?.branch ?? "保存时取知识库主检出的当前分支"}</dd>
            <dt className="text-muted-foreground">worktree 目录</dt>
            <dd className="break-all font-mono text-xs">{stored?.worktree_root ?? "运行目录下的 vault-worktrees"}</dd>
          </dl>

          <div className="flex flex-wrap items-center gap-3">
            <Button type="submit" disabled={saving}>{saving ? "保存中…" : "保存"}</Button>
            <Button type="button" variant="outline" disabled={saving || checking} onClick={() => void runCheck()}>
              {checking && <Loader2 className="h-4 w-4 animate-spin motion-reduce:animate-none" aria-hidden="true" />}检查
            </Button>
            {saved && <span role="status" className="text-sm text-muted-foreground">已保存。</span>}
          </div>
          {error && <p role="alert" className="break-words text-sm text-destructive">{error}</p>}
          {checkError && <p role="alert" className="break-words text-sm text-destructive">检查没跑成：{checkError}</p>}
          {check && (
            <ul aria-label="检查结果" className="space-y-1.5">
              {check.checks.map((item) => (
                <li key={item.name} className="flex items-start gap-2 text-sm">
                  {item.ok ? <CheckCircle2 aria-hidden="true" className="mt-0.5 h-4 w-4 shrink-0 text-success" /> : <XCircle aria-hidden="true" className="mt-0.5 h-4 w-4 shrink-0 text-destructive" />}
                  <span className="min-w-0 break-words">
                    {CHECK_ZH[item.name] ?? item.name}：{item.ok ? "通过" : "没通过"}
                    {item.detail && <span className={item.ok ? "text-muted-foreground" : ""}> · {item.detail}</span>}
                  </span>
                </li>
              ))}
            </ul>
          )}
        </form>
      )}
      {asking && (
        <ConfirmDialog title="确认这次保存" onClose={() => setAsking(null)}>
          <p className="whitespace-pre-wrap break-words text-sm">{asking.impact}</p>
          <div className="flex justify-end gap-2">
            <Button type="button" variant="outline" onClick={() => setAsking(null)}>取消</Button>
            <Button type="button" disabled={saving} onClick={() => finish(memoryConfirmSave(asking))}>确认并保存</Button>
          </div>
        </ConfirmDialog>
      )}
    </Card>
  );
}
