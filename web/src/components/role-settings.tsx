import { useCallback, useEffect, useId, useRef, useState } from "react";
import { History, Loader2, Power, PowerOff, RotateCcw, Save } from "lucide-react";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Switch } from "@/components/ui/switch";
import { LayerSettingsForm } from "@/components/layer-settings";
import {
  fetchAgentConfigs,
  fetchAgentSettings,
  resetAgentPrompt,
  updateAgentConfig,
  updateRoleSettings,
  updateSystemSettings,
  type AgentConfig,
  type AgentRole,
  type AgentSettings,
} from "@/lib/api";
import { roleLabel } from "@/lib/runtime-catalog";
import { AGENT_PROFILE } from "@/lib/settings-pages";
import { cn } from "@/lib/utils";

const ROLES: readonly AgentRole[] = ["manager", "planner", "worker", "reviewer"];
const isRole = (agentId: string): agentId is AgentRole => (ROLES as readonly string[]).includes(agentId);

/** The role configs in the order a task meets them. */
function rank(agentId: string) {
  const at = (ROLES as readonly string[]).indexOf(agentId);
  return at < 0 ? ROLES.length : at;
}

// --- The role prompt ---

type PromptMode = "replace" | "append";

function RolePromptEditor({ id, value, onChange }: { id: string; value: string; onChange: (v: string) => void }) {
  const [mode, setMode] = useState<PromptMode>("replace");
  const [appendText, setAppendText] = useState("");
  const textareaRef = useRef<HTMLTextAreaElement>(null);

  const lineCount = value.split("\n").length;

  const handleApply = () => {
    if (mode === "append" && appendText.trim()) {
      onChange(value ? value + "\n\n" + appendText : appendText);
      setAppendText("");
    }
  };

  return (
    <div className="space-y-2">
      <div className="flex items-center justify-between">
        <label htmlFor={id} className="text-xs font-medium text-muted-foreground">Role prompt</label>
        <div className="flex items-center gap-2">
          <span className="text-[10px] text-muted-foreground">
            {value.length} chars · {lineCount} lines
          </span>
          <div className="flex rounded-md border border-border overflow-hidden">
            {(["replace", "append"] as PromptMode[]).map((m) => (
              <button
                key={m}
                type="button"
                onClick={() => setMode(m)}
                aria-pressed={mode === m}
                className={cn(
                  "px-2 py-0.5 text-[10px] font-medium transition-colors",
                  mode === m
                    ? "bg-primary text-primary-foreground"
                    : "text-muted-foreground hover:text-foreground",
                )}
              >
                {m === "replace" ? "Replace" : "Append"}
              </button>
            ))}
          </div>
        </div>
      </div>

      {mode === "replace" ? (
        <textarea
          id={id}
          ref={textareaRef}
          value={value}
          onChange={(e) => onChange(e.target.value)}
          rows={Math.max(12, Math.min(lineCount + 2, 40))}
          className="w-full resize-y rounded-md border border-input bg-transparent p-3 text-sm text-foreground font-mono leading-relaxed placeholder:text-muted-foreground focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring"
          placeholder="What this kind of agent is responsible for..."
        />
      ) : (
        <div className="space-y-2">
          <div className="rounded-md border border-border bg-muted/20 p-3 max-h-[300px] overflow-y-auto">
            <pre className="text-xs text-muted-foreground font-mono whitespace-pre-wrap">
              {value || "(empty)"}
            </pre>
          </div>
          <textarea
            id={id}
            value={appendText}
            onChange={(e) => setAppendText(e.target.value)}
            rows={6}
            className="w-full resize-y rounded-md border border-input bg-transparent p-3 text-sm text-foreground font-mono leading-relaxed placeholder:text-muted-foreground focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring"
            placeholder="Text to append..."
          />
          <Button
            size="sm"
            variant="outline"
            onClick={handleApply}
            disabled={!appendText.trim()}
            className="w-full"
          >
            Append to Prompt
          </Button>
        </div>
      )}
    </div>
  );
}

// --- A role's own fields (agent_configs) ---

function RoleConfigForm({
  config,
  onSave,
  onResetPrompt,
  saving,
}: {
  config: AgentConfig;
  onSave: (fields: Partial<AgentConfig>) => void;
  onResetPrompt: () => void;
  saving: boolean;
}) {
  const id = useId();
  const [form, setForm] = useState({
    display_name: config.display_name,
    max_turns: config.max_turns,
    timeout_seconds: config.timeout_seconds,
    max_concurrency: config.max_concurrency,
    enabled: config.enabled,
    system_prompt: config.system_prompt,
  });

  useEffect(() => {
    setForm({
      display_name: config.display_name,
      max_turns: config.max_turns,
      timeout_seconds: config.timeout_seconds,
      max_concurrency: config.max_concurrency,
      enabled: config.enabled,
      system_prompt: config.system_prompt,
    });
  }, [config]);

  return (
    <div className="space-y-5">
      <div className="space-y-1.5">
        <label htmlFor={`${id}-name`} className="text-xs font-medium text-muted-foreground">Display Name</label>
        <Input
          id={`${id}-name`}
          value={form.display_name}
          onChange={(e) => setForm({ ...form, display_name: e.target.value })}
        />
      </div>

      <div className="grid gap-4 sm:grid-cols-3">
        <div className="space-y-1.5">
          <label htmlFor={`${id}-turns`} className="text-xs font-medium text-muted-foreground">Max Turns</label>
          <Input
            id={`${id}-turns`}
            type="number"
            min={0}
            value={form.max_turns}
            onChange={(e) => setForm({ ...form, max_turns: Number(e.target.value) })}
          />
          <p className="text-[11px] text-muted-foreground">0 = unlimited</p>
        </div>
        <div className="space-y-1.5">
          <label htmlFor={`${id}-timeout`} className="text-xs font-medium text-muted-foreground">Timeout (s)</label>
          <Input
            id={`${id}-timeout`}
            type="number"
            min={0}
            value={form.timeout_seconds}
            onChange={(e) => setForm({ ...form, timeout_seconds: Number(e.target.value) })}
          />
          <p className="text-[11px] text-muted-foreground">0 = unlimited</p>
        </div>
        <div className="space-y-1.5">
          <label htmlFor={`${id}-concurrency`} className="text-xs font-medium text-muted-foreground">Concurrency</label>
          <Input
            id={`${id}-concurrency`}
            type="number"
            min={1}
            value={form.max_concurrency}
            onChange={(e) => setForm({ ...form, max_concurrency: Number(e.target.value) })}
          />
        </div>
      </div>

      <div className="space-y-1">
        <span className="text-xs font-medium text-muted-foreground">Permissions</span>
        <p className="text-sm">
          Every tool, no permission prompts{" "}
          <code className="rounded bg-muted px-1 py-0.5 font-mono text-xs">--dangerously-skip-permissions</code>
        </p>
        <p className="text-[11px] text-muted-foreground">
          The same for every Looper agent; the role prompt says what this agent is for. Lark and Telegram pass on
          only the senders listed in Settings → Channels.
        </p>
      </div>

      <div className="flex items-center justify-between rounded-md border border-border px-4 py-3">
        <div className="flex items-center gap-2">
          {form.enabled ? (
            <Power className="h-4 w-4 text-success" aria-hidden="true" />
          ) : (
            <PowerOff className="h-4 w-4 text-muted-foreground" aria-hidden="true" />
          )}
          <span className="text-sm">{form.enabled ? "Enabled" : "Disabled"}</span>
        </div>
        <Switch
          checked={form.enabled}
          onChange={(enabled) => setForm({ ...form, enabled })}
          label={`Enable ${config.display_name || config.agent_id}`}
        />
      </div>

      <RolePromptEditor
        id={`${id}-prompt`}
        value={form.system_prompt}
        onChange={(v) => setForm({ ...form, system_prompt: v })}
      />

      {config.default_prompt && config.system_prompt !== config.default_prompt && (
        <div className="flex items-center justify-between rounded-md border border-border bg-muted/20 px-3 py-2">
          <span className="text-[11px] text-muted-foreground">
            This prompt differs from the system-recommended one.
          </span>
          <Button variant="outline" size="sm" onClick={onResetPrompt} disabled={saving}>
            <RotateCcw className="h-3.5 w-3.5" />
            Reset to recommended
          </Button>
        </div>
      )}

      <Button onClick={() => onSave(form)} disabled={saving} className="w-full">
        {saving ? <Loader2 className="h-4 w-4 animate-spin" /> : <Save className="h-4 w-4" />}
        Save Config
      </Button>
    </div>
  );
}

// --- The page ---

/**
 * Settings → Roles: the defaults every role inherits (the system layer), then each role: its layer (Agent
 * Profile, harness, effort, environment, per-run cap) and its own prompt and limits. `sub` names the role shown
 * (#settings/roles/<role>), which a link scrolls into view.
 */
export function RolesSection({ sub, onNavigate }: { sub: string; onNavigate: (sub: string) => void }) {
  const [configs, setConfigs] = useState<AgentConfig[]>([]);
  const [settings, setSettings] = useState<AgentSettings | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [saving, setSaving] = useState(false);
  const [toast, setToast] = useState("");
  const roleRef = useRef<HTMLElement>(null);

  useEffect(() => {
    fetchAgentConfigs()
      .then((rows) => {
        setConfigs([...rows].sort((a, b) => rank(a.agent_id) - rank(b.agent_id)));
        setError("");
      })
      .catch((e) => setError(e.message))
      .finally(() => setLoading(false));
    fetchAgentSettings().then(setSettings).catch((e) => setError(e.message));
  }, []);

  const selectedId = sub.split("/")[0] || configs[0]?.agent_id || "";
  const selected = configs.find((c) => c.agent_id === selectedId) ?? null;

  // A link to a role (from Runs or an Agent Profile's "used by") shows its section.
  const linked = !!sub;
  useEffect(() => {
    if (!linked || loading) return;
    const reduce = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
    roleRef.current?.scrollIntoView({ block: "start", behavior: reduce ? "auto" : "smooth" });
  }, [linked, loading, selectedId]);

  const flash = (text: string) => {
    setToast(text);
    setTimeout(() => setToast(""), text.startsWith("Error") ? 3000 : 2000);
  };
  const run = useCallback(async (action: () => Promise<AgentConfig>, done: string) => {
    setSaving(true);
    try {
      const updated = await action();
      setConfigs((prev) => prev.map((c) => (c.agent_id === updated.agent_id ? updated : c)));
      flash(done);
    } catch (e) {
      flash(`Error: ${(e as Error).message}`);
    } finally {
      setSaving(false);
    }
  }, []);

  if (loading) {
    return (
      <div className="flex h-64 items-center justify-center">
        <Loader2 className="h-6 w-6 animate-spin text-muted-foreground" aria-label="Loading" />
      </div>
    );
  }

  return (
    <div className="space-y-6">
      {error && (
        <div role="alert" className="rounded-lg border border-destructive/50 bg-destructive/10 px-5 py-4 text-sm text-destructive">
          {error}
        </div>
      )}

      {settings && (
        <section aria-labelledby="roles-defaults" className="space-y-3">
          <div>
            <h2 id="roles-defaults" className="text-sm font-semibold">Every role</h2>
            <p className="mt-1 text-xs text-muted-foreground">
              What every role inherits. A role below sets its own, and a project&apos;s Settings tab sets them for its
              agents and for each role in it; the most specific one wins.
            </p>
          </div>
          <div className="rounded-lg border border-border bg-card px-5 py-4">
            <LayerSettingsForm
              idPrefix="roles-system"
              layer="system"
              roles={[...ROLES]}
              values={settings.system.values}
              effective={settings.system.effective}
              limits={settings.limits}
              onSave={async (changes) => {
                const next = await updateSystemSettings(changes);
                setSettings(next);
                return next.system.values;
              }}
              harnessHint={
                <>
                  Every agent&apos;s working principles, boundaries and tone, placed before its role prompt. Empty: the
                  built-in harness.
                </>
              }
            />
          </div>
        </section>
      )}

      <section ref={roleRef} aria-labelledby="roles-role" className="scroll-mt-4 space-y-3">
        <div className="flex flex-wrap items-center justify-between gap-2">
          <h2 id="roles-role" className="text-sm font-semibold">Each role</h2>
          <div role="group" aria-label="Role" className="flex flex-wrap rounded-md border border-border p-0.5">
            {configs.map((c) => (
              <button
                key={c.agent_id}
                type="button"
                aria-pressed={c.agent_id === selectedId}
                onClick={() => onNavigate(c.agent_id)}
                className={cn(
                  "rounded px-3 py-1.5 text-xs font-medium transition-colors focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring",
                  c.agent_id === selectedId ? "bg-primary/10 text-primary" : "text-muted-foreground hover:text-foreground",
                )}
              >
                {isRole(c.agent_id) ? roleLabel(c.agent_id) : c.display_name || c.agent_id}
              </button>
            ))}
          </div>
        </div>

        {toast && (
          <div
            role="status"
            className={cn(
              "rounded-lg border px-4 py-2.5 text-sm",
              toast.startsWith("Error")
                ? "border-destructive/50 bg-destructive/10 text-destructive"
                : "border-success/50 bg-success/10 text-success",
            )}
          >
            {toast}
          </div>
        )}

        {!error && configs.length === 0 && (
          <p className="text-sm text-muted-foreground">
            No roles configured. Start the server with a database connection to seed defaults.
          </p>
        )}

        {selected && (
          <div className="space-y-5 rounded-lg border border-border bg-card px-5 py-4">
            <div className="flex flex-wrap items-center gap-2">
              <h3 className="text-sm font-semibold">{selected.display_name || selected.agent_id}</h3>
              <Badge variant="outline">{selected.role || "—"}</Badge>
              <a
                href={`#runs/${encodeURIComponent(selected.agent_id)}`}
                className="ml-auto flex items-center gap-1 text-xs text-primary underline-offset-2 hover:underline"
              >
                <History className="h-3.5 w-3.5" aria-hidden="true" />
                Its runs
              </a>
            </div>

            {settings && isRole(selected.agent_id) && (
              <section aria-labelledby={`role-layer-${selected.agent_id}`} className="space-y-3">
                <div>
                  <h4 id={`role-layer-${selected.agent_id}`} className="text-xs font-semibold">
                    {AGENT_PROFILE.name}, harness and environment
                  </h4>
                  <p className="mt-1 text-xs text-muted-foreground">
                    {selected.agent_id === "manager"
                      ? "The Manager's own settings over every role's. Empty fields inherit."
                      : "This role's settings in every project, over every role's. A project's Settings tab can " +
                        "override them for all its agents or for this role. Empty fields inherit."}
                  </p>
                </div>
                <LayerSettingsForm
                  key={selected.agent_id}
                  idPrefix={`role-${selected.agent_id}`}
                  layer="role"
                  roles={[selected.agent_id]}
                  values={settings.roles[selected.agent_id].values}
                  effective={settings.roles[selected.agent_id].effective}
                  limits={settings.limits}
                  onSave={async (changes) => {
                    const role = selected.agent_id as AgentRole;
                    const next = await updateRoleSettings(role, changes);
                    setSettings(next);
                    return next.roles[role].values;
                  }}
                  harnessHint={
                    selected.agent_id === "reviewer"
                      ? "Empty: the Worker's harness at this layer, else the default."
                      : "Working principles, boundaries and tone, placed before the role prompt."
                  }
                />
              </section>
            )}

            <section aria-labelledby={`role-config-${selected.agent_id}`} className="space-y-3 border-t border-border pt-5">
              <h4 id={`role-config-${selected.agent_id}`} className="text-xs font-semibold">Role prompt and limits</h4>
              <RoleConfigForm
                key={selected.agent_id}
                config={selected}
                saving={saving}
                onSave={(fields) => run(() => updateAgentConfig(selected.agent_id, fields), "Saved")}
                onResetPrompt={() => run(() => resetAgentPrompt(selected.agent_id), "Reset to recommended prompt")}
              />
            </section>
          </div>
        )}
      </section>
    </div>
  );
}
