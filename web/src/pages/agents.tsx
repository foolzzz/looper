import { useEffect, useState, useCallback, useRef } from "react";
import {
  Bot,
  Loader2,
  RefreshCw,
  RotateCcw,
  Save,
  Power,
  PowerOff,
  Clock,
  Cpu,
  Zap,
} from "lucide-react";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { cn } from "@/lib/utils";
import { LayerSettingsForm } from "@/components/layer-settings";
import {
  fetchAgentConfigs,
  fetchAgentSettings,
  updateRoleSettings,
  updateAgentConfig,
  resetAgentPrompt,
  fetchLlmRecords,
  type AgentConfig,
  type AgentRole,
  type AgentSettings,
  type LlmRecord,
} from "@/lib/api";
import { historyHref } from "@/pages/agent-history";

type Tab = "config" | "sessions";

function roleBadge(role: string) {
  switch (role) {
    case "manager":
      return <Badge variant="success">{role}</Badge>;
    case "planner":
      return <Badge variant="default">{role}</Badge>;
    case "worker":
    case "reviewer":
      return <Badge variant="outline">{role}</Badge>;
    default:
      return <Badge variant="outline">{role || "—"}</Badge>;
  }
}

function formatTime(iso: string | null) {
  if (!iso) return "—";
  try {
    return new Date(iso).toLocaleString();
  } catch {
    return iso;
  }
}

// --- Agent Card ---

/** The agents in the order a task meets them. */
const AGENT_ORDER = ["manager", "planner", "worker", "reviewer"];

function agentRank(agentId: string) {
  const at = AGENT_ORDER.indexOf(agentId);
  return at < 0 ? AGENT_ORDER.length : at;
}

function AgentCard({
  config,
  selected,
  onSelect,
  agent,
}: {
  config: AgentConfig;
  selected: boolean;
  onSelect: () => void;
  /** The registered agent this role runs with outside any project; null while it loads. */
  agent: string | null;
}) {
  return (
    <button
      type="button"
      onClick={onSelect}
      className={cn(
        "w-full text-left rounded-lg border p-4 transition-colors",
        selected
          ? "border-primary bg-primary/5"
          : "border-border bg-card hover:bg-muted/50",
      )}
    >
      <div className="flex items-start gap-3">
        <div
          className={cn(
            "flex h-10 w-10 shrink-0 items-center justify-center rounded-lg",
            config.enabled ? "bg-primary/10 text-primary" : "bg-muted text-muted-foreground",
          )}
        >
          <Bot className="h-5 w-5" />
        </div>
        <div className="min-w-0 flex-1">
          <div className="flex items-center gap-2">
            <span className="text-sm font-semibold truncate">
              {config.display_name || config.agent_id}
            </span>
            {!config.enabled && <PowerOff className="h-3.5 w-3.5 text-muted-foreground" />}
          </div>
          <div className="mt-1.5 flex flex-wrap items-center gap-1.5">
            {roleBadge(config.role)}
          </div>
          <div className="mt-2 flex items-center gap-3 text-xs text-muted-foreground">
            {agent && (
              <span className="flex min-w-0 items-center gap-1">
                <Cpu className="h-3 w-3 shrink-0" />
                <span className="truncate">{agent}</span>
              </span>
            )}
            <span className="flex items-center gap-1">
              <Zap className="h-3 w-3" />
              x{config.max_concurrency}
            </span>
            {config.timeout_seconds > 0 && (
              <span className="flex items-center gap-1">
                <Clock className="h-3 w-3" />
                {config.timeout_seconds}s
              </span>
            )}
          </div>
        </div>
      </div>
    </button>
  );
}

// --- System Prompt Editor ---

type PromptMode = "replace" | "append";

function SystemPromptEditor({
  value,
  onChange,
}: {
  value: string;
  onChange: (v: string) => void;
}) {
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
        <label className="text-xs font-medium text-muted-foreground">Role prompt</label>
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

// --- Config Tab ---

const ROLES: readonly AgentRole[] = ["manager", "planner", "worker", "reviewer"];
const isRole = (agentId: string): agentId is AgentRole => (ROLES as readonly string[]).includes(agentId);

/** A role's layer of the agent settings: its harness, agent, effort, environment and per-run cap. */
function RoleSettingsSection({
  role,
  settings,
  onSaved,
}: {
  role: AgentRole;
  settings: AgentSettings;
  onSaved: (settings: AgentSettings) => void;
}) {
  const state = settings.roles[role];
  return (
    <section aria-labelledby={`role-settings-${role}`} className="space-y-3 border-t border-border pt-5">
      <div>
        <h2 id={`role-settings-${role}`} className="text-sm font-semibold">Harness, agent and environment</h2>
        <p className="mt-1 text-xs text-muted-foreground">
          {role === "manager"
            ? "The Manager's own settings over the defaults in Settings. Empty fields inherit."
            : "This role's settings in every project, over the defaults in Settings. A project's Settings tab can " +
              "override them for all its agents or for this role. Empty fields inherit."}
        </p>
      </div>
      <LayerSettingsForm
        key={role}
        idPrefix={`role-${role}`}
        layer="role"
        values={state.values}
        effective={state.effective}
        limits={settings.limits}
        onSave={async (changes) => {
          const next = await updateRoleSettings(role, changes);
          onSaved(next);
          return next.roles[role].values;
        }}
        harnessHint={
          role === "reviewer"
            ? "Empty: the Worker's harness at this layer, else the default."
            : "Working principles, boundaries and tone, placed before the role prompt."
        }
      />
    </section>
  );
}

function ConfigTab({
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

  const handleSave = () => {
    onSave({
      display_name: form.display_name,
      max_turns: form.max_turns,
      timeout_seconds: form.timeout_seconds,
      max_concurrency: form.max_concurrency,
      enabled: form.enabled,
      system_prompt: form.system_prompt,
    } as Partial<AgentConfig>);
  };

  return (
    <div className="space-y-5">
      <div className="space-y-1.5">
        <label className="text-xs font-medium text-muted-foreground">Display Name</label>
        <Input
          value={form.display_name}
          onChange={(e) => setForm({ ...form, display_name: e.target.value })}
        />
      </div>

      <div className="grid grid-cols-3 gap-4">
        <div className="space-y-1.5">
          <label className="text-xs font-medium text-muted-foreground">Max Turns</label>
          <Input
            type="number"
            min={0}
            value={form.max_turns}
            onChange={(e) => setForm({ ...form, max_turns: Number(e.target.value) })}
          />
          <p className="text-[11px] text-muted-foreground">0 = unlimited</p>
        </div>
        <div className="space-y-1.5">
          <label className="text-xs font-medium text-muted-foreground">Timeout (s)</label>
          <Input
            type="number"
            min={0}
            value={form.timeout_seconds}
            onChange={(e) => setForm({ ...form, timeout_seconds: Number(e.target.value) })}
          />
          <p className="text-[11px] text-muted-foreground">0 = unlimited</p>
        </div>
        <div className="space-y-1.5">
          <label className="text-xs font-medium text-muted-foreground">Concurrency</label>
          <Input
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
          The same for every LoopX agent; the role prompt says what this agent is for. Lark and Telegram pass on
          only the senders listed in Settings.
        </p>
      </div>

      <div className="flex items-center justify-between rounded-md border border-border px-4 py-3">
        <div className="flex items-center gap-2">
          {form.enabled ? (
            <Power className="h-4 w-4 text-success" />
          ) : (
            <PowerOff className="h-4 w-4 text-muted-foreground" />
          )}
          <span className="text-sm">{form.enabled ? "Enabled" : "Disabled"}</span>
        </div>
        <button
          type="button"
          onClick={() => setForm({ ...form, enabled: !form.enabled })}
          className={cn(
            "relative h-5 w-9 rounded-full transition-colors",
            form.enabled ? "bg-primary" : "bg-muted-foreground/30",
          )}
        >
          <span
            className={cn(
              "absolute top-0.5 h-4 w-4 rounded-full bg-white transition-transform",
              form.enabled ? "translate-x-4" : "translate-x-0.5",
            )}
          />
        </button>
      </div>

      <SystemPromptEditor
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

      <Button onClick={handleSave} disabled={saving} className="w-full">
        {saving ? <Loader2 className="h-4 w-4 animate-spin" /> : <Save className="h-4 w-4" />}
        Save Config
      </Button>
    </div>
  );
}

// --- Runs Tab ---

function RunsTab({ agentId }: { agentId: string }) {
  const [runs, setRuns] = useState<LlmRecord[] | null>(null);

  useEffect(() => {
    setRuns(null);
    fetchLlmRecords({ role: agentId, limit: 100 })
      .then(setRuns)
      .catch(() => setRuns([]));
  }, [agentId]);

  if (runs === null) {
    return (
      <div className="flex h-40 items-center justify-center">
        <Loader2 className="h-5 w-5 animate-spin text-muted-foreground" />
      </div>
    );
  }
  if (!runs.length) {
    return (
      <div className="flex h-40 items-center justify-center">
        <p className="text-sm text-muted-foreground">No runs yet</p>
      </div>
    );
  }
  return (
    <div className="space-y-2">
      {runs.map((run) => (
        <a
          key={run.id}
          href={historyHref(run.agent_id, run.task_id)}
          className="block rounded-lg border border-border bg-card px-4 py-3 hover:bg-accent/50 transition-colors"
        >
          <div className="flex flex-wrap items-center gap-x-3 gap-y-1 text-xs text-muted-foreground">
            <span className="font-mono tabular-nums">{formatTime(run.created_at)}</span>
            <Badge variant={run.status === "completed" ? "success" : run.status === "failed" ? "destructive" : "warning"}>
              {run.status}
            </Badge>
            <span className="font-mono text-foreground">{run.agent_id}</span>
            {run.task_id && <span>{run.task_id}</span>}
            {run.duration_ms != null && <span>{Math.round(run.duration_ms / 1000)}s</span>}
            {run.cost_usd != null && <span>${run.cost_usd.toFixed(4)}</span>}
          </div>
          {run.input_summary && <p className="mt-1 truncate text-sm">{run.input_summary}</p>}
          {run.error && <p className="mt-1 truncate text-xs text-destructive">{run.error}</p>}
        </a>
      ))}
    </div>
  );
}

// --- Page ---

interface AgentsPageProps {
  sub?: string;
  onNavigate?: (sub: string) => void;
}

export function AgentsPage({ sub, onNavigate }: AgentsPageProps) {
  const [configs, setConfigs] = useState<AgentConfig[]>([]);
  const [settings, setSettings] = useState<AgentSettings | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [saving, setSaving] = useState(false);
  const [toast, setToast] = useState("");

  const subParts = (sub || "").split("/").filter(Boolean);
  const selectedId = subParts[0] || null;
  const activeTab: Tab = (subParts[1] === "sessions" ? "sessions" : "config");
  const setActiveTab = (tab: Tab) =>
    onNavigate?.(selectedId ? (tab === "config" ? selectedId : `${selectedId}/${tab}`) : "");

  const loadConfigs = useCallback(() => {
    fetchAgentConfigs()
      .then((rows) => {
        const data = [...rows].sort((a, b) => agentRank(a.agent_id) - agentRank(b.agent_id));
        setConfigs(data);
        setError("");
        if (!selectedId && data.length > 0) {
          onNavigate?.(data[0].agent_id);
        }
      })
      .catch((e) => setError(e.message))
      .finally(() => setLoading(false));
  }, [selectedId, onNavigate]);

  const loadDefaults = useCallback(() => {
    fetchAgentSettings().then(setSettings).catch(() => {});
  }, []);

  useEffect(() => {
    loadConfigs();
    loadDefaults();
  }, [loadConfigs, loadDefaults]);

  const selected = configs.find((c) => c.agent_id === selectedId) ?? null;

  const handleSave = async (fields: Partial<AgentConfig>) => {
    if (!selectedId) return;
    setSaving(true);
    try {
      const updated = await updateAgentConfig(selectedId, fields);
      setConfigs((prev) => prev.map((c) => (c.agent_id === selectedId ? updated : c)));
      setToast("Saved");
      setTimeout(() => setToast(""), 2000);
    } catch (e) {
      setToast(`Error: ${(e as Error).message}`);
      setTimeout(() => setToast(""), 3000);
    } finally {
      setSaving(false);
    }
  };

  const handleResetPrompt = async () => {
    if (!selectedId) return;
    setSaving(true);
    try {
      const updated = await resetAgentPrompt(selectedId);
      setConfigs((prev) => prev.map((c) => (c.agent_id === selectedId ? updated : c)));
      setToast("Reset to recommended prompt");
      setTimeout(() => setToast(""), 2000);
    } catch (e) {
      setToast(`Error: ${(e as Error).message}`);
      setTimeout(() => setToast(""), 3000);
    } finally {
      setSaving(false);
    }
  };

  if (loading) {
    return (
      <div className="flex h-64 items-center justify-center">
        <Loader2 className="h-6 w-6 animate-spin text-muted-foreground" />
      </div>
    );
  }

  return (
    <div className="space-y-6">
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-2xl font-bold">Agents</h1>
          <p className="text-sm text-muted-foreground mt-1">
            {configs.length} agents
          </p>
        </div>
        <button
          type="button"
          onClick={() => { loadConfigs(); loadDefaults(); }}
          className="flex items-center gap-2 rounded-lg border border-border px-3 py-2 text-sm text-muted-foreground hover:text-foreground transition-colors"
        >
          <RefreshCw className="h-4 w-4" />
          Refresh
        </button>
      </div>

      {error && (
        <div className="rounded-lg border border-destructive/50 bg-destructive/10 px-5 py-4 text-sm text-destructive">
          Failed to load agent configs: {error}
        </div>
      )}

      {toast && (
        <div
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
        <div className="flex h-64 items-center justify-center">
          <p className="text-sm text-muted-foreground">No agents configured. Start the server with a database connection to seed defaults.</p>
        </div>
      )}

      <div className="flex gap-6">
        {/* Card list */}
        <div className="w-72 shrink-0 space-y-3">
          {configs.map((cfg) => (
            <AgentCard
              key={cfg.agent_id}
              config={cfg}
              selected={cfg.agent_id === selectedId}
              onSelect={() => {
                onNavigate?.(cfg.agent_id);
              }}
              agent={isRole(cfg.agent_id) ? settings?.roles[cfg.agent_id]?.effective.agent.value.name ?? null : null}
            />
          ))}
        </div>

        {/* Editor panel */}
        {selected && (
          <div className="min-w-0 flex-1 rounded-lg border border-border bg-card">
            <div className="flex items-center gap-1 border-b border-border px-4">
              {(["config", "sessions"] as Tab[]).map((tab) => (
                <button
                  key={tab}
                  type="button"
                  onClick={() => setActiveTab(tab)}
                  className={cn(
                    "px-4 py-3 text-sm font-medium border-b-2 transition-colors",
                    activeTab === tab
                      ? "border-primary text-primary"
                      : "border-transparent text-muted-foreground hover:text-foreground",
                  )}
                >
                  {tab === "config" ? "Config" : "Runs"}
                </button>
              ))}
              <div className="ml-auto flex items-center gap-2 text-xs text-muted-foreground">
                <span className="font-mono">{selected.agent_id}</span>
              </div>
            </div>

            <div className="p-5">
              {activeTab === "config" && (
                <div className="space-y-5">
                  <ConfigTab
                    config={selected}
                    onSave={handleSave}
                    onResetPrompt={handleResetPrompt}
                    saving={saving}
                  />
                  {settings && isRole(selected.agent_id) && (
                    <RoleSettingsSection role={selected.agent_id} settings={settings} onSaved={setSettings} />
                  )}
                </div>
              )}
              {activeTab === "sessions" && <RunsTab agentId={selected.agent_id} />}
            </div>
          </div>
        )}
      </div>
    </div>
  );
}
