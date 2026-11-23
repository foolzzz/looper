import { useEffect, useState } from "react";
import { Button } from "@/components/ui/button";
import { Switch } from "@/components/ui/switch";
import { fetchMetricsAlerts, updateMetricsAlerts, type MetricsAlertsSettings } from "@/lib/metrics-alerts";
import { startPolling } from "@/lib/polling";
import { errorText } from "@/lib/use-action";
import type { V1PendingConfirmation } from "@/lib/v1";

/** Operator-only alert settings, separate from the metric figures and their filters. */
export function MetricsAlertsControl() {
  const [settings, setSettings] = useState<MetricsAlertsSettings | null>(null);
  const [error, setError] = useState("");
  const [attempt, setAttempt] = useState(0);
  const [saving, setSaving] = useState(false);
  const [pending, setPending] = useState<{ enabled: boolean; confirmation: V1PendingConfirmation } | null>(null);

  useEffect(() => startPolling(async (signal) => {
    try {
      const answer = await fetchMetricsAlerts(signal);
      if (!signal.aborted) { setSettings(answer); setError(""); }
    } catch (err) {
      if (!signal.aborted) setError(errorText(err));
    }
  }, 60_000), [attempt]);

  const save = async (enabled: boolean, confirmation?: V1PendingConfirmation) => {
    setSaving(true); setError("");
    try {
      const answer = await updateMetricsAlerts(enabled, confirmation);
      if ("status" in answer) setPending({ enabled, confirmation: answer });
      else { setSettings(answer); setPending(null); }
    } catch (err) { setError(errorText(err)); }
    finally { setSaving(false); }
  };

  return (
    <div role="group" aria-label="质量告警设置" className="mt-3 space-y-2 border-t border-border pt-3">
      <div className="flex items-center justify-between gap-3">
        <div className="min-w-0 space-y-1">
          <p className="text-xs font-medium">质量回退告警{settings ? ` · ${settings.enabled ? "已开启" : "已关闭"}` : ""}</p>
          <p className="text-xs text-muted-foreground">
            每天检查质量回退，触发时向告警会话提醒；送达层开启时遵守免打扰和每日上限。
          </p>
          <p className="text-xs text-muted-foreground">
            {settings ? settings.last_checked ? `上次检查：${settings.last_checked}` : "尚未检查" : "读取中…"}
          </p>
        </div>
        <Switch checked={settings?.enabled ?? false} label="质量回退告警" disabled={!settings || saving || !!pending}
          onChange={(enabled) => void save(enabled)} />
      </div>
      {pending && <div role="group" aria-label="确认质量告警设置" className="space-y-2 rounded-md border border-border p-3">
        <p className="break-words text-xs">{pending.confirmation.impact}</p>
        <div className="flex flex-wrap gap-2">
          <Button size="sm" disabled={saving} onClick={() => void save(pending.enabled, pending.confirmation)}>确认</Button>
          <Button size="sm" variant="outline" disabled={saving} onClick={() => setPending(null)}>取消</Button>
        </div>
      </div>}
      {error && <div className="space-y-2">
        <p role="alert" className="break-words text-xs text-destructive">质量告警设置：{error}</p>
        {!settings && <Button size="sm" variant="outline" onClick={() => setAttempt((value) => value + 1)}>重试</Button>}
      </div>}
    </div>
  );
}
