import { useEffect, useId, useState } from "react";
import { AlertTriangle, Save, Loader2, MessageCircle, Send, CheckCircle2, XCircle, Plus, Trash2 } from "lucide-react";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Switch } from "@/components/ui/switch";
import { fetchChannelConfigs, updateChannelConfig, type ChannelConfig } from "@/lib/api";
import { useSettingsTarget } from "@/lib/settings-target";
import { cn } from "@/lib/utils";

function channelIcon(type: string) {
  if (type === "telegram") return Send;
  return MessageCircle;
}

/** Each chat channel's sender allowlist, as the server checks it (looper/agents/channels/allowlist.py). */
const SENDER_ALLOWLISTS: Record<string, { field: string; label: string; pattern: RegExp; placeholder: string; hint: string }> = {
  telegram: {
    field: "allowed_user_ids",
    label: "Telegram user IDs",
    pattern: /^[1-9][0-9]{0,19}$/,
    placeholder: "123456789",
    hint: "Each sender's numeric Telegram user ID (a negative number is a group or channel, not a sender).",
  },
  lark: {
    field: "allowed_open_ids",
    label: "Lark open_ids",
    pattern: /^ou_[0-9A-Za-z]{8,64}$/,
    placeholder: "ou_…",
    hint: "Each sender's open_id for this app, starting with ou_ (not a union_id or a user_id).",
  },
};
const MAX_SENDERS = 100;

/** The server's secret sentinel: sending it back keeps the stored token unchanged (looper/agents/profile_registry.py). */
const SECRET_MASK = "***";

/** Channels whose bot token is stored server-side (masked). */
const TOKEN_CHANNELS: Record<string, { label: string; hint: string }> = {
  telegram: { label: "Bot token", hint: "From @BotFather. Stored on the server and never shown; leave as is to keep it, clear to remove it." },
};

/** The ids stored in an allowlist field: a list, or one comma-separated string as older Telegram settings hold it. */
function storedIds(raw: unknown): string[] {
  if (Array.isArray(raw)) return raw.map(String);
  if (typeof raw === "string") return raw.split(",").map((id) => id.trim()).filter(Boolean);
  return [];
}

function stringFields(config: Record<string, unknown>, skip: string[]) {
  const fields: Record<string, string> = {};
  for (const [key, value] of Object.entries(config)) {
    if (!skip.includes(key) && typeof value === "string") fields[key] = value;
  }
  return fields;
}

function ChannelCard({ channel, onSaved }: { channel: ChannelConfig; onSaved: () => void }) {
  const id = useId();
  const allowlist = SENDER_ALLOWLISTS[channel.channel_type];
  const saved = channel.config || {};
  const skip = ["description", ...(allowlist ? [allowlist.field] : [])];
  const [enabled, setEnabled] = useState(channel.enabled);
  const [fields, setFields] = useState<Record<string, string>>(stringFields(saved, skip));
  const [ids, setIds] = useState<string[]>(allowlist ? storedIds(saved[allowlist.field]) : []);
  const tokenChannel = TOKEN_CHANNELS[channel.channel_type];
  // Prefilled with the mask when a token is stored: sending it back unchanged keeps it.
  const [botToken, setBotToken] = useState(channel.has_bot_token ? SECRET_MASK : "");
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState("");
  const [justSaved, setJustSaved] = useState(false);
  const Icon = channelIcon(channel.channel_type);
  // An import's "secrets to fill in" links here (#settings/channels/<id>).
  const ref = useSettingsTarget<HTMLDivElement>("channel", channel.channel_id);

  const savedIds = allowlist ? storedIds(saved[allowlist.field]) : [];
  const dirty =
    enabled !== channel.enabled ||
    JSON.stringify(fields) !== JSON.stringify(stringFields(saved, skip)) ||
    JSON.stringify(ids) !== JSON.stringify(savedIds);
  const idProblems = ids.map((value, index) => {
    const text = value.trim();
    if (!text) return "Enter an ID or remove this row.";
    if (allowlist && !allowlist.pattern.test(text)) return `Not a valid ${allowlist.label.replace(/s$/, "")}.`;
    if (ids.findIndex((other) => other.trim() === text) !== index) return "Listed twice.";
    return "";
  });
  const invalid = idProblems.some(Boolean);
  // Why the channel does not start: the saved state from the server, or the same rule for unsaved edits.
  const startProblem = !allowlist
    ? null
    : dirty
      ? ids.length === 0
        ? "its sender allowlist is empty; nobody could reach the Manager through it"
        : null
      : channel.start_problem ?? null;

  const handleSave = async () => {
    setSaving(true);
    setError("");
    try {
      const config: Record<string, unknown> = { ...saved, ...fields };
      if (allowlist) config[allowlist.field] = ids.map((value) => value.trim());
      const payload: Partial<ChannelConfig> & { bot_token?: string } = { enabled, config };
      // Only send the token when it changed from the mask; the mask would keep it anyway, but this avoids a needless write.
      if (tokenChannel && botToken !== SECRET_MASK) payload.bot_token = botToken;
      await updateChannelConfig(channel.channel_id, payload);
      setJustSaved(true);
      setTimeout(() => setJustSaved(false), 2000);
      onSaved();
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setSaving(false);
    }
  };

  const description = typeof saved.description === "string" ? saved.description : "";

  return (
    <div ref={ref} className={cn(
      "rounded-lg border bg-card overflow-hidden",
      enabled ? "border-primary/40" : "border-border",
    )}>
      <div className="flex items-center justify-between px-5 py-4 border-b border-border">
        <div className="flex items-center gap-3">
          <div className={cn(
            "flex h-10 w-10 items-center justify-center rounded-lg",
            enabled ? "bg-primary/10 text-primary" : "bg-muted text-muted-foreground",
          )}>
            <Icon className="h-5 w-5" />
          </div>
          <div>
            <h3 className="text-sm font-semibold">{channel.display_name}</h3>
            <p className="text-xs text-muted-foreground">{description}</p>
          </div>
        </div>
        <div className="flex items-center gap-3">
          <Badge variant={enabled ? "success" : "outline"}>
            {enabled ? "Enabled" : "Disabled"}
          </Badge>
          <Switch checked={enabled} onChange={setEnabled} label={`Enable ${channel.display_name}`} />
        </div>
      </div>

      <div className="px-5 py-4 space-y-4">
        {enabled && startProblem && (
          <div role="status" className="flex items-start gap-2 rounded-md border border-warning/50 bg-warning/10 px-3 py-2 text-xs text-foreground">
            <AlertTriangle className="mt-0.5 h-3.5 w-3.5 shrink-0 text-warning" />
            <span>Does not start: {startProblem}.</span>
          </div>
        )}

        {tokenChannel && (
          <div>
            <label htmlFor={`${id}-bot_token`} className="block text-xs font-medium text-muted-foreground mb-1.5">
              {tokenChannel.label}
            </label>
            <input
              id={`${id}-bot_token`}
              type="password"
              autoComplete="off"
              value={botToken}
              onChange={(e) => setBotToken(e.target.value)}
              placeholder={channel.has_bot_token ? "" : "123456:ABC-DEF…"}
              className="w-full rounded-md border border-input bg-background px-3 py-2 text-sm text-foreground placeholder:text-muted-foreground outline-none focus:ring-2 focus:ring-ring"
            />
            <p className="mt-1 text-[10px] text-muted-foreground">{tokenChannel.hint}</p>
          </div>
        )}

        {Object.keys(fields).map((key) => (
          <div key={key}>
            <label htmlFor={`${id}-${key}`} className="block text-xs font-medium text-muted-foreground mb-1.5">
              {key.replace(/_/g, " ").replace(/\b\w/g, (c) => c.toUpperCase())}
            </label>
            <input
              id={`${id}-${key}`}
              type="text"
              value={fields[key] || ""}
              onChange={(e) => setFields({ ...fields, [key]: e.target.value })}
              placeholder={key.endsWith("_env") ? "Environment variable name" : ""}
              className="w-full rounded-md border border-input bg-background px-3 py-2 text-sm text-foreground placeholder:text-muted-foreground outline-none focus:ring-2 focus:ring-ring"
            />
            {key.endsWith("_env") && (
              <p className="mt-1 text-[10px] text-muted-foreground">
                The server reads the actual value from this environment variable at startup
              </p>
            )}
          </div>
        ))}

        {allowlist && (
          <div className="space-y-2">
            <div className="flex items-baseline justify-between gap-2">
              <span className="text-xs font-medium text-muted-foreground">Allowed senders ({allowlist.label})</span>
              <span className="text-[11px] tabular-nums text-muted-foreground">{ids.length} / {MAX_SENDERS}</span>
            </div>
            {ids.map((value, index) => (
              <div key={index} className="space-y-1">
                <div className="flex items-center gap-2">
                  <Input
                    aria-label={`Allowed sender ${index + 1}`}
                    value={value}
                    onChange={(e) => setIds(ids.map((current, i) => (i === index ? e.target.value : current)))}
                    placeholder={allowlist.placeholder}
                    aria-invalid={!!idProblems[index]}
                    className="flex-1 font-mono"
                  />
                  <Button
                    type="button"
                    variant="ghost"
                    size="sm"
                    onClick={() => setIds(ids.filter((_, i) => i !== index))}
                    aria-label={`Remove ${value.trim() || `sender ${index + 1}`}`}
                  >
                    <Trash2 className="h-3.5 w-3.5" />
                  </Button>
                </div>
                {idProblems[index] && <p className="text-xs text-destructive">{idProblems[index]}</p>}
              </div>
            ))}
            <Button
              type="button"
              variant="outline"
              size="sm"
              onClick={() => setIds([...ids, ""])}
              disabled={ids.length >= MAX_SENDERS}
            >
              <Plus className="h-3.5 w-3.5" />
              Add sender
            </Button>
            <p className="text-xs text-muted-foreground">
              {allowlist.hint} Only these senders reach the Manager, which runs any command on this machine without
              asking; messages from anyone else are dropped and logged, with no reply. With an empty list the channel
              does not start.
            </p>
          </div>
        )}

        <div className="flex items-center justify-between gap-2 pt-2">
          <p className="text-[10px] text-muted-foreground">
            Channel ID: <code className="text-foreground">{channel.channel_id}</code>
            {" · "}Updated: {new Date(channel.updated_at).toLocaleString()}
          </p>
          <div className="flex items-center gap-2">
            {error && <p role="alert" className="text-xs text-destructive">{error}</p>}
            {justSaved && (
              <span className="flex items-center gap-1 text-xs text-success">
                <CheckCircle2 className="h-3.5 w-3.5" />
                Saved
              </span>
            )}
            <Button type="button" size="sm" onClick={handleSave} disabled={saving || !dirty || invalid}>
              {saving ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <Save className="h-3.5 w-3.5" />}
              Save
            </Button>
          </div>
        </div>
      </div>
    </div>
  );
}

/** Settings → Channels: the Telegram and Lark channels, each with its sender allowlist. */
export function ChannelsSection() {
  const [channels, setChannels] = useState<ChannelConfig[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");

  const load = () => {
    fetchChannelConfigs()
      .then((configs) => {
        setChannels(configs);
        setError("");
      })
      .catch((e) => setError(e.message))
      .finally(() => setLoading(false));
  };

  useEffect(() => { load(); }, []);

  return (
    <div className="space-y-4">
      {error && !channels.length && (
        <div className="rounded-lg border border-destructive/50 bg-destructive/10 px-5 py-4 text-sm text-destructive">
          {error}
        </div>
      )}

      {loading ? (
        <div className="flex h-32 items-center justify-center">
          <Loader2 className="h-6 w-6 animate-spin text-muted-foreground" />
        </div>
      ) : channels.length === 0 ? (
        <div className="flex h-32 items-center justify-center rounded-lg border border-dashed border-border">
          <div className="text-center">
            <XCircle className="mx-auto h-8 w-8 text-muted-foreground/40" />
            <p className="mt-2 text-sm text-muted-foreground">No channel configurations found</p>
            <p className="text-xs text-muted-foreground">Restart the server to seed defaults</p>
          </div>
        </div>
      ) : (
        channels.map((ch) => <ChannelCard key={ch.channel_id} channel={ch} onSaved={load} />)
      )}
    </div>
  );
}
