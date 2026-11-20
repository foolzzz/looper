import { useEffect, useState } from "react";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Switch } from "@/components/ui/switch";
import { ConfirmDialog } from "@/components/confirm-dialog";
import {
  intentsSettingsGet, intentsSettingsPut, leadsSettingsGet, leadsSettingsPut,
  type IntentSubscription, type IntentsSettings, type LeadSubscription, type LeadsSettings,
} from "@/lib/api-leads";
import { errorText } from "@/lib/use-action";

/** The two switches that turn message leads and intent recognition on, next to the processor settings. */
export function LeadsSettings() {
  const [leads, setLeads] = useState<LeadsSettings | null>(null);
  const [intents, setIntents] = useState<IntentsSettings | null>(null);
  const [days, setDays] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const [confirm, setConfirm] = useState<"leads" | "intents" | null>(null);
  const [attempt, setAttempt] = useState(0);

  useEffect(() => {
    let live = true;
    Promise.all([leadsSettingsGet(), intentsSettingsGet()]).then(([leadSettings, intentSettings]) => {
      if (!live) return;
      if (!validLeads(leadSettings) || !validIntents(intentSettings)) {
        setError("Cannot read the leads settings.");
        return;
      }
      setLeads(leadSettings);
      setIntents(intentSettings);
      setDays(String(leadSettings.valid_days));
      setError("");
    }, (err: unknown) => { if (live) setError(errorText(err)); });
    return () => { live = false; };
  }, [attempt]);

  const run = async (action: () => Promise<void>) => {
    setBusy(true);
    setError("");
    try {
      await action();
      setConfirm(null);
      setAttempt((n) => n + 1);
    } catch (err) {
      setError(errorText(err));
    } finally {
      setBusy(false);
    }
  };

  if (!leads || !intents) {
    return error
      ? <p role="alert" className="text-xs text-destructive">{error}</p>
      : <p role="status" className="text-xs text-muted-foreground">加载中…</p>;
  }

  const pending = confirm === "leads" ? leads.subscriptions : confirm === "intents" ? intents.subscriptions : null;
  return (
    <section aria-label="消息线索和意图识别" className="space-y-3 rounded-md border border-border px-4 py-3">
      <SwitchRow
        title="消息线索"
        checked={leads.enabled}
        subscriptions={leads.subscriptions}
        disabled={busy}
        onChange={(next) => { if (next) void run(() => leadsSettingsPut({ enabled: true }).then(() => undefined)); else setConfirm("leads"); }}
      >
        <div className="flex flex-wrap items-end gap-2">
          <div className="space-y-1">
            <label htmlFor="leads-valid-days" className="text-xs text-muted-foreground">有效天数</label>
            <Input id="leads-valid-days" inputMode="numeric" value={days} onChange={(e) => setDays(e.target.value)} className="w-24" />
          </div>
          <Button type="button" size="sm" variant="outline" disabled={busy} onClick={() => {
            const validDays = Number(days);
            if (!Number.isInteger(validDays) || validDays < 1 || validDays > 365) {
              setError("有效天数要在 1 到 365 之间。");
              return;
            }
            void run(() => leadsSettingsPut({ valid_days: validDays }).then(() => undefined));
          }}>保存</Button>
        </div>
      </SwitchRow>
      <SwitchRow
        title="意图识别"
        checked={intents.enabled}
        subscriptions={intents.subscriptions}
        disabled={busy}
        onChange={(next) => { if (next) void run(() => intentsSettingsPut({ enabled: true }).then(() => undefined)); else setConfirm("intents"); }}
      />
      {error && <p role="alert" className="text-xs text-destructive">{error}</p>}
      {confirm && pending && (
        <ConfirmDialog title={confirm === "leads" ? "关闭消息线索" : "关闭意图识别"} onClose={() => setConfirm(null)}>
          <p className="text-sm">关闭后这些订阅会停用。</p>
          {pending.length === 0
            ? <p className="text-sm text-muted-foreground">没有订阅会被停用。</p>
            : <ul className="list-disc space-y-1 pl-5 text-sm">{pending.map((item) => <li key={item.schedule_id}>{item.name}</li>)}</ul>}
          <div className="flex justify-end gap-2">
            <Button type="button" variant="outline" onClick={() => setConfirm(null)}>取消</Button>
            <Button type="button" disabled={busy} onClick={() => void run(() => (
              confirm === "leads" ? leadsSettingsPut({ enabled: false }) : intentsSettingsPut({ enabled: false })
            ).then(() => undefined))}>关闭</Button>
          </div>
        </ConfirmDialog>
      )}
    </section>
  );
}

function validLeads(value: LeadsSettings): boolean {
  return Boolean(value) && typeof value.enabled === "boolean" && Array.isArray(value.subscriptions);
}

function validIntents(value: IntentsSettings): boolean {
  return Boolean(value) && typeof value.enabled === "boolean" && Array.isArray(value.subscriptions);
}

function SwitchRow({
  title, checked, subscriptions, disabled, onChange, children,
}: {
  title: string;
  checked: boolean;
  subscriptions: (LeadSubscription | IntentSubscription)[];
  disabled: boolean;
  onChange: (next: boolean) => void;
  children?: React.ReactNode;
}) {
  return (
    <div className="space-y-2 border-t border-border pt-3 first:border-t-0 first:pt-0">
      <div className="flex items-center justify-between gap-3">
        <span className="text-sm font-medium">{title}</span>
        <Switch checked={checked} disabled={disabled} label={title} onChange={onChange} />
      </div>
      {subscriptions.length === 0
        ? <p className="text-xs text-muted-foreground">没有订阅</p>
        : (
          <ul className="space-y-1 text-xs text-muted-foreground">
            {subscriptions.map((item) => (
              <li key={item.schedule_id}>
                {item.name} · {item.enabled ? "已启用" : "已关闭"}
                {"target_ok" in item && item.target_ok === false ? " · 目标未确认" : ""}
              </li>
            ))}
          </ul>
        )}
      {children}
    </div>
  );
}
