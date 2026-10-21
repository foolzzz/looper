import { useCallback, useEffect, useState } from "react";
import { AlertTriangle } from "lucide-react";
import { SELECT } from "@/components/registry-ui";
import {
  fetchManagerSession,
  setManagerSessionProfile,
  type ManagerProfileChoice,
  type ManagerSessionView as SessionView,
} from "@/lib/api";
import { profileLabel } from "@/lib/use-registry";

export function sourceText(view: Pick<SessionView, "effective">): string {
  return view.effective.source === "session" ? "this session" : "Settings";
}

export function confirmText(choice: ManagerProfileChoice): string | null {
  if (choice.effect !== "new_session") return null;
  return `The next message first has the current session write a handoff, then starts a new session on ${choice.name}. The conversation itself does not carry over; the handoff and recent messages do.`;
}

export function ManagerSessionProfileView({
  view,
  busy,
  error,
  onSelect,
}: {
  view: SessionView;
  busy: boolean;
  error: string | null;
  onSelect: (profileId: string | null) => void;
}) {
  const profile = view.effective.profile;
  const name = profile ? profileLabel(profile) : "none";
  return (
    <div className="flex flex-col gap-1 text-xs">
      <div className="flex items-center gap-1.5 text-muted-foreground">
        <span>Agent:</span>
        <span className="hidden text-foreground sm:inline">{name}</span>
        <span>({sourceText(view)})</span>
      </div>
      <label className="sr-only" htmlFor="manager-session-profile">Agent Profile for this session</label>
      <select
        id="manager-session-profile"
        value={view.selected?.profile_id ?? ""}
        disabled={busy}
        onChange={(e) => onSelect(e.target.value || null)}
        className={`${SELECT} h-7 text-xs`}
        title="Agent Profile for this session"
      >
        <option value="">Follow Settings</option>
        {view.choices.map((c) => (
          <option key={c.profile_id} value={c.profile_id} disabled={c.problem != null}>
            {c.number}. {c.name}
            {c.problem ? ` (${c.problem})` : ""}
          </option>
        ))}
      </select>
      {view.problem && (
        <p className="flex items-start gap-1 text-warning">
          <AlertTriangle className="mt-0.5 h-3.5 w-3.5 shrink-0" />
          {view.problem}
        </p>
      )}
      {error && (
        <p className="flex items-start gap-1 text-destructive">
          <AlertTriangle className="mt-0.5 h-3.5 w-3.5 shrink-0" />
          {error}
        </p>
      )}
    </div>
  );
}

export function ManagerSessionProfile({ refreshKey = 0 }: { refreshKey?: number }) {
  const [view, setView] = useState<SessionView | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    try {
      setView(await fetchManagerSession());
      setError(null);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    }
  }, []);

  useEffect(() => {
    load();
  }, [load, refreshKey]);

  const select = async (profileId: string | null) => {
    const choice = view?.choices.find((c) => c.profile_id === profileId);
    const text = choice ? confirmText(choice) : null;
    if (text !== null && !confirm(text)) return;
    setBusy(true);
    try {
      await setManagerSessionProfile(profileId);
      await load();
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  };

  if (!view) {
    return error ? (
      <span className="flex items-center gap-1 text-xs text-destructive">
        <AlertTriangle className="h-3.5 w-3.5" />
        {error}
      </span>
    ) : null;
  }
  return <ManagerSessionProfileView view={view} busy={busy} error={error} onSelect={select} />;
}
