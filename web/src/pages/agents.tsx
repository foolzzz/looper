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
  Plus,
  Pencil,
  Trash2,
  ChevronDown,
  ChevronUp,
  Database,
} from "lucide-react";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { cn } from "@/lib/utils";
import { HarnessField, harnessTooLong } from "@/components/harness-field";
import {
  fetchAgentConfigs,
  fetchAgentSettings,
  updateAgentConfig,
  resetAgentPrompt,
  fetchLlmRecords,
  fetchLlmProfiles,
  createLlmProfile,
  updateLlmProfile,
  deleteLlmProfile,
  type AgentConfig,
  type LlmRecord,
  type LlmProfile,
} from "@/lib/api";
import { historyHref } from "@/pages/agent-history";

type Tab = "config" | "sessions";

const CODEX_SANDBOXES = [
  { value: "", label: "Default" },
  { value: "full-auto", label: "Full Auto" },
  { value: "suggest", label: "Suggest" },
  { value: "ask", label: "Ask" },
];

const PROVIDER_KINDS = [
  { value: "anthropic", label: "Anthropic", runtimes: ["claude-code"] },
  { value: "openai", label: "OpenAI", runtimes: ["codex-cli"] },
  { value: "openai-compatible", label: "OpenAI Compatible", runtimes: ["codex-cli"] },
];

function runtimeBadge(runtime: string) {
  return runtime === "codex-cli" ? (
    <Badge variant="warning">Codex CLI</Badge>
  ) : (
    <Badge variant="default">Claude Code</Badge>
  );
}

function providerBadge(kind: string) {
  switch (kind) {
    case "anthropic":
      return <Badge variant="default">Anthropic</Badge>;
    case "openai":
      return <Badge variant="warning">OpenAI</Badge>;
    default:
      return <Badge variant="outline">{kind}</Badge>;
  }
}

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
  defaultModel,
}: {
  config: AgentConfig;
  selected: boolean;
  onSelect: () => void;
  /** The default model in Settings, which an agent without its own model runs with. */
  defaultModel: string | null;
}) {
  const displayModel = config.model || defaultModel;
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
            {runtimeBadge(config.runtime)}
          </div>
          <div className="mt-2 flex items-center gap-3 text-xs text-muted-foreground">
            {displayModel && (
              <span className="flex items-center gap-1">
                <Cpu className="h-3 w-3" />
                {displayModel}
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

// --- Profile Editor (inline) ---

function ProfileEditor({
  profile,
  onSaved,
  onCancel,
}: {
  profile: LlmProfile | null;
  onSaved: (p: LlmProfile) => void;
  onCancel: () => void;
}) {
  const isNew = !profile;
  const [form, setForm] = useState({
    name: profile?.name ?? "",
    display_name: profile?.display_name ?? "",
    provider_kind: profile?.provider_kind ?? "anthropic",
    model: profile?.model ?? "",
    base_url: profile?.base_url ?? "",
    api_key_env: profile?.api_key_env ?? "",
    reasoning_effort: profile?.reasoning_effort ?? "",
    description: profile?.description ?? "",
  });
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState("");

  const handleSave = async () => {
    if (!form.name.trim()) {
      setError("Name is required");
      return;
    }
    setSaving(true);
    setError("");
    try {
      const fields = {
        ...form,
        base_url: form.base_url || null,
        api_key_env: form.api_key_env || null,
        reasoning_effort: form.reasoning_effort || null,
      };
      const saved = isNew
        ? await createLlmProfile(fields)
        : await updateLlmProfile(profile!.id, fields);
      onSaved(saved);
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setSaving(false);
    }
  };

  return (
    <div className="space-y-3 rounded-md border border-border bg-muted/30 p-4">
      <div className="flex items-center justify-between">
        <span className="text-xs font-semibold text-foreground">
          {isNew ? "New Profile" : "Edit Profile"}
        </span>
        <button
          type="button"
          onClick={onCancel}
          className="text-xs text-muted-foreground hover:text-foreground"
        >
          Cancel
        </button>
      </div>

      {error && (
        <p className="text-xs text-destructive">{error}</p>
      )}

      <div className="grid grid-cols-2 gap-3">
        <div className="space-y-1">
          <label className="text-[11px] text-muted-foreground">Name (slug)</label>
          <Input
            value={form.name}
            onChange={(e) => setForm({ ...form, name: e.target.value })}
            placeholder="my-profile"
            disabled={!isNew}
          />
        </div>
        <div className="space-y-1">
          <label className="text-[11px] text-muted-foreground">Display Name</label>
          <Input
            value={form.display_name}
            onChange={(e) => setForm({ ...form, display_name: e.target.value })}
            placeholder="My LLM Profile"
          />
        </div>
      </div>

      <div className="grid grid-cols-2 gap-3">
        <div className="space-y-1">
          <label className="text-[11px] text-muted-foreground">Provider</label>
          <select
            value={form.provider_kind}
            onChange={(e) => setForm({ ...form, provider_kind: e.target.value })}
            className="flex h-9 w-full rounded-md border border-input bg-transparent px-3 py-1 text-sm text-foreground"
          >
            {PROVIDER_KINDS.map((p) => (
              <option key={p.value} value={p.value}>{p.label}</option>
            ))}
          </select>
        </div>
        <div className="space-y-1">
          <label className="text-[11px] text-muted-foreground">Model</label>
          <Input
            value={form.model}
            onChange={(e) => setForm({ ...form, model: e.target.value })}
            placeholder="claude-sonnet-4-20250514"
          />
        </div>
      </div>

      <div className="grid grid-cols-2 gap-3">
        <div className="space-y-1">
          <label className="text-[11px] text-muted-foreground">Base URL</label>
          <Input
            value={form.base_url}
            onChange={(e) => setForm({ ...form, base_url: e.target.value })}
            placeholder="https://api.anthropic.com (optional)"
          />
        </div>
        <div className="space-y-1">
          <label className="text-[11px] text-muted-foreground">API Key Env Var</label>
          <Input
            value={form.api_key_env}
            onChange={(e) => setForm({ ...form, api_key_env: e.target.value })}
            placeholder="ANTHROPIC_API_KEY (optional)"
          />
        </div>
      </div>

      <div className="grid grid-cols-2 gap-3">
        <div className="space-y-1">
          <label className="text-[11px] text-muted-foreground">Reasoning Effort</label>
          <select
            value={form.reasoning_effort}
            onChange={(e) => setForm({ ...form, reasoning_effort: e.target.value })}
            className="flex h-9 w-full rounded-md border border-input bg-transparent px-3 py-1 text-sm text-foreground"
          >
            <option value="">Default</option>
            <option value="low">Low</option>
            <option value="medium">Medium</option>
            <option value="high">High</option>
          </select>
        </div>
        <div className="space-y-1">
          <label className="text-[11px] text-muted-foreground">Description</label>
          <Input
            value={form.description}
            onChange={(e) => setForm({ ...form, description: e.target.value })}
            placeholder="Optional description"
          />
        </div>
      </div>

      <Button onClick={handleSave} disabled={saving} size="sm" className="w-full">
        {saving ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <Save className="h-3.5 w-3.5" />}
        {isNew ? "Create Profile" : "Save Profile"}
      </Button>
    </div>
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

/** Where an agent's harness comes from, for the agents whose harness is not edited here. */
const HARNESS_NOTE: Record<string, string> = {
  planner: "Harness: each project's Planner override, else the default in Settings.",
  worker: "Harness: each project's Worker override, else the default in Settings.",
  reviewer: "Harness: the same as the Workers of its project (their override, else the default in Settings).",
};

function ConfigTab({
  config,
  profiles,
  defaultModel,
  onSave,
  onResetPrompt,
  onProfilesChanged,
  saving,
}: {
  config: AgentConfig;
  profiles: LlmProfile[];
  /** The default model in Settings; null while it loads. */
  defaultModel: string | null;
  onSave: (fields: Partial<AgentConfig>) => void;
  onResetPrompt: () => void;
  onProfilesChanged: () => void;
  saving: boolean;
}) {
  const isManager = config.agent_id === "manager";
  const [harness, setHarness] = useState(config.harness_prompt ?? "");
  const [form, setForm] = useState({
    display_name: config.display_name,
    runtime: config.runtime,
    llm_profile_id: config.llm_profile_id,
    model: config.model ?? "",
    max_turns: config.max_turns,
    timeout_seconds: config.timeout_seconds,
    max_concurrency: config.max_concurrency,
    permission_mode: config.permission_mode ?? "",
    enabled: config.enabled,
    system_prompt: config.system_prompt,
  });
  const [profileExpanded, setProfileExpanded] = useState(false);
  const [editingProfile, setEditingProfile] = useState<"new" | "edit" | null>(null);

  useEffect(() => {
    setForm({
      display_name: config.display_name,
      runtime: config.runtime,
      llm_profile_id: config.llm_profile_id,
      model: config.model ?? "",
      max_turns: config.max_turns,
      timeout_seconds: config.timeout_seconds,
      max_concurrency: config.max_concurrency,
      permission_mode: config.permission_mode ?? "",
      enabled: config.enabled,
      system_prompt: config.system_prompt,
    });
    setHarness(config.harness_prompt ?? "");
    setEditingProfile(null);
  }, [config]);

  const handleSave = () => {
    onSave({
      // Only the Manager has a harness of its own; the server refuses it for the others.
      ...(isManager ? { harness_prompt: harness } : {}),
      display_name: form.display_name,
      runtime: form.runtime,
      llm_profile_id: form.llm_profile_id,
      model: form.model || null,
      max_turns: form.max_turns,
      timeout_seconds: form.timeout_seconds,
      max_concurrency: form.max_concurrency,
      permission_mode: form.permission_mode || null,
      enabled: form.enabled,
      system_prompt: form.system_prompt,
    } as Partial<AgentConfig>);
  };

  const compatibleProfiles = profiles.filter((p) => {
    const pk = PROVIDER_KINDS.find((k) => k.value === p.provider_kind);
    return !pk || pk.runtimes.includes(form.runtime);
  });

  const selectedProfile = profiles.find((p) => p.id === form.llm_profile_id) ?? null;

  const handleDeleteProfile = async () => {
    if (!selectedProfile) return;
    if (!confirm(`Delete profile "${selectedProfile.display_name || selectedProfile.name}"?`)) return;
    try {
      await deleteLlmProfile(selectedProfile.id);
      setForm({ ...form, llm_profile_id: null });
      onProfilesChanged();
    } catch {
      // ignore
    }
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

      <div className="grid grid-cols-2 gap-4">
        <div className="space-y-1.5">
          <label className="text-xs font-medium text-muted-foreground">Runtime</label>
          <select
            value={form.runtime}
            onChange={(e) => setForm({ ...form, runtime: e.target.value, permission_mode: "", llm_profile_id: null })}
            className="flex h-9 w-full rounded-md border border-input bg-transparent px-3 py-1 text-sm text-foreground"
          >
            <option value="claude-code">Claude Code</option>
            <option value="codex-cli">Codex CLI</option>
          </select>
        </div>
        <div className="space-y-1.5">
          <label htmlFor="agent-model" className="text-xs font-medium text-muted-foreground">Model</label>
          <Input
            id="agent-model"
            value={form.model}
            onChange={(e) => setForm({ ...form, model: e.target.value })}
            placeholder={defaultModel ? `${defaultModel} (default)` : "the default model"}
            className="font-mono"
          />
          <p className="text-[11px] text-muted-foreground">Empty: the default model in Settings</p>
        </div>
      </div>

      {/* LLM Profile section */}
      <div className="space-y-2">
        <button
          type="button"
          onClick={() => setProfileExpanded(!profileExpanded)}
          className="flex w-full items-center gap-2 text-xs font-medium text-muted-foreground hover:text-foreground transition-colors"
        >
          <Database className="h-3.5 w-3.5" />
          LLM Profile
          {selectedProfile && (
            <span className="ml-1 text-foreground">
              — {selectedProfile.display_name || selectedProfile.name}
            </span>
          )}
          {profileExpanded ? <ChevronUp className="ml-auto h-3.5 w-3.5" /> : <ChevronDown className="ml-auto h-3.5 w-3.5" />}
        </button>

        {profileExpanded && (
          <div className="space-y-3">
            {/* Profile selector */}
            <div className="flex items-center gap-2">
              <select
                value={form.llm_profile_id ?? ""}
                onChange={(e) => setForm({ ...form, llm_profile_id: e.target.value ? Number(e.target.value) : null })}
                className="flex h-9 flex-1 rounded-md border border-input bg-transparent px-3 py-1 text-sm text-foreground"
              >
                <option value="">None (use defaults)</option>
                {compatibleProfiles.map((p) => (
                  <option key={p.id} value={p.id}>
                    {p.display_name || p.name} — {p.provider_kind}/{p.model}
                  </option>
                ))}
              </select>
              <Button
                variant="outline"
                size="sm"
                onClick={() => setEditingProfile("new")}
                title="New profile"
              >
                <Plus className="h-3.5 w-3.5" />
              </Button>
              {selectedProfile && (
                <>
                  <Button
                    variant="outline"
                    size="sm"
                    onClick={() => setEditingProfile("edit")}
                    title="Edit profile"
                  >
                    <Pencil className="h-3.5 w-3.5" />
                  </Button>
                  <Button
                    variant="outline"
                    size="sm"
                    onClick={handleDeleteProfile}
                    title="Delete profile"
                  >
                    <Trash2 className="h-3.5 w-3.5" />
                  </Button>
                </>
              )}
            </div>

            {/* Selected profile summary */}
            {selectedProfile && !editingProfile && (
              <div className="rounded-md border border-border bg-muted/20 px-4 py-3 text-xs space-y-1">
                <div className="flex items-center gap-2">
                  {providerBadge(selectedProfile.provider_kind)}
                  <span className="font-mono">{selectedProfile.model}</span>
                </div>
                {selectedProfile.base_url && (
                  <p className="text-muted-foreground">Endpoint: {selectedProfile.base_url}</p>
                )}
                {selectedProfile.reasoning_effort && (
                  <p className="text-muted-foreground">Reasoning: {selectedProfile.reasoning_effort}</p>
                )}
                {selectedProfile.description && (
                  <p className="text-muted-foreground">{selectedProfile.description}</p>
                )}
              </div>
            )}

            {/* Inline editor */}
            {editingProfile && (
              <ProfileEditor
                profile={editingProfile === "edit" ? selectedProfile : null}
                onSaved={(saved) => {
                  setForm({ ...form, llm_profile_id: saved.id });
                  setEditingProfile(null);
                  onProfilesChanged();
                }}
                onCancel={() => setEditingProfile(null)}
              />
            )}
          </div>
        )}
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

      {form.runtime === "codex-cli" ? (
        <div className="space-y-1.5">
          <label className="text-xs font-medium text-muted-foreground">Sandbox</label>
          <select
            value={form.permission_mode}
            onChange={(e) => setForm({ ...form, permission_mode: e.target.value })}
            className="flex h-9 w-full rounded-md border border-input bg-transparent px-3 py-1 text-sm text-foreground"
          >
            {CODEX_SANDBOXES.map((o) => (
              <option key={o.value} value={o.value}>
                {o.label}
              </option>
            ))}
          </select>
        </div>
      ) : (
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
      )}

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

      {isManager ? (
        <HarnessField
          id="manager-harness"
          label="Harness prompt (override)"
          value={harness}
          onChange={setHarness}
          inherits="the default"
          rows={harness.trim() ? 10 : 3}
          hint={
            <>
              The Manager&apos;s working principles, boundaries and tone, before its role prompt. Empty: the default
              harness from <a href="#settings" className="underline underline-offset-2 hover:text-foreground">Settings</a>.
            </>
          }
        />
      ) : (
        HARNESS_NOTE[config.agent_id] && (
          <p className="rounded-md border border-border bg-muted/20 px-3 py-2 text-[11px] text-muted-foreground">
            {HARNESS_NOTE[config.agent_id]}
          </p>
        )
      )}

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

      <Button onClick={handleSave} disabled={saving || (isManager && harnessTooLong(harness))} className="w-full">
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
  const [profiles, setProfiles] = useState<LlmProfile[]>([]);
  const [defaultModel, setDefaultModel] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [saving, setSaving] = useState(false);
  const [toast, setToast] = useState("");

  const subParts = (sub || "").split("/").filter(Boolean);
  const selectedId = subParts[0] || null;
  const activeTab: Tab = (subParts[1] === "sessions" ? "sessions" : "config");
  const setSelectedId = (id: string | null) => onNavigate?.(id || "");
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

  const loadProfiles = useCallback(() => {
    fetchLlmProfiles().then(setProfiles).catch(() => {});
  }, []);

  const loadDefaults = useCallback(() => {
    fetchAgentSettings().then((s) => setDefaultModel(s.default_model)).catch(() => {});
  }, []);

  useEffect(() => {
    loadConfigs();
    loadProfiles();
    loadDefaults();
  }, [loadConfigs, loadProfiles, loadDefaults]);

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
            {configs.length} agents · {profiles.length} LLM profiles
          </p>
        </div>
        <button
          type="button"
          onClick={() => { loadConfigs(); loadProfiles(); loadDefaults(); }}
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
              defaultModel={defaultModel}
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
                <ConfigTab
                  config={selected}
                  profiles={profiles}
                  defaultModel={defaultModel}
                  onSave={handleSave}
                  onResetPrompt={handleResetPrompt}
                  onProfilesChanged={loadProfiles}
                  saving={saving}
                />
              )}
              {activeTab === "sessions" && <RunsTab agentId={selected.agent_id} />}
            </div>
          </div>
        )}
      </div>
    </div>
  );
}
