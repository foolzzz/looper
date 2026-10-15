import { useEffect, useId, useState } from "react";
import { AlertTriangle, Bot, Save, Loader2, MessageCircle, Search, Send, CheckCircle2, XCircle, Plus, Trash2, UserPlus } from "lucide-react";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Switch } from "@/components/ui/switch";
import {
  fetchChannelConfigs,
  fetchLarkStatus,
  searchLarkPeople,
  updateChannelConfig,
  type ChannelConfig,
  type LarkPerson,
  type LarkStatus,
} from "@/lib/api";
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
    hint: "Each sender's open_id for this bot's app, starting with ou_ (not a union_id or a user_id); open_ids differ per app. Only direct messages to the bot are handled.",
  },
};
const MAX_SENDERS = 100;

/** The server's secret sentinel: sending it back keeps the stored token unchanged (looper/agents/profile_registry.py). */
const SECRET_MASK = "***";

/** Channels whose bot token is stored server-side (masked). */
const TOKEN_CHANNELS: Record<string, { label: string; hint: string }> = {
  telegram: { label: "Bot token", hint: "From @BotFather. Stored on the server and never shown; leave as is to keep it, clear to remove it." },
};

/** Config keys the server owns: shown elsewhere or not at all, never as an editable field. */
const SERVER_KEYS = ["allowlist_app_id"];

/** Placeholders and hints for config fields that are not environment variable names. */
const FIELD_HELP: Record<string, { placeholder: string; hint: string }> = {
  profile: {
    placeholder: "Empty: lark-cli's active profile",
    hint: "The lark-cli profile the channel runs as (`lark-cli profile list`): it chooses the app, and so the bot.",
  },
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
  const skip = ["description", ...SERVER_KEYS, ...(allowlist ? [allowlist.field] : [])];
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
  const isLark = channel.channel_type === "lark";
  // The Lark channel as lark-cli sees its saved settings: the bot, names, refused senders. Read again after a save.
  const [lark, setLark] = useState<LarkStatus | null>(null);
  const [larkError, setLarkError] = useState("");
  useEffect(() => {
    if (!isLark) return;
    let alive = true;
    fetchLarkStatus()
      .then((status) => alive && (setLark(status), setLarkError("")))
      .catch((e: Error) => alive && setLarkError(e.message));
    return () => {
      alive = false;
    };
  }, [isLark, channel.updated_at]);
  // An import's "secrets to fill in" links here (#settings/channels/<id>).
  const ref = useSettingsTarget<HTMLDivElement>("channel", channel.channel_id);

  const savedIds = allowlist ? storedIds(saved[allowlist.field]) : [];
  const savedToken = channel.has_bot_token ? SECRET_MASK : "";
  const dirty =
    enabled !== channel.enabled ||
    JSON.stringify(fields) !== JSON.stringify(stringFields(saved, skip)) ||
    JSON.stringify(ids) !== JSON.stringify(savedIds) ||
    (!!tokenChannel && botToken !== savedToken);
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
      : channel.start_problem ?? lark?.problem ?? null;

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
      // Reflect the new stored state: a sent non-empty token is now kept (masked), a sent empty one is gone.
      if (payload.bot_token !== undefined) setBotToken(payload.bot_token === "" ? "" : SECRET_MASK);
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
            {isLark && <LarkBotLine status={lark} error={larkError} />}
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
              placeholder={key.endsWith("_env") ? "Environment variable name" : FIELD_HELP[key]?.placeholder ?? ""}
              className="w-full rounded-md border border-input bg-background px-3 py-2 text-sm text-foreground placeholder:text-muted-foreground outline-none focus:ring-2 focus:ring-ring"
            />
            {key.endsWith("_env") ? (
              <p className="mt-1 text-[10px] text-muted-foreground">
                The server reads the actual value from this environment variable at startup
              </p>
            ) : FIELD_HELP[key] ? (
              <p className="mt-1 text-[10px] text-muted-foreground">{FIELD_HELP[key].hint}</p>
            ) : null}
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
                    aria-label={`Allowed sender ${index + 1}${lark?.names[value.trim()] ? `, ${lark.names[value.trim()]}` : ""}`}
                    value={value}
                    onChange={(e) => setIds(ids.map((current, i) => (i === index ? e.target.value : current)))}
                    placeholder={allowlist.placeholder}
                    aria-invalid={!!idProblems[index]}
                    className="flex-1 font-mono"
                  />
                  {isLark && lark?.names[value.trim()] && (
                    <span className="max-w-[40%] shrink-0 truncate text-xs text-foreground">{lark.names[value.trim()]}</span>
                  )}
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

        {isLark && (
          <LarkPeople
            status={lark}
            ids={ids.map((value) => value.trim())}
            onAdd={(openId) => setIds((current) => (current.some((v) => v.trim() === openId) ? current : [...current, openId]))}
            full={ids.length >= MAX_SENDERS}
          />
        )}

        <div className="flex items-center justify-between gap-2 pt-2">
          <p className="text-[10px] text-muted-foreground">
            Channel ID: <code className="text-foreground">{channel.channel_id}</code>
            {" · "}Updated: {new Date(channel.updated_at).toLocaleString()}
            {" · "}Saved changes take effect after the server restarts.
          </p>
          <div className="flex items-center gap-2">
            {error && <p role="alert" className="text-xs text-destructive">{error}</p>}
            {justSaved && (
              <span className="flex items-center gap-1 text-xs text-success">
                <CheckCircle2 className="h-3.5 w-3.5" />
                Saved — restart to apply
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

function shortId(id: string): string {
  return id.length > 14 ? `${id.slice(0, 12)}…` : id;
}

/** "Bot: HBDataBot (cli_a945…, profile …)", or why lark-cli cannot act as the bot. */
function LarkBotLine({ status, error }: { status: LarkStatus | null; error: string }) {
  if (error) return <p className="mt-0.5 text-xs text-destructive">Bot: {error}</p>;
  if (!status) return <p className="mt-0.5 text-xs text-muted-foreground">Bot: checking lark-cli…</p>;
  if (!status.bot) return <p className="mt-0.5 text-xs text-warning">Bot: unknown, lark-cli cannot act as it</p>;
  return (
    <p className="mt-0.5 flex items-center gap-1 text-xs text-foreground">
      <Bot className="h-3.5 w-3.5 text-muted-foreground" aria-hidden="true" />
      <span>
        Bot: <span className="font-medium">{status.bot.name}</span>{" "}
        <span className="text-muted-foreground">
          ({shortId(status.bot.app_id)}, profile {status.bot.profile})
        </span>
      </span>
    </p>
  );
}

/** Add Lark senders by name or email, and let in the ones the allowlist refused lately. Adding changes the draft
 * allowlist above; Save stores it. */
function LarkPeople({
  status,
  ids,
  onAdd,
  full,
}: {
  status: LarkStatus | null;
  ids: string[];
  onAdd: (openId: string) => void;
  full: boolean;
}) {
  const id = useId();
  const [query, setQuery] = useState("");
  const [people, setPeople] = useState<LarkPerson[] | null>(null);
  const [searching, setSearching] = useState(false);
  const [error, setError] = useState("");
  const listed = (openId: string) => ids.includes(openId);

  const search = (event: React.FormEvent) => {
    event.preventDefault();
    if (!query.trim()) return;
    setSearching(true);
    setError("");
    searchLarkPeople(query.trim())
      .then(setPeople, (e: Error) => setError(e.message))
      .finally(() => setSearching(false));
  };

  const rejected = (status?.rejected ?? []).filter((r) => !listed(r.open_id));
  return (
    <div className="space-y-4 rounded-md border border-border px-4 py-3">
      <form onSubmit={search} className="space-y-2" role="search" aria-label="Find a Lark sender">
        <label htmlFor={`${id}-q`} className="block text-xs font-medium text-muted-foreground">
          Add a sender by name or email
        </label>
        <div className="flex gap-2">
          <Input
            id={`${id}-q`}
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            placeholder="Name or email"
            maxLength={50}
            className="flex-1"
          />
          <Button type="submit" variant="outline" size="sm" disabled={searching || !query.trim()}>
            {searching ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <Search className="h-3.5 w-3.5" />}
            Search
          </Button>
        </div>
        {error && <p role="alert" className="text-xs text-destructive">{error}</p>}
        {people && (
          <ul aria-live="polite" className="space-y-1">
            {people.length === 0 && <li className="text-xs text-muted-foreground">Nobody found</li>}
            {people.map((person) => (
              <li key={person.open_id} className="flex flex-wrap items-center gap-x-2 gap-y-1 text-xs">
                <span className="font-medium text-foreground">{person.name || person.open_id}</span>
                <span className="min-w-0 max-w-full truncate text-muted-foreground">
                  {[person.email, person.department].filter(Boolean).join(" · ")}
                </span>
                <Button
                  type="button"
                  variant="ghost"
                  size="sm"
                  className="ml-auto"
                  disabled={listed(person.open_id) || full}
                  onClick={() => onAdd(person.open_id)}
                  aria-label={`Add ${person.name || person.open_id}`}
                >
                  <UserPlus className="h-3.5 w-3.5" />
                  {listed(person.open_id) ? "Listed" : "Add"}
                </Button>
              </li>
            ))}
          </ul>
        )}
        <p className="text-[10px] text-muted-foreground">
          Searches as the user lark-cli is signed in as; the open_ids found are this app&apos;s.
        </p>
      </form>

      <div className="space-y-1">
        <span className="block text-xs font-medium text-muted-foreground">Recently refused senders</span>
        {rejected.length === 0 ? (
          <p className="text-xs text-muted-foreground">None</p>
        ) : (
          <ul className="space-y-1.5">
            {rejected.map((r) => (
              <li key={r.open_id} className="flex flex-wrap items-center gap-x-2 gap-y-1 text-xs">
                <span className="font-medium text-foreground">{r.name || shortId(r.open_id)}</span>
                <span className="text-muted-foreground">
                  {r.count > 1 ? `${r.count} messages, last ` : ""}
                  {new Date(r.last_at).toLocaleString()}
                </span>
                <Button
                  type="button"
                  variant="ghost"
                  size="sm"
                  className="ml-auto"
                  disabled={full}
                  onClick={() => onAdd(r.open_id)}
                  aria-label={`Allow ${r.name || r.open_id}`}
                >
                  <UserPlus className="h-3.5 w-3.5" />
                  Allow
                </Button>
              </li>
            ))}
          </ul>
        )}
      </div>
    </div>
  );
}

/** Settings → Channels: the Telegram and Lark (lark-cli) channels, each with its sender allowlist. */
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
