import { useEffect, useId, useState } from "react";
import { Button } from "@/components/ui/button";
import { Card } from "@/components/ui/card";
import { fetchChatSettings, updateChatSettings, type ChatSettings } from "@/lib/api";
import { errorText } from "@/lib/use-action";

/** Settings → Chat: whether a reply can be stopped (Esc, the Stop button, /stop). Experimental, off by default. */
export function ChatSettingsSection() {
  const id = useId();
  const [settings, setSettings] = useState<ChatSettings | null>(null);
  const [error, setError] = useState("");
  const [attempt, setAttempt] = useState(0);
  const [saving, setSaving] = useState(false);

  useEffect(() => {
    let alive = true;
    fetchChatSettings().then((value) => {
      if (alive) setSettings(value);
    }).catch((err: unknown) => {
      if (alive) setError(errorText(err));
    });
    return () => { alive = false; };
  }, [attempt]);

  const save = async (stop_enabled: boolean) => {
    setSaving(true);
    setError("");
    try {
      setSettings(await updateChatSettings({ stop_enabled }));
    } catch (err) {
      setError(errorText(err));
    } finally {
      setSaving(false);
    }
  };

  return (
    <Card className="min-w-0 p-5">
      {!settings ? (
        error ? (
          <div className="space-y-3">
            <p role="alert" className="break-words text-sm text-destructive">{error}</p>
            <Button variant="outline" onClick={() => { setError(""); setAttempt((n) => n + 1); }}>Retry</Button>
          </div>
        ) : (
          <p role="status" className="text-sm text-muted-foreground">Loading…</p>
        )
      ) : (
        <div className="space-y-1.5">
          <label htmlFor={`${id}-stop`} className="flex items-center gap-2 text-sm font-medium">
            <input id={`${id}-stop`} type="checkbox" checked={settings.stop_enabled} disabled={saving}
                   aria-describedby={`${id}-stop-help`} onChange={(event) => void save(event.target.checked)} />
            Allow stopping a reply
          </label>
          <p id={`${id}-stop-help`} className="text-xs text-muted-foreground">
            Experimental, off by default: Esc, the Stop button, and /stop on Telegram and Lark. Telegram's command menu
            lists /stop after the server restarts.
          </p>
          {error && <p role="alert" className="break-words text-sm text-destructive">{error}</p>}
        </div>
      )}
    </Card>
  );
}
