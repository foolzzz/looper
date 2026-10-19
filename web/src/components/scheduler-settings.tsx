import { useEffect, useId, useState } from "react";
import { Loader2 } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import {
  fetchSchedulerSettings,
  updateSchedulerSettings,
  type SchedulerSetting,
  type SchedulerSettings,
} from "@/lib/api";

const errorText = (err: unknown) => (err instanceof Error ? err.message : String(err));

const SOURCE_TEXT: Record<SchedulerSetting["source"], string> = {
  setting: "Saved here",
  env: "From the deprecated LOOPER_SCHEDULE_MIN_INTERVAL_SECONDS variable; save a value to replace it",
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
];

/** Settings → Scheduler: the two system-wide limits of schedules, each with its effective value and source. */
export function SchedulerSection() {
  const [settings, setSettings] = useState<SchedulerSettings | null>(null);
  const [drafts, setDrafts] = useState<Record<Name, string>>({ min_interval_seconds: "", run_timeout_seconds: "" });
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
          return (
            <form
              key={name}
              className="space-y-1"
              onSubmit={(e) => {
                e.preventDefault();
                void save(name, Number(typed));
              }}
            >
              <label htmlFor={id} className="block text-xs font-medium text-muted-foreground">{label}</label>
              <div className="flex flex-wrap items-center gap-2">
                <Input
                  id={id}
                  inputMode="numeric"
                  value={drafts[name]}
                  onChange={(e) => setDrafts({ ...drafts, [name]: e.target.value })}
                  aria-describedby={`${id}-hint`}
                  className="w-40"
                />
                <Button type="submit" size="sm" disabled={busy || !changed || !/^\d+$/.test(typed)}>Save</Button>
                {setting.source === "setting" && (
                  <Button type="button" size="sm" variant="outline" disabled={busy} onClick={() => void save(name, null)}>
                    Use the built-in ({setting.built_in == null ? "no limit" : `${setting.built_in} s`})
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
