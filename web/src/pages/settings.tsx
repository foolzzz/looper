import { useEffect, useState } from "react";
import { Save, Loader2, MessageCircle, Send, CheckCircle2, XCircle } from "lucide-react";
import { Badge } from "@/components/ui/badge";
import { fetchChannelConfigs, updateChannelConfig, type ChannelConfig } from "@/lib/api";
import { cn } from "@/lib/utils";

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
          Channel integrations and system configuration
        </p>
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
