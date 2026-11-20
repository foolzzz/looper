import { useEffect, useId, useState } from "react";
import { Button } from "@/components/ui/button";
import { Card } from "@/components/ui/card";
import { configureLedger, fetchLedgerState, type LedgerState } from "@/lib/ledger-api";
import { errorText } from "@/lib/use-action";

/** Settings → Chat: the ledger's switch. Off by default; while it is off the Manager has no ledger tools. */
export function LedgerSettingsSection() {
  const id = useId();
  const [state, setState] = useState<LedgerState | null>(null);
  const [error, setError] = useState("");
  const [attempt, setAttempt] = useState(0);
  const [saving, setSaving] = useState(false);

  useEffect(() => {
    let alive = true;
    fetchLedgerState().then((value) => { if (alive) setState(value); })
      .catch((err: unknown) => { if (alive) setError(errorText(err)); });
    return () => { alive = false; };
  }, [attempt]);

  const save = async (enabled: boolean) => {
    setSaving(true);
    setError("");
    try {
      setState(await configureLedger(enabled));
    } catch (err) {
      setError(errorText(err));
    } finally {
      setSaving(false);
    }
  };

  return (
    <Card className="min-w-0 p-5">
      {!state ? (
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
          <label htmlFor={`${id}-ledger`} className="flex items-center gap-2 text-sm font-medium">
            <input id={`${id}-ledger`} type="checkbox" checked={state.enabled} disabled={saving}
                   aria-describedby={`${id}-ledger-help`} onChange={(event) => void save(event.target.checked)} />
            Keep a ledger of to-dos and decisions
          </label>
          <p id={`${id}-ledger-help`} className="text-xs text-muted-foreground">
            Off by default. On, the Manager can write down what you decide and list what waits for you, and the To-do
            page shows gates, confirmations, tasks and decisions in one place. Only your own messages (this dashboard,
            or a direct chat with a bot whose My ID is set) can confirm or close an item.
            {state.enabled && state.operator_unset && " A chat bot has no My ID set yet: Settings → Channels."}
          </p>
          {error && <p role="alert" className="break-words text-sm text-destructive">{error}</p>}
        </div>
      )}
    </Card>
  );
}
