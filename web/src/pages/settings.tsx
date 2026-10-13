import { useEffect, useId, useState } from "react";
import { Save, Loader2, MessageCircle, Send, CheckCircle2, XCircle, Plus, RotateCcw, Trash2 } from "lucide-react";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { HarnessField, harnessTooLong } from "@/components/harness-field";
import { LibraryCard } from "@/components/library-card";
import {
  fetchAgentSettings,
  fetchChannelConfigs,
  resetDefaultHarness,
  updateAgentSettings,
  updateChannelConfig,
  type AgentSettings,
  type AgentSettingsUpdate,
  type ChannelConfig,
  type EnvEntry,
} from "@/lib/api";
import { cn } from "@/lib/utils";

const ENV_NAME = /^[A-Z_][A-Z0-9_]*$/;
const CREDENTIAL_WORDS = ["KEY", "TOKEN", "SECRET", "PASSWORD", "CREDENTIAL"];

/** Why an environment variable name would be refused, or "" when it is fine (the server checks again). */
function envNameProblem(key: string) {
  if (!ENV_NAME.test(key)) return "Use capitals, digits and _ (not starting with a digit).";
  if (CREDENTIAL_WORDS.some((word) => key.includes(word))) return "Looks like a credential: secrets do not belong here.";
  return "";
}

/** The defaults every agent inherits: harness prompt, model, effort and environment variables. */
function AgentDefaultsCard() {
  const id = useId();
  const [settings, setSettings] = useState<AgentSettings | null>(null);
  const [harness, setHarness] = useState("");
  const [model, setModel] = useState("");
  const [effort, setEffort] = useState("");
  const [env, setEnv] = useState<EnvEntry[]>([]);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [saved, setSaved] = useState(false);

  const show = (next: AgentSettings) => {
    setSettings(next);
    setHarness(next.default_harness_prompt);
    setModel(next.model_source === "system_default" ? next.default_model : "");
    setEffort(next.default_effort);
    setEnv(next.default_env);
  };

  useEffect(() => {
    fetchAgentSettings().then(show).catch((e) => setError(e.message));
  }, []);

  if (!settings) {
    return error ? (
      <div className="rounded-lg border border-destructive/50 bg-destructive/10 px-5 py-4 text-sm text-destructive">{error}</div>
    ) : (
      <div className="flex h-32 items-center justify-center">
        <Loader2 className="h-6 w-6 animate-spin text-muted-foreground" />
      </div>
    );
  }

  const savedModel = settings.model_source === "system_default" ? settings.default_model : "";
  const envChanged = JSON.stringify(env) !== JSON.stringify(settings.default_env);
  const changes: AgentSettingsUpdate = {};
  if (harness !== settings.default_harness_prompt) changes.default_harness_prompt = harness;
  if (model.trim() !== savedModel) changes.default_model = model.trim();
  if (effort !== settings.default_effort) changes.default_effort = effort;
  if (envChanged) changes.default_env = env;
  const dirty = Object.keys(changes).length > 0;
  const envProblems = env.map((entry) => (entry.key ? envNameProblem(entry.key) : "A name is required."));
  const invalid = harnessTooLong(harness) || envProblems.some(Boolean);
  const customized = settings.harness_source === "system_default";

  const run = async (request: () => Promise<AgentSettings>) => {
    setBusy(true);
    setError("");
    try {
      show(await request());
      setSaved(true);
      setTimeout(() => setSaved(false), 2000);
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  };

  const setEntry = (index: number, entry: Partial<EnvEntry>) =>
    setEnv(env.map((current, i) => (i === index ? { ...current, ...entry } : current)));

  return (
    <div className="rounded-lg border border-border bg-card">
      <div className="space-y-5 px-5 py-4">
        <div className="space-y-2">
          <HarnessField
            id={`${id}-harness`}
            label="Default harness prompt"
            value={harness}
            onChange={setHarness}
            rows={12}
            hint={
              <>
                Every agent&apos;s working principles, boundaries and tone, placed before its role prompt. The Manager
                (Agents page) and each project&apos;s Planner and Workers (project editor) can override it; Reviewers
                follow their project&apos;s Workers. Changes apply to each agent&apos;s next run.
              </>
            }
          />
          <div className="flex items-center justify-between gap-2">
            <Badge variant={customized ? "default" : "outline"}>{customized ? "Customized" : "Built-in"}</Badge>
            <Button
              type="button"
              variant="outline"
              size="sm"
              onClick={() => run(resetDefaultHarness)}
              disabled={busy || !customized}
            >
              <RotateCcw className="h-3.5 w-3.5" />
              Reset to built-in
            </Button>
          </div>
        </div>

        <div className="grid gap-4 sm:grid-cols-2">
          <div className="space-y-1">
            <label htmlFor={`${id}-model`} className="text-xs font-medium text-muted-foreground">Default model</label>
            <Input
              id={`${id}-model`}
              value={model}
              onChange={(e) => setModel(e.target.value)}
              placeholder={`${settings.builtin_model} (built-in)`}
              className="font-mono"
            />
            <p className="text-xs text-muted-foreground">
              An alias (opus, sonnet) or a full model name. An agent&apos;s own model on the Agents page wins.
            </p>
          </div>
          <div className="space-y-1">
            <label htmlFor={`${id}-effort`} className="text-xs font-medium text-muted-foreground">Effort</label>
            <select
              id={`${id}-effort`}
              value={effort}
              onChange={(e) => setEffort(e.target.value)}
              className="flex h-9 w-full rounded-md border border-input bg-transparent px-3 py-1 text-sm text-foreground"
            >
              <option value="">Claude Code default</option>
              {settings.effort_levels.map((level) => (
                <option key={level} value={level}>{level}</option>
              ))}
            </select>
          </div>
        </div>

        <div className="space-y-2">
          <div className="flex items-baseline justify-between gap-2">
            <span className="text-xs font-medium text-muted-foreground">Environment variables</span>
            <span className="text-[11px] tabular-nums text-muted-foreground">
              {env.length} / {settings.env_max_entries}
            </span>
          </div>
          {env.map((entry, index) => (
            <div key={index} className="space-y-1">
              <div className="flex items-center gap-2">
                <Input
                  aria-label={`Variable ${index + 1} name`}
                  value={entry.key}
                  onChange={(e) => setEntry(index, { key: e.target.value.toUpperCase() })}
                  placeholder="NAME"
                  aria-invalid={!!envProblems[index]}
                  className="w-2/5 font-mono"
                />
                <Input
                  aria-label={`Variable ${index + 1} value`}
                  value={entry.value}
                  onChange={(e) => setEntry(index, { value: e.target.value })}
                  placeholder="value"
                  className="flex-1 font-mono"
                />
                <Button
                  type="button"
                  variant="ghost"
                  size="sm"
                  onClick={() => setEnv(env.filter((_, i) => i !== index))}
                  aria-label={`Remove ${entry.key || `variable ${index + 1}`}`}
                >
                  <Trash2 className="h-3.5 w-3.5" />
                </Button>
              </div>
              {envProblems[index] && <p className="text-xs text-destructive">{envProblems[index]}</p>}
            </div>
          ))}
          <Button
            type="button"
            variant="outline"
            size="sm"
            onClick={() => setEnv([...env, { key: "", value: "" }])}
            disabled={env.length >= settings.env_max_entries}
          >
            <Plus className="h-3.5 w-3.5" />
            Add variable
          </Button>
          <p className="text-xs text-muted-foreground">
            Set for every agent&apos;s Claude Code, on top of the server&apos;s environment. Not for secrets: values are
            shown here, and run records keep the names with a hash of each value.
          </p>
        </div>
      </div>

      <div className="flex items-center justify-end gap-2 border-t border-border px-5 py-3">
        {error && <p role="alert" className="mr-auto text-xs text-destructive">{error}</p>}
        {saved && (
          <span className="flex items-center gap-1 text-xs text-success">
            <CheckCircle2 className="h-3.5 w-3.5" />
            Saved
          </span>
        )}
        <Button type="button" size="sm" onClick={() => run(() => updateAgentSettings(changes))} disabled={busy || !dirty || invalid}>
          {busy ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <Save className="h-3.5 w-3.5" />}
          Save
        </Button>
      </div>
    </div>
  );
}

function channelIcon(type: string) {
  if (type === "telegram") return Send;
  return MessageCircle;
}

function ChannelCard({ channel, onSaved }: { channel: ChannelConfig; onSaved: () => void }) {
  const [enabled, setEnabled] = useState(channel.enabled);
  const [config, setConfig] = useState<Record<string, string>>(channel.config || {});
  const [saving, setSaving] = useState(false);
  const [saved, setSaved] = useState(false);
  const Icon = channelIcon(channel.channel_type);

  const dirty =
    enabled !== channel.enabled ||
    JSON.stringify(config) !== JSON.stringify(channel.config || {});

  const handleSave = async () => {
    setSaving(true);
    try {
      await updateChannelConfig(channel.channel_id, { enabled, config });
      setSaved(true);
      setTimeout(() => setSaved(false), 2000);
      onSaved();
    } catch {
      // ignore
    } finally {
      setSaving(false);
    }
  };

  const configKeys = Object.keys(config).filter((k) => k !== "description");
  const description = config.description || "";

  return (
    <div className={cn(
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
          <button
            type="button"
            onClick={() => setEnabled(!enabled)}
            className={cn(
              "relative inline-flex h-6 w-11 shrink-0 cursor-pointer rounded-full border-2 border-transparent transition-colors",
              enabled ? "bg-primary" : "bg-muted",
            )}
          >
            <span
              className={cn(
                "pointer-events-none inline-block h-5 w-5 rounded-full bg-background shadow-lg transition-transform",
                enabled ? "translate-x-5" : "translate-x-0",
              )}
            />
          </button>
        </div>
      </div>

      <div className="px-5 py-4 space-y-4">
        {configKeys.map((key) => (
          <div key={key}>
            <label className="block text-xs font-medium text-muted-foreground mb-1.5">
              {key.replace(/_/g, " ").replace(/\b\w/g, (c) => c.toUpperCase())}
            </label>
            <input
              type="text"
              value={config[key] || ""}
              onChange={(e) => setConfig({ ...config, [key]: e.target.value })}
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

        <div className="flex items-center justify-between pt-2">
          <p className="text-[10px] text-muted-foreground">
            Channel ID: <code className="text-foreground">{channel.channel_id}</code>
            {" · "}Updated: {new Date(channel.updated_at).toLocaleString()}
          </p>
          <div className="flex items-center gap-2">
            {saved && (
              <span className="flex items-center gap-1 text-xs text-success">
                <CheckCircle2 className="h-3.5 w-3.5" />
                Saved
              </span>
            )}
            <button
              type="button"
              onClick={handleSave}
              disabled={saving || !dirty}
              className={cn(
                "flex items-center gap-1.5 rounded-md px-3 py-1.5 text-sm font-medium transition-colors",
                dirty
                  ? "bg-primary text-primary-foreground hover:bg-primary/90"
                  : "bg-muted text-muted-foreground cursor-not-allowed",
              )}
            >
              {saving ? (
                <Loader2 className="h-3.5 w-3.5 animate-spin" />
              ) : (
                <Save className="h-3.5 w-3.5" />
              )}
              Save
            </button>
          </div>
        </div>
      </div>
    </div>
  );
}

export function SettingsPage() {
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
    <div className="space-y-6 p-6">
      <div>
        <h1 className="text-2xl font-bold">Settings</h1>
        <p className="text-sm text-muted-foreground mt-1">
          Agent defaults, plugins and skills, channel integrations and system configuration
        </p>
      </div>

      <div>
        <h2 className="text-sm font-semibold mb-3">Agent Defaults</h2>
        <AgentDefaultsCard />
      </div>

      <div>
        <h2 className="text-sm font-semibold mb-3">Plugins &amp; skills</h2>
        <LibraryCard />
      </div>

      <div>
        <h2 className="text-sm font-semibold mb-3">Channel Integrations</h2>
        <p className="text-xs text-muted-foreground mb-4">
          Connect external messaging platforms. Channels read credentials from environment variables at server startup — restart the server after enabling a channel.
        </p>

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
          <div className="space-y-4">
            {channels.map((ch) => (
              <ChannelCard key={ch.channel_id} channel={ch} onSaved={load} />
            ))}
          </div>
        )}
      </div>
    </div>
  );
}
