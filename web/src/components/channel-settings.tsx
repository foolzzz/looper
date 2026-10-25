import { useEffect, useId, useState } from "react";
import {
  AlertTriangle,
  Bot,
  Save,
  Loader2,
  MessageCircle,
  MessageCircleQuestionMark,
  Search,
  Send,
  CheckCircle2,
  XCircle,
  Plus,
  Trash2,
  UserPlus,
} from "lucide-react";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Switch } from "@/components/ui/switch";
import { ChannelChatBindings } from "@/components/channel-chat-bindings";
import { LABEL, SELECT } from "@/components/registry-ui";
import {
  createChannelBot,
  deleteChannelBot,
  fetchChannelConfigs,
  fetchManagers,
  fetchGateSettings,
  fetchLarkStatus,
  searchLarkPeople,
  updateChannelConfig,
  updateGateSettings,
  type ChannelConfig,
  type GateSettings,
  type GateTarget,
  type LarkPerson,
  type LarkStatus,
  type ManagerInfo,
} from "@/lib/api";
import { larkPeopleSearchNote } from "@/lib/chat-channels";
import { useSettingsTarget } from "@/lib/settings-target";
import { errorText, useAction } from "@/lib/use-action";
import { cn } from "@/lib/utils";

function channelIcon(type: string) {
  if (type === "telegram") return Send;
  return MessageCircle;
}

const LARK_ALLOWLIST = {
  field: "allowed_open_ids",
  label: "Lark open_ids",
  pattern: /^ou_[0-9A-Za-z]{8,64}$/,
  placeholder: "ou_…",
  hint: "Each sender's open_id for this bot's app, starting with ou_ (not a union_id or a user_id); open_ids differ per app. Direct messages to the bot are handled, and in a group only the messages that @-mention it.",
};

/** Each chat channel's sender allowlist, as the server checks it (looper/agents/channels/allowlist.py). */
const SENDER_ALLOWLISTS: Record<string, { field: string; label: string; pattern: RegExp; placeholder: string; hint: string }> = {
  telegram: {
    field: "allowed_user_ids",
    label: "Telegram user IDs",
    pattern: /^[1-9][0-9]{0,19}$/,
    placeholder: "123456789",
    hint: "Each sender's numeric Telegram user ID (a negative number is a group or channel, not a sender).",
  },
  lark: LARK_ALLOWLIST,
  lark_bot: LARK_ALLOWLIST,
};
const MAX_SENDERS = 100;

/** The server's secret sentinel: sending it back keeps the stored token unchanged (looper/agents/profile_registry.py). */
const SECRET_MASK = "***";

/** Channels whose bot token is stored server-side (masked). */
const TOKEN_CHANNELS: Record<string, { label: string; hint: string }> = {
  telegram: { label: "Bot token", hint: "From @BotFather. Stored on the server and never shown; leave as is to keep it, clear to remove it." },
  lark_bot: { label: "App secret", hint: "From the Lark developer console (Credentials & Basic Info). Stored on the server and never shown; leave as is to keep it, clear to remove it." },
};

/** Config keys the server owns: shown elsewhere or not at all, never as an editable field. */
const SERVER_KEYS = ["allowlist_app_id"];

/** Placeholders and hints for config fields that are not environment variable names. */
const FIELD_HELP: Record<string, { placeholder: string; hint: string }> = {
  profile: {
    placeholder: "Empty: lark-cli's active profile",
    hint: "The lark-cli profile the channel runs as (`lark-cli profile list`): it chooses the app, and so the bot.",
  },
  app_id: {
    placeholder: "cli_…",
    hint: "The app's App ID. Enable the bot capability, subscribe to the im.message.receive_v1 event with the long-connection mode, and grant the im:message, im:message:send_as_bot, im:message.reactions:write_only and contact read scopes.",
  },
  domain: {
    placeholder: "feishu",
    hint: "feishu for open.feishu.cn, lark for open.larksuite.com",
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

type Allowlist = (typeof SENDER_ALLOWLISTS)[string];

/** A chat channel's sender allowlist: one id per row, each checked against the channel's id pattern. */
function AllowlistEditor({
  allowlist,
  ids,
  setIds,
  problems,
  names,
}: {
  allowlist: Allowlist;
  ids: string[];
  setIds: (ids: string[]) => void;
  problems: string[];
  names?: Record<string, string>;
}) {
  return (
        <div className="space-y-2">
          <div className="flex items-baseline justify-between gap-2">
            <span className="text-xs font-medium text-muted-foreground">Allowed senders ({allowlist.label})</span>
            <span className="text-[11px] tabular-nums text-muted-foreground">{ids.length} / {MAX_SENDERS}</span>
          </div>
          {ids.map((value, index) => (
            <div key={index} className="space-y-1">
              <div className="flex items-center gap-2">
                <Input
                  aria-label={`Allowed sender ${index + 1}${names?.[value.trim()] ? `, ${names[value.trim()]}` : ""}`}
                  value={value}
                  onChange={(e) => setIds(ids.map((current, i) => (i === index ? e.target.value : current)))}
                  placeholder={allowlist.placeholder}
                  aria-invalid={!!problems[index]}
                  className="flex-1 font-mono"
                />
                {names?.[value.trim()] && (
                  <span className="max-w-[40%] shrink-0 truncate text-xs text-foreground">{names[value.trim()]}</span>
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
              {problems[index] && <p className="text-xs text-destructive">{problems[index]}</p>}
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
  );
}

/** Why each id of an allowlist is not acceptable ("" when it is). */
function allowlistProblems(ids: string[], allowlist?: Allowlist): string[] {
  return ids.map((value, index) => {
    const text = value.trim();
    if (!text) return "Enter an ID or remove this row.";
    if (allowlist && !allowlist.pattern.test(text)) return `Not a valid ${allowlist.label.replace(/s$/, "")}.`;
    if (ids.findIndex((other) => other.trim() === text) !== index) return "Listed twice.";
    return "";
  });
}

function ChannelCard({ channel, onSaved, freshlySaved = false }: { channel: ChannelConfig; onSaved: () => void; freshlySaved?: boolean }) {
  const id = useId();
  const allowlist = SENDER_ALLOWLISTS[channel.channel_type];
  const saved = channel.config || {};
  const skip = ["description", ...SERVER_KEYS, ...(channel.builtin === false ? ["bot_token_env"] : []), ...(allowlist ? [allowlist.field] : [])];
  const [enabled, setEnabled] = useState(channel.enabled);
  const [fields, setFields] = useState<Record<string, string>>(stringFields(saved, skip));
  const [ids, setIds] = useState<string[]>(allowlist ? storedIds(saved[allowlist.field]) : []);
  const tokenChannel = TOKEN_CHANNELS[channel.channel_type];
  // Prefilled with the mask when a token is stored: sending it back unchanged keeps it.
  const [botToken, setBotToken] = useState(channel.has_bot_token ? SECRET_MASK : "");
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState("");
  const [justSaved, setJustSaved] = useState(freshlySaved);
  const added = channel.builtin === false;
  useEffect(() => {
    if (!freshlySaved) return;
    const timer = setTimeout(() => setJustSaved(false), 2000);
    return () => clearTimeout(timer);
  }, [freshlySaved]);
  const remove = useAction();
  const Icon = channelIcon(channel.channel_type);
  const isLark = channel.channel_type === "lark" || channel.channel_type === "lark_bot";
  // The Lark channel's saved settings: the bot, names, refused senders. Read again after a save.
  const [lark, setLark] = useState<LarkStatus | null>(null);
  const [larkError, setLarkError] = useState("");
  useEffect(() => {
    if (!isLark) return;
    let alive = true;
    fetchLarkStatus(channel.channel_type, channel.channel_id)
      .then((status) => alive && (setLark(status), setLarkError("")))
      .catch((e: Error) => alive && setLarkError(e.message));
    return () => {
      alive = false;
    };
  }, [isLark, channel.channel_type, channel.channel_id, channel.updated_at]);
  // An import's "secrets to fill in" links here (#settings/channels/<id>).
  const ref = useSettingsTarget<HTMLDivElement>("channel", channel.channel_id);

  const savedIds = allowlist ? storedIds(saved[allowlist.field]) : [];
  const savedToken = channel.has_bot_token ? SECRET_MASK : "";
  const dirty =
    enabled !== channel.enabled ||
    JSON.stringify(fields) !== JSON.stringify(stringFields(saved, skip)) ||
    JSON.stringify(ids) !== JSON.stringify(savedIds) ||
    (!!tokenChannel && botToken !== savedToken);
  const idProblems = allowlistProblems(ids, allowlist);
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
            {added && <p className="text-xs text-muted-foreground">Talks to {channel.manager_name ?? channel.manager_id}</p>}
            <p className="text-xs text-muted-foreground">{description}</p>
            {isLark && <LarkBotLine status={lark} error={larkError} channelType={channel.channel_type} />}
          </div>
        </div>
        <div className="flex items-center gap-3">
          <Badge variant={enabled ? "success" : "outline"}>
            {enabled ? "Enabled" : "Disabled"}
          </Badge>
          <Switch checked={enabled} onChange={setEnabled} label={`Enable ${channel.display_name}`} />
          {added && (
            <Button
              type="button"
              variant="ghost"
              className="min-h-11 text-destructive hover:text-destructive"
              disabled={remove.busy}
              onClick={async () => {
                if (!confirm(`Remove ${channel.display_name}? Its conversations stay stored; it keeps running until the server restarts.`)) return;
                if (await remove.run(() => deleteChannelBot(channel.channel_id))) onSaved();
              }}
            >
              <Trash2 className="h-3.5 w-3.5" aria-hidden="true" />
              Remove
            </Button>
          )}
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
              placeholder={channel.has_bot_token ? "" : channel.channel_type === "lark_bot" ? "App secret" : "123456:ABC-DEF…"}
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
          <AllowlistEditor
            allowlist={allowlist}
            ids={ids}
            setIds={setIds}
            problems={idProblems}
            names={isLark ? lark?.names : undefined}
          />
        )}

        {isLark && (
          <LarkPeople
            channelType={channel.channel_type}
            channelId={channel.channel_id}
            status={lark}
            ids={ids.map((value) => value.trim())}
            onAdd={(openId) => setIds((current) => (current.some((v) => v.trim() === openId) ? current : [...current, openId]))}
            full={ids.length >= MAX_SENDERS}
          />
        )}

        {allowlist && <ChannelChatBindings channelId={channel.channel_id} botManagerId={channel.manager_id} botManagerName={channel.manager_name ?? undefined} />}

        <div className="flex items-center justify-between gap-2 pt-2">
          <p className="text-[10px] text-muted-foreground">
            Channel ID: <code className="text-foreground">{channel.channel_id}</code>
            {" · "}Updated: {new Date(channel.updated_at).toLocaleString()}
            {" · "}Bot connection, credentials and allowlist changes take effect after the server restarts. Chat bindings and Only bound chats apply immediately.
          </p>
          <div className="flex items-center gap-2">
            {(error || remove.error) && <p role="alert" className="text-xs text-destructive">{error || remove.error}</p>}
            {justSaved && (
              <span className="flex items-center gap-1 text-xs text-success">
                <CheckCircle2 className="h-3.5 w-3.5" />
                Saved — restart to apply bot settings
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

/** The bot's name and app (with a profile for lark-cli), or why it is unavailable. */
function LarkBotLine({ status, error, channelType }: { status: LarkStatus | null; error: string; channelType: string }) {
  const isBotToken = channelType === "lark_bot";
  if (error) return <p className="mt-0.5 text-xs text-destructive">Bot: {error}</p>;
  if (!status) return <p className="mt-0.5 text-xs text-muted-foreground">{isBotToken ? "Bot: checking the Lark platform…" : "Bot: checking lark-cli…"}</p>;
  if (!status.bot) return <p className="mt-0.5 text-xs text-warning">{isBotToken ? `Bot: unknown${status.problem ? `, ${status.problem}` : ""}` : "Bot: unknown, lark-cli cannot act as it"}</p>;
  return (
    <p className="mt-0.5 flex items-center gap-1 text-xs text-foreground">
      <Bot className="h-3.5 w-3.5 text-muted-foreground" aria-hidden="true" />
      <span>
        Bot: <span className="font-medium">{status.bot.name}</span>{" "}
        <span className="text-muted-foreground">
          ({shortId(status.bot.app_id)}{status.bot.profile ? `, profile ${status.bot.profile}` : ""})
        </span>
      </span>
    </p>
  );
}

/** Add Lark senders by name or email, and let in the ones the allowlist refused lately. Adding changes the draft
 * allowlist above; Save stores it. */
function LarkPeople({
  channelType,
  channelId,
  status,
  ids,
  onAdd,
  full,
}: {
  channelType: string;
  channelId: string;
  status: LarkStatus | null;
  ids: string[];
  onAdd: (openId: string) => void;
  full: boolean;
}) {
  const id = useId();
  const [query, setQuery] = useState("");
  const [people, setPeople] = useState<LarkPerson[] | null>(null);
  const [complete, setComplete] = useState(true);
  const [searching, setSearching] = useState(false);
  const [error, setError] = useState("");
  const listed = (openId: string) => ids.includes(openId);

  const search = (event: React.FormEvent) => {
    event.preventDefault();
    if (!query.trim()) return;
    setSearching(true);
    setError("");
    searchLarkPeople(query.trim(), channelType, channelId)
      .then((result) => {
        setPeople(result.people);
        setComplete(result.complete);
      }, (e: Error) => setError(e.message))
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
        <p aria-live="polite" className="text-[10px] text-muted-foreground">
          {larkPeopleSearchNote(channelType, complete)}
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
                  {r.chat_id ? `, in group chat ${shortId(r.chat_id)}` : ""}
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

/** A gate target as the select holds it; "" = only the task page. */
const gateTargetKey = (target: GateTarget | null) => (target ? `${target.channel}\n${target.chat}` : "");

/** The chat the gate messages of tasks started on the dashboard also go to, from the channels' sender allowlists.
 * Read again when `reloadKey` changes (a channel was saved). */
function DashboardGatesCard({ reloadKey }: { reloadKey: string }) {
  const id = useId();
  const [settings, setSettings] = useState<GateSettings | null>(null);
  const [loadError, setLoadError] = useState("");
  const [choice, setChoice] = useState("");
  const [justSaved, setJustSaved] = useState(false);
  const { busy, error, run } = useAction();

  useEffect(() => {
    let alive = true;
    fetchGateSettings().then(
      (next) => {
        if (!alive) return;
        setSettings(next);
        setChoice(gateTargetKey(next.dashboard_target));
        setLoadError("");
      },
      (err) => alive && setLoadError(errorText(err)),
    );
    return () => {
      alive = false;
    };
  }, [reloadKey]);

  const saved = gateTargetKey(settings?.dashboard_target ?? null);
  const targets = settings?.targets ?? [];
  const chosen = targets.find((target) => gateTargetKey(target) === choice);
  // A saved chat the allowlist no longer has stays selectable, so the select shows what is saved.
  const gone = settings?.dashboard_target && !targets.some((t) => gateTargetKey(t) === saved) ? settings.dashboard_target : null;

  const save = async () => {
    setJustSaved(false);
    const ok = await run(async () => {
      // Save is off while the choice is the saved one, so anything but a listed chat is "only the task page".
      const next = await updateGateSettings(chosen ? { channel: chosen.channel, chat: chosen.chat } : null);
      setSettings(next);
      setChoice(gateTargetKey(next.dashboard_target));
    });
    if (ok) setJustSaved(true);
  };

  return (
    <section aria-labelledby={`${id}-title`} className="rounded-lg border border-border bg-card overflow-hidden">
      <div className="flex items-center gap-3 px-5 py-4 border-b border-border">
        <div className="flex h-10 w-10 shrink-0 items-center justify-center rounded-lg bg-muted text-muted-foreground">
          <MessageCircleQuestionMark className="h-5 w-5" aria-hidden="true" />
        </div>
        <div>
          <h3 id={`${id}-title`} className="text-sm font-semibold">Gates of dashboard tasks</h3>
          <p className="text-xs text-muted-foreground">
            Questions a task started on the dashboard asks you at a gate go to its task page, and also to the chat
            chosen here.
          </p>
        </div>
      </div>

      <div className="px-5 py-4 space-y-3">
        {!settings ? (
          loadError ? (
            <p role="alert" className="text-xs text-destructive">{loadError}</p>
          ) : (
            <Loader2 className="h-4 w-4 animate-spin text-muted-foreground motion-reduce:animate-none" aria-label="Loading" />
          )
        ) : targets.length === 0 && !gone ? (
          <p className="text-xs text-muted-foreground">
            Gate questions stay on the task page. To have them sent to a chat as well, add a person to the Lark or
            Telegram channel&apos;s sender allowlist above first.
          </p>
        ) : (
          <>
            <div className="space-y-1">
              <label htmlFor={`${id}-target`} className={LABEL}>Also send to</label>
              <select
                id={`${id}-target`}
                value={choice}
                onChange={(e) => {
                  setChoice(e.target.value);
                  setJustSaved(false);
                }}
                aria-describedby={`${id}-hint`}
                className={cn(SELECT, "h-11")}
              >
                <option value="">Only the task page</option>
                {targets.map((target) => (
                  // A chat on a channel that is off cannot be chosen (the API refuses it); a saved one stays shown.
                  <option
                    key={gateTargetKey(target)}
                    value={gateTargetKey(target)}
                    disabled={!target.channel_enabled && gateTargetKey(target) !== saved}
                  >
                    {`${target.label}${target.channel_enabled ? "" : " (channel off)"}`}
                  </option>
                ))}
                {gone && <option value={saved}>{`${gone.channel} · ${gone.chat} (not in the allowlist)`}</option>}
              </select>
              <p id={`${id}-hint`} className="text-xs text-muted-foreground">
                {chosen && !chosen.channel_enabled ? (
                  <span className="text-warning">This channel is turned off: nothing reaches the chat until it is on. </span>
                ) : gone && choice === saved ? (
                  <span className="text-warning">
                    {settings.dashboard_target_problem ?? "This chat is no longer in its channel's sender allowlist"}.{" "}
                  </span>
                ) : null}
                Chats a channel&apos;s sender allowlist lets in.
              </p>
            </div>
            <div className="flex flex-wrap items-center justify-end gap-2">
              {error && <p role="alert" className="text-xs text-destructive">{error}</p>}
              {justSaved && (
                <span role="status" className="flex items-center gap-1 text-xs text-success">
                  <CheckCircle2 className="h-3.5 w-3.5" aria-hidden="true" />
                  Saved
                </span>
              )}
              <Button type="button" onClick={save} disabled={busy || choice === saved} className="min-h-11">
                {busy ? (
                  <Loader2 className="h-3.5 w-3.5 animate-spin motion-reduce:animate-none" aria-hidden="true" />
                ) : (
                  <Save className="h-3.5 w-3.5" aria-hidden="true" />
                )}
                Save
              </Button>
            </div>
          </>
        )}
      </div>
    </section>
  );
}

const CHANNEL_GROUPS = [
  { type: "telegram", noun: "Telegram" },
  { type: "lark", noun: "Lark (lark-cli)" },
  { type: "lark_bot", noun: "Lark (bot token)" },
];

const TEXT_INPUT = "w-full rounded-md border border-input bg-background px-3 py-2 text-sm text-foreground placeholder:text-muted-foreground outline-none focus:ring-2 focus:ring-ring";

/** The form that adds a bot of one type for a Manager. The secret is cleared after a failed request, never shown again. */
function AddBotForm({
  type,
  noun,
  onCancel,
  onCreated,
}: {
  type: string;
  noun: string;
  onCancel: () => void;
  onCreated: (channelId: string) => void;
}) {
  const id = useId();
  const allowlist = SENDER_ALLOWLISTS[type];
  const tokenChannel = TOKEN_CHANNELS[type];
  const [managers, setManagers] = useState<ManagerInfo[]>([]);
  const [managersError, setManagersError] = useState("");
  const [managerId, setManagerId] = useState("");
  const [displayName, setDisplayName] = useState("");
  const [secret, setSecret] = useState("");
  const [fields, setFields] = useState<Record<string, string>>(
    type === "lark" ? { profile: "" } : type === "lark_bot" ? { app_id: "", domain: "feishu" } : {},
  );
  const [ids, setIds] = useState<string[]>([]);
  const [enabled, setEnabled] = useState(false);
  const { busy, error, run } = useAction();

  useEffect(() => {
    let alive = true;
    fetchManagers().then(
      (list) => {
        if (!alive) return;
        // The default Manager first, the rest in the API's order.
        const sorted = [...list.filter((m) => m.id === "manager"), ...list.filter((m) => m.id !== "manager")];
        setManagers(sorted);
        setManagerId(sorted[0]?.id ?? "");
      },
      (err) => alive && setManagersError(errorText(err)),
    );
    return () => {
      alive = false;
    };
  }, []);

  const manager = managers.find((m) => m.id === managerId);
  const problems = allowlistProblems(ids, allowlist);
  const missing =
    (tokenChannel && !secret) ||
    (type === "lark" && !fields.profile?.trim()) ||
    (type === "lark_bot" && !fields.app_id?.trim());
  const canSave = !busy && !!manager && !missing && !problems.some(Boolean);

  const save = async () => {
    const config: Record<string, unknown> = {};
    for (const [key, value] of Object.entries(fields)) config[key] = value.trim();
    if (allowlist) config[allowlist.field] = ids.map((value) => value.trim());
    const body: Parameters<typeof createChannelBot>[0] = { channel_type: type, manager_id: managerId, enabled, config };
    if (displayName.trim()) body.display_name = displayName.trim();
    if (tokenChannel) body.bot_token = secret;
    let created: ChannelConfig | undefined;
    const ok = await run(async () => {
      created = await createChannelBot(body);
    });
    if (ok && created) onCreated(created.channel_id);
    else setSecret("");
  };

  return (
    <div className="rounded-lg border border-border bg-card overflow-hidden">
      <div className="px-5 py-4 border-b border-border">
        <h3 className="text-sm font-semibold">Add a {noun} bot</h3>
      </div>
      <div className="px-5 py-4 space-y-4">
        {managersError && (
          <p role="alert" className="flex items-start gap-2 text-xs text-destructive">
            <AlertTriangle className="mt-0.5 h-3.5 w-3.5 shrink-0" aria-hidden="true" />
            {managersError}
          </p>
        )}
        <div className="grid gap-4 sm:grid-cols-2">
          <div className="space-y-1">
            <label htmlFor={`${id}-manager`} className={LABEL}>Manager</label>
            <select
              id={`${id}-manager`}
              value={managerId}
              onChange={(e) => setManagerId(e.target.value)}
              className={cn(SELECT, "h-11")}
            >
              {managers.map((m) => (
                <option key={m.id} value={m.id}>{`${m.name}${m.enabled ? "" : " (off)"}`}</option>
              ))}
            </select>
          </div>
          <div className="space-y-1">
            <label htmlFor={`${id}-name`} className={LABEL}>Display name (optional)</label>
            <Input
              id={`${id}-name`}
              value={displayName}
              onChange={(e) => setDisplayName(e.target.value)}
              placeholder={`${noun} · ${manager?.name ?? "Manager"}`}
              className="h-11"
            />
          </div>
        </div>

        {tokenChannel && (
          <div>
            <label htmlFor={`${id}-secret`} className="block text-xs font-medium text-muted-foreground mb-1.5">
              {tokenChannel.label}
            </label>
            <input
              id={`${id}-secret`}
              type="password"
              autoComplete="off"
              required
              value={secret}
              onChange={(e) => setSecret(e.target.value)}
              placeholder={type === "lark_bot" ? "App secret" : "123456:ABC-DEF…"}
              className={cn(TEXT_INPUT, "min-h-11")}
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
              required={key !== "domain"}
              value={fields[key]}
              onChange={(e) => setFields({ ...fields, [key]: e.target.value })}
              placeholder={FIELD_HELP[key]?.placeholder ?? ""}
              className={cn(TEXT_INPUT, "min-h-11")}
            />
            {FIELD_HELP[key] && <p className="mt-1 text-[10px] text-muted-foreground">{FIELD_HELP[key].hint}</p>}
          </div>
        ))}

        {allowlist && <AllowlistEditor allowlist={allowlist} ids={ids} setIds={setIds} problems={problems} />}

        <div className="flex items-center gap-3">
          <Switch checked={enabled} onChange={setEnabled} label={`Enable the new ${noun} bot`} />
          <span className="text-xs text-muted-foreground">Enabled (starts after the server restarts)</span>
        </div>

        <div className="flex flex-wrap items-center justify-end gap-2 pt-2">
          {error && (
            <p role="alert" className="flex items-center gap-1 text-xs text-destructive">
              <XCircle className="h-3.5 w-3.5 shrink-0" aria-hidden="true" />
              {error}
            </p>
          )}
          <Button type="button" variant="outline" className="min-h-11" onClick={onCancel}>Cancel</Button>
          <Button type="button" className="min-h-11" onClick={save} disabled={!canSave}>
            {busy ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <Save className="h-3.5 w-3.5" />}
            Save
          </Button>
        </div>
      </div>
    </div>
  );
}

/** Settings → Channels: Telegram and both Lark types (lark-cli and bot token), each with its sender allowlist. */
export function ChannelsSection() {
  const [channels, setChannels] = useState<ChannelConfig[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [adding, setAdding] = useState<string | null>(null);
  const [fresh, setFresh] = useState("");

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
        CHANNEL_GROUPS.map(({ type, noun }) => {
          const group = channels
            .filter((ch) => ch.channel_type === type)
            .sort((x, y) => Number(y.builtin !== false) - Number(x.builtin !== false) || x.channel_id.localeCompare(y.channel_id));
          return (
            <div key={type} className="space-y-4">
              {group.map((ch) => (
                <ChannelCard key={ch.channel_id} channel={ch} onSaved={load} freshlySaved={ch.channel_id === fresh} />
              ))}
              {adding === type ? (
                <AddBotForm
                  type={type}
                  noun={noun}
                  onCancel={() => setAdding(null)}
                  onCreated={(channelId) => {
                    setAdding(null);
                    setFresh(channelId);
                    load();
                  }}
                />
              ) : (
                <Button type="button" variant="outline" size="sm" className="min-h-11" onClick={() => setAdding(type)}>
                  <Plus className="h-3.5 w-3.5" aria-hidden="true" />
                  Add a {noun} bot
                </Button>
              )}
            </div>
          );
        })
      )}

      {!loading && <DashboardGatesCard reloadKey={channels.map((ch) => ch.updated_at).join()} />}
    </div>
  );
}
