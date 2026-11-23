import { useEffect, useId, useState } from "react";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Switch } from "@/components/ui/switch";
import { callV1, confirmV1, V1Error } from "@/lib/v1";
import { REGISTRY_CHANGED_EVENT } from "@/lib/api";
import { errorText } from "@/lib/use-action";

interface AcpReviewerSettings {
  enabled: boolean;
  command: string[];
  model: string;
  account: string;
  config_dir: string;
  inspect_roots: string[];
  policy_check: { reason: string; detail: string };
  latest_launch_check: { reason: string; detail?: string; mechanism?: string } | null;
}

async function request(action: "get" | "update", body: Record<string, unknown> = {}) {
  const result = await callV1<AcpReviewerSettings>(`/api/v1/acp_reviewer/${action}`, body);
  if (!("status" in result)) return result;
  if (action !== "update") throw new V1Error(202, "unavailable", result.impact);
  if (!confirm(result.impact)) return null;
  const saved = await confirmV1<AcpReviewerSettings>(result.confirmation_id, result.session);
  if ("status" in saved) throw new V1Error(202, "unavailable", saved.impact);
  return saved;
}

function strings(text: string, label: string): string[] {
  let value: unknown;
  try { value = JSON.parse(text); } catch { throw new Error(`${label}需要填写 JSON 字符串数组`); }
  if (!Array.isArray(value) || !value.every((part) => typeof part === "string" && part.length > 0)) {
    throw new Error(`${label}需要填写 JSON 字符串数组`);
  }
  return value;
}

/** Command registration belongs only to the operator; the process policy is checked on every review. */
export function AcpReviewerSettingsSection() {
  const id = useId();
  const [settings, setSettings] = useState<AcpReviewerSettings | null>(null);
  const [command, setCommand] = useState("[]");
  const [roots, setRoots] = useState("[]");
  const [error, setError] = useState("");
  const [saving, setSaving] = useState(false);
  const [attempt, setAttempt] = useState(0);
  const apply = (value: AcpReviewerSettings) => {
    setSettings(value); setCommand(JSON.stringify(value.command)); setRoots(JSON.stringify(value.inspect_roots));
  };
  useEffect(() => {
    let alive = true;
    request("get").then((value) => { if (alive && value) apply(value); })
      .catch((err: unknown) => { if (alive) setError(errorText(err)); });
    return () => { alive = false; };
  }, [attempt]);
  const save = async () => {
    if (!settings || saving) return;
    setError("");
    try {
      const argv = strings(command, "命令");
      const inspect = strings(roots, "检查目录");
      if (argv.length > 0 && argv.length < 2) throw new Error("命令至少包含两个字符串：可执行文件及参数");
      if (settings.enabled && (argv.length < 2 || !settings.model.trim() || !settings.account.trim())) {
        throw new Error("启用前请填写命令、模型和账户标签");
      }
      setSaving(true);
      const next = await request("update", { enabled: settings.enabled, command: argv, model: settings.model,
        account: settings.account, config_dir: settings.config_dir, inspect_roots: inspect });
      if (next) {
        apply(next);
        window.dispatchEvent(new CustomEvent(REGISTRY_CHANGED_EVENT));
      }
    } catch (err) { setError(errorText(err)); } finally { setSaving(false); }
  };
  return <Card className="min-w-0">
    <CardHeader><CardTitle>ACP Reviewer（试点，默认关闭）</CardTitle></CardHeader>
    <CardContent className="min-w-0 space-y-4">
      {settings ? <form className="min-w-0 space-y-4" onSubmit={(event) => { event.preventDefault(); void save(); }}>
        <div className="flex min-h-11 flex-wrap items-center gap-3 text-sm">
          <Switch label="启用 ACP Reviewer" checked={settings.enabled} disabled={saving}
            onChange={(enabled) => setSettings({ ...settings, enabled })} />
          <span>启用 ACP Reviewer</span><Badge variant={settings.enabled ? "success" : "outline"}>{settings.enabled ? "已启用" : "已关闭"}</Badge>
        </div>
        <p className="text-xs text-muted-foreground">
          仅用于 Reviewer，每次评审启动独立进程。只允许写本次 scratch 和独立的 CLI 配置目录；仓库、Git 元数据及检查目录不可写。
          检查采取保守策略：目前只识别 grok agent stdio；CLI 配置必须可读，MCP 服务器必须由该进程以 stdio 启动。
          外部 HTTP/SSE 服务和无法判断的命令均拒绝启动。
        </p>
        <div className="space-y-1.5">
          <label htmlFor={`${id}-command`} className="text-sm font-medium">命令（JSON argv）</label>
          <Input id={`${id}-command`} value={command} disabled={saving} className="font-mono"
            placeholder={'["grok","agent","stdio"]'} onChange={(event) => setCommand(event.target.value)} />
          <p className="text-xs text-muted-foreground">系统合约与评审提示通过 stdin 传入。请勿在命令中填写密钥或 --always-approve。</p>
        </div>
        <div className="grid gap-4 md:grid-cols-2">
          <div className="space-y-1.5"><label htmlFor={`${id}-model`} className="text-sm font-medium">声明模型</label>
            <Input id={`${id}-model`} value={settings.model} disabled={saving}
              onChange={(event) => setSettings({ ...settings, model: event.target.value })} /></div>
          <div className="space-y-1.5"><label htmlFor={`${id}-account`} className="text-sm font-medium">账户标签（非密钥）</label>
            <Input id={`${id}-account`} value={settings.account} disabled={saving}
              onChange={(event) => setSettings({ ...settings, account: event.target.value })} /></div>
        </div>
        <div className="space-y-1.5"><label htmlFor={`${id}-config`} className="text-sm font-medium">CLI 配置目录</label>
          <Input id={`${id}-config`} value={settings.config_dir} disabled={saving} placeholder="留空：使用本次 scratch"
            onChange={(event) => setSettings({ ...settings, config_dir: event.target.value })} />
          <p className="text-xs text-muted-foreground">必须与保护目录分离；每次启动都会重新检查。</p></div>
        <div className="space-y-1.5"><label htmlFor={`${id}-roots`} className="text-sm font-medium">额外检查目录（JSON 数组）</label>
          <Input id={`${id}-roots`} value={roots} disabled={saving} className="font-mono" onChange={(event) => setRoots(event.target.value)} /></div>
        <p role="status" className="break-words text-xs text-muted-foreground">
          配置检查：{settings.policy_check.reason} — {settings.policy_check.detail}
        </p>
        {settings.latest_launch_check && <p className="break-words text-xs text-muted-foreground">
          最近启动检查：{settings.latest_launch_check.reason}
          {settings.latest_launch_check.detail ? ` — ${settings.latest_launch_check.detail}` : settings.latest_launch_check.mechanism ? ` — ${settings.latest_launch_check.mechanism}` : ""}
        </p>}
        <p className="text-xs text-muted-foreground">启用后，在 Agent Profiles 页创建类型为 acp 的 Profile，模型和 Provider 名称需对应上面的模型与账户标签；再在 Reviewer 角色中选择它。关闭后下一次评审会报错，不自动回退。用量未知时花费留空。</p>
        <Button type="submit" disabled={saving}>{saving ? "保存中…" : "保存配置"}</Button>
      </form> : !error && <p role="status" className="text-sm text-muted-foreground">加载中…</p>}
      {error && <div className="space-y-2"><p role="alert" className="break-words text-sm text-destructive">{error}</p>
        {!settings && <Button variant="outline" onClick={() => { setError(""); setAttempt((value) => value + 1); }}>重试</Button>}</div>}
    </CardContent>
  </Card>;
}
