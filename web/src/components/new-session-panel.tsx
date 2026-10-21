import { useState } from "react";
import { SELECT } from "@/components/registry-ui";
import { Button } from "@/components/ui/button";
import type { ManagerProfileChoice } from "@/lib/api";

export function NewSessionPanel({
  settingsName,
  choices,
  onStart,
  onCancel,
  busy,
}: {
  settingsName: string | null;
  choices: ManagerProfileChoice[];
  onStart: (profileId: string | null) => void;
  onCancel: () => void;
  busy?: boolean;
}) {
  const [value, setValue] = useState("");
  return (
    <div
      className="space-y-2 border-b border-border px-4 py-3 text-xs"
      onKeyDown={(e) => {
        if (e.key === "Escape") onCancel();
      }}
    >
      <h3 className="text-sm font-medium">New Manager session</h3>
      <p className="text-muted-foreground">Every channel shares the Manager's session. The next message starts clean.</p>
      <label className="sr-only" htmlFor="new-session-profile">Agent Profile for the new session</label>
      <select
        id="new-session-profile"
        value={value}
        onChange={(e) => setValue(e.target.value)}
        disabled={busy}
        className={`${SELECT} h-8 text-xs`}
      >
        <option value="">{settingsName ? `Follow Settings (${settingsName})` : "Follow Settings"}</option>
        {choices.map((c) => (
          <option key={c.profile_id} value={c.profile_id} disabled={c.problem != null}>
            {c.number}. {c.name}
            {c.problem ? ` (${c.problem})` : ""}
          </option>
        ))}
      </select>
      <div className="flex gap-2">
        <Button size="sm" disabled={busy} onClick={() => onStart(value || null)}>Start new session</Button>
        <Button size="sm" variant="outline" onClick={onCancel}>Cancel</Button>
      </div>
    </div>
  );
}
