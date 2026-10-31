import { useEffect, useId, useState } from "react";
import { Loader2 } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import {
  fetchSchedulerSettings,
  updateSchedulerSettings,
  getWaitSettings,
  updateWaitSettings,
  type SchedulerSetting,
  type SchedulerSettings,
  type WaitSettings,
} from "@/lib/api";
import { waitDuration } from "@/lib/waits";

const errorText = (err: unknown) => (err instanceof Error ? err.message : String(err));

const SOURCE_TEXT: Record<SchedulerSetting["source"], string> = {
  setting: "Saved here",
  "built-in": "Built-in value",
};

type Name = keyof SchedulerSettings;

const FIELDS: { name: Name; label: string; help: string }[] = [
  {
    name: "min_interval_seconds",
    label: "Least time between two fires (seconds)",
    help: "A schedule's rule may not fire more often than this; a schedule with a gate command may fire every minute. Saved schedules are not checked again.",
  },
  {
    name: "run_timeout_seconds",
    label: "Default run timeout (seconds; no limit until you set one)",
    help: "For schedules set to use the default. A run keeps the timeout it started with, so a change affects later runs only.",
  },
  {
    name: "run_retention_days",
    label: "Run retention (days)",
    help: "Days a finished schedule run is kept; each schedule keeps its newest 100. Enter 1–3650 days.",
  },
];

/** Settings → Scheduler: system-wide schedule settings, each with its effective value and source. */
function ScheduleSettings() {
  const [settings, setSettings] = useState<SchedulerSettings | null>(null);
  const [drafts, setDrafts] = useState<Record<Name, string>>({ min_interval_seconds: "", run_timeout_seconds: "", run_retention_days: "" });
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [saved, setSaved] = useState(false);
  const idBase = useId();

  /** Show the settings as stored; `only` leaves the other field's draft alone (it may hold unsaved typing). */
  const show = (next: SchedulerSettings, only?: Name) => {
    setSettings(next);
    const stored: Record<Name, string> = {
      min_interval_seconds: String(next.min_interval_seconds.value),
      run_timeout_seconds: next.run_timeout_seconds.value == null ? "" : String(next.run_timeout_seconds.value),
      run_retention_days: String(next.run_retention_days.value),
    };
    setDrafts((current) => (only ? { ...current, [only]: stored[only] } : stored));
  };

  useEffect(() => {
    let alive = true;
    fetchSchedulerSettings().then(
      (next) => alive && show(next),
      (err) => alive && setError(errorText(err)),
    );
    return () => {
      alive = false;
    };
  }, []);

  if (!settings) {
    return error ? (
      <p role="alert" className="text-sm text-destructive">{error}</p>
    ) : (
      <p className="flex items-center gap-2 text-sm text-muted-foreground">
        <Loader2 className="h-4 w-4 animate-spin motion-reduce:animate-none" aria-hidden /> Loading
      </p>
    );
  }

  const save = async (name: Name, value: number | null) => {
    setBusy(true);
    setError(null);
    setSaved(false);
    try {
      show(await updateSchedulerSettings({ [name]: value }), name);
      setSaved(true);
    } catch (err) {
      setError(errorText(err));
    } finally {
      setBusy(false);
    }
  };

  return (
    <Card>
      <CardHeader>
        <CardTitle>Schedules</CardTitle>
      </CardHeader>
      <CardContent className="space-y-5">
        {FIELDS.map(({ name, label, help }) => {
          const setting = settings[name];
          const id = `${idBase}-${name}`;
          const typed = drafts[name].trim();
          const changed = typed !== (setting.value == null ? "" : String(setting.value));
          const retention = name === "run_retention_days";
          const valid = /^\d+$/.test(typed) && (!retention ||
            (Number(typed) >= setting.minimum && Number(typed) <= setting.maximum!));
          return (
            <form
              key={name}
              className="space-y-1"
              onSubmit={(e) => {
                e.preventDefault();
                if (valid) void save(name, Number(typed));
              }}
            >
              <label htmlFor={id} className="block text-xs font-medium text-muted-foreground">{label}</label>
              <div className="flex flex-wrap items-center gap-2">
                <Input
                  id={id}
                  type={retention ? "number" : "text"}
                  min={retention ? setting.minimum : undefined}
                  max={retention ? setting.maximum : undefined}
                  step={retention ? 1 : undefined}
                  inputMode="numeric"
                  value={drafts[name]}
                  onChange={(e) => setDrafts({ ...drafts, [name]: e.target.value })}
                  aria-describedby={`${id}-hint`}
                  className="w-40"
                />
                <Button type="submit" size="sm" disabled={busy || !changed || !valid}>Save</Button>
                {setting.source === "setting" && (
                  <Button type="button" size="sm" variant="outline" disabled={busy} onClick={() => void save(name, null)}>
                    Use the built-in ({setting.built_in == null ? "no limit" : `${setting.built_in} ${retention ? "days" : "s"}`})
                  </Button>
                )}
              </div>
              <p id={`${id}-hint`} className="text-xs text-muted-foreground">
                {SOURCE_TEXT[setting.source]}. {help}
              </p>
            </form>
          );
        })}
        {error && <p role="alert" className="text-sm text-destructive">{error}</p>}
        {saved && !error && <p role="status" className="text-xs text-muted-foreground">Saved.</p>}
      </CardContent>
    </Card>
  );
}

type WaitName = keyof WaitSettings;
const WAIT_FIELDS: { name: WaitName; label: string }[] = [
  { name: "max_wait_seconds", label: "Longest wait" },
  { name: "min_check_seconds", label: "Shortest check interval" },
];

function WaitSettingsSection() {
  const [settings, setSettings] = useState<WaitSettings | null>(null);
  const [drafts, setDrafts] = useState<Record<WaitName, string>>({ max_wait_seconds: "", min_check_seconds: "" });
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [saved, setSaved] = useState(false);
  const idBase = useId();
  const show = (next: WaitSettings, only?: WaitName) => {
    setSettings(next);
    const stored = { max_wait_seconds: String(next.max_wait_seconds.value), min_check_seconds: String(next.min_check_seconds.value) };
    setDrafts((current) => only ? { ...current, [only]: stored[only] } : stored);
  };
  useEffect(() => {
    let alive = true;
    getWaitSettings().then(
      (next) => alive && show(next),
      (err) => alive && setError(errorText(err)),
    );
    return () => { alive = false; };
  }, []);

  const save = async (name: WaitName, value: number | null) => {
    setBusy(true);
    setError(null);
    setSaved(false);
    try {
      show(await updateWaitSettings({ [name]: value }), name);
      setSaved(true);
    } catch (err) {
      setError(errorText(err));
    } finally {
      setBusy(false);
    }
  };

  if (!settings) return error ? <p role="alert" className="text-sm text-destructive">{error}</p> : (
    <p className="flex items-center gap-2 text-sm text-muted-foreground">
      <Loader2 className="h-4 w-4 animate-spin motion-reduce:animate-none" aria-hidden /> Loading waits
    </p>
  );

  return (
    <Card>
      <CardHeader><CardTitle>Waits</CardTitle></CardHeader>
      <CardContent className="space-y-5">
        {WAIT_FIELDS.map(({ name, label }) => {
          const setting = settings[name];
          const id = `${idBase}-${name}`;
          const typed = drafts[name].trim();
          const valid = /^\d+$/.test(typed);
          return (
            <form key={name} className="space-y-1" onSubmit={(e) => { e.preventDefault(); void save(name, Number(typed)); }}>
              <label htmlFor={id} className="block text-xs font-medium text-muted-foreground">{label} (seconds)</label>
              <div className="flex flex-wrap items-center gap-2">
                <Input id={id} inputMode="numeric" value={drafts[name]}
                  onChange={(e) => setDrafts({ ...drafts, [name]: e.target.value })}
                  aria-describedby={`${id}-hint`} className="w-40" />
                <Button type="submit" size="sm" disabled={busy || typed === String(setting.value) || !valid}>Save</Button>
                {setting.source === "setting" && (
                  <Button type="button" size="sm" variant="outline" disabled={busy} onClick={() => void save(name, null)}>
                    Use the built-in ({waitDuration(setting.default)})
                  </Button>
                )}
              </div>
              <p id={`${id}-hint`} className="text-xs text-muted-foreground">
                {SOURCE_TEXT[setting.source]}. Built-in default: {waitDuration(setting.default)} ({setting.default} s).
                {valid && ` Entered: ${waitDuration(Number(typed))}.`}
              </p>
            </form>
          );
        })}
        {error && <p role="alert" className="text-sm text-destructive">{error}</p>}
        {saved && !error && <p role="status" className="text-xs text-muted-foreground">Saved.</p>}
      </CardContent>
    </Card>
  );
}

export function SchedulerSection() {
  return <div className="space-y-6"><ScheduleSettings /><WaitSettingsSection /></div>;
}
