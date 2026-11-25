import { useEffect, useId, useState } from "react";
import { Button } from "@/components/ui/button";
import { Card } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Switch } from "@/components/ui/switch";
import { ConfirmDialog } from "@/components/confirm-dialog";
import { callV1, confirmV1, type V1PendingConfirmation } from "@/lib/v1";
import { errorText } from "@/lib/use-action";

interface MorningBriefSettings { enabled: boolean; time: string; cutoff: string }

/** Operator-only timing controls use the brief's own operations, separate from chat settings. */
export function MorningBriefSettingsSection() {
  const id = useId();
  const [settings, setSettings] = useState<MorningBriefSettings | null>(null);
  const [error, setError] = useState("");
  const [attempt, setAttempt] = useState(0);
  const [saving, setSaving] = useState(false);
  const [saved, setSaved] = useState(false);
  const [asking, setAsking] = useState<V1PendingConfirmation | null>(null);

  useEffect(() => {
    let alive = true;
    callV1<MorningBriefSettings>("/api/v1/morning_brief/get", {}).then((value) => {
      if (alive) {
        if ("status" in value) setError(value.impact);
        else setSettings(value);
      }
    }).catch((err: unknown) => { if (alive) setError(errorText(err)); });
    return () => { alive = false; };
  }, [attempt]);

  const finish = async (request: Promise<MorningBriefSettings | V1PendingConfirmation>) => {
    setSaving(true); setError(""); setSaved(false);
    try {
      const value = await request;
      if ("status" in value) setAsking(value);
      else { setSettings(value); setAsking(null); setSaved(true); }
    } catch (err) { setError(errorText(err)); setAsking(null); }
    finally { setSaving(false); }
  };

  const save = () => {
    if (!settings || saving) return;
    if ((settings.cutoff || "12:00") <= (settings.time || "09:15")) {
      setError("补发截止时间必须晚于发送时间。"); return;
    }
    void finish(callV1<MorningBriefSettings>("/api/v1/morning_brief/update", {
      enabled: settings.enabled, time: settings.time || null, cutoff: settings.cutoff || null,
    }));
  };

  return <Card className="min-w-0 p-5">
    <h2 className="mb-3 text-base font-semibold">工作日晨报</h2>
    <p className="mb-4 text-xs text-muted-foreground">
      按设置的时区发送，默认关闭。错过发送时间时，只在补发截止时间之前补发；免打扰期间不发送。
      未发送的摘要会并入下一工作日的晨报，不占每日主动消息额度。
    </p>
    {settings ? <form className="space-y-4" onSubmit={(event) => { event.preventDefault(); save(); }}>
      <div className="flex min-h-11 items-center gap-3 text-sm">
        <Switch label="启用工作日晨报" checked={settings.enabled} disabled={saving || !!asking}
          onChange={(enabled) => { setSettings({ ...settings, enabled }); setSaved(false); }} />
        <span>启用工作日晨报</span>
      </div>
      <div className="grid max-w-xl gap-4 sm:grid-cols-2">
        {([{ key: "time", label: "发送时间", default: "09:15" },
          { key: "cutoff", label: "补发截止时间", default: "12:00" }] as const).map((field) => <div key={field.key} className="space-y-1.5">
          <label htmlFor={`${id}-${field.key}`} className="text-sm font-medium">{field.label}</label>
          <Input id={`${id}-${field.key}`} type="time" value={settings[field.key]} disabled={saving || !!asking}
            aria-describedby={`${id}-${field.key}-help`} onChange={(event) => {
              setSettings({ ...settings, [field.key]: event.target.value }); setSaved(false); setError("");
            }} />
          <p id={`${id}-${field.key}-help`} className="text-xs text-muted-foreground">留空恢复默认 {field.default}。</p>
        </div>)}
      </div>
      <Button type="submit" disabled={saving || !!asking}>{saving ? "保存中…" : "保存晨报设置"}</Button>
      {saved && <p role="status" className="text-sm text-muted-foreground">已保存</p>}
    </form> : !error && <p role="status" className="text-sm text-muted-foreground">加载中…</p>}
    {error && <div className="mt-3 space-y-2">
      <p role="alert" className="break-words text-sm text-destructive">{error}</p>
      {!settings && <Button variant="outline" onClick={() => { setError(""); setAttempt((value) => value + 1); }}>重试</Button>}
    </div>}
    {asking && <ConfirmDialog title="确认晨报设置" onClose={() => { if (!saving) setAsking(null); }}>
      <p className="whitespace-pre-wrap break-words text-sm">{asking.impact}</p>
      <div className="flex gap-2">
        <Button disabled={saving} onClick={() => void finish(confirmV1<MorningBriefSettings>(asking.confirmation_id, asking.session))}>确认保存</Button>
        <Button variant="outline" disabled={saving} onClick={() => setAsking(null)}>取消</Button>
      </div>
    </ConfirmDialog>}
  </Card>;
}
