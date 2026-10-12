import { useEffect, useState, useCallback } from "react";
import {
  Bot,
  Loader2,
  RefreshCw,
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
import {
  fetchAgentConfigs,
  updateAgentConfig,
  fetchLlmRecords,
  fetchLlmProfiles,
  createLlmProfile,
  updateLlmProfile,
  deleteLlmProfile,
  type AgentConfig,
  type LlmRecord,
  type LlmProfile,
} from "@/lib/api";

type Tab = "config" | "prompt" | "history";

const CLAUDE_CODE_PERMISSIONS = [
  { value: "", label: "Default" },
  { value: "plan", label: "Plan" },
  { value: "acceptEdits", label: "Accept Edits" },
  { value: "bypassPermissions", label: "Bypass Permissions" },
];

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

function AgentCard({
  config,
  selected,
  onSelect,
  profile,
}: {
  config: AgentConfig;
  selected: boolean;
  onSelect: () => void;
  profile: LlmProfile | null;
}) {
  const displayModel = config.model || profile?.model;
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

// --- Config Tab ---

function ConfigTab({
  config,
  profiles,
  onSave,
  onProfilesChanged,
  saving,
}: {
  config: AgentConfig;
  profiles: LlmProfile[];
  onSave: (fields: Partial<AgentConfig>) => void;
  onProfilesChanged: () => void;
  saving: boolean;
}) {
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
    });
    setEditingProfile(null);
  }, [config]);

  const handleSave = () => {
    onSave({
      display_name: form.display_name,
      runtime: form.runtime,
      llm_profile_id: form.llm_profile_id,
      model: form.model || null,
      max_turns: form.max_turns,
      timeout_seconds: form.timeout_seconds,
      max_concurrency: form.max_concurrency,
      permission_mode: form.permission_mode || null,
      enabled: form.enabled,
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

  const permOptions =
    form.runtime === "codex-cli" ? CODEX_SANDBOXES : CLAUDE_CODE_PERMISSIONS;
  const permLabel = form.runtime === "codex-cli" ? "Sandbox" : "Permission Mode";

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
          <label className="text-xs font-medium text-muted-foreground">Model Override</label>
          <Input
            value={form.model}
            onChange={(e) => setForm({ ...form, model: e.target.value })}
            placeholder={selectedProfile?.model || "from profile"}
          />
          <p className="text-[11px] text-muted-foreground">Overrides profile model if set</p>
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

      <div className="space-y-1.5">
        <label className="text-xs font-medium text-muted-foreground">{permLabel}</label>
        <select
          value={form.permission_mode}
          onChange={(e) => setForm({ ...form, permission_mode: e.target.value })}
          className="flex h-9 w-full rounded-md border border-input bg-transparent px-3 py-1 text-sm text-foreground"
        >
          {permOptions.map((o) => (
            <option key={o.value} value={o.value}>
              {o.label}
            </option>
          ))}
        </select>
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

      <Button onClick={handleSave} disabled={saving} className="w-full">
        {saving ? <Loader2 className="h-4 w-4 animate-spin" /> : <Save className="h-4 w-4" />}
        Save Config
      </Button>
    </div>
  );
}

// --- Prompt Tab ---

function PromptTab({
  config,
  onSave,
  saving,
}: {
  config: AgentConfig;
  onSave: (fields: Partial<AgentConfig>) => void;
  saving: boolean;
}) {
  const [prompt, setPrompt] = useState(config.system_prompt);

  useEffect(() => {
    setPrompt(config.system_prompt);
  }, [config]);

  return (
    <div className="flex h-full flex-col gap-3">
      <div className="flex items-center justify-between">
        <label className="text-xs font-medium text-muted-foreground">System Prompt</label>
        <span className="text-xs text-muted-foreground">{prompt.length} chars</span>
      </div>
      <textarea
        value={prompt}
        onChange={(e) => setPrompt(e.target.value)}
        className="flex-1 min-h-[300px] w-full resize-none rounded-md border border-input bg-transparent p-3 text-sm text-foreground font-mono leading-relaxed placeholder:text-muted-foreground focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring"
        placeholder="Enter system prompt..."
      />
      <Button onClick={() => onSave({ system_prompt: prompt })} disabled={saving}>
        {saving ? <Loader2 className="h-4 w-4 animate-spin" /> : <Save className="h-4 w-4" />}
        Save Prompt
      </Button>
    </div>
  );
}

// --- History Tab ---

function HistoryTab({ agentId }: { agentId: string }) {
  const [records, setRecords] = useState<LlmRecord[]>([]);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    setLoading(true);
    fetchLlmRecords({ agent_id: agentId, limit: 50 })
      .then(setRecords)
      .catch(() => setRecords([]))
      .finally(() => setLoading(false));
  }, [agentId]);

  if (loading) {
    return (
      <div className="flex h-40 items-center justify-center">
        <Loader2 className="h-5 w-5 animate-spin text-muted-foreground" />
      </div>
    );
  }

  if (!records.length) {
    return (
      <div className="flex h-40 items-center justify-center">
        <p className="text-sm text-muted-foreground">No LLM invocation records yet</p>
      </div>
    );
  }

  return (
    <div className="space-y-2">
      <div className="grid grid-cols-[1fr_2fr_2fr_80px_80px] gap-2 px-3 py-2 text-xs font-medium text-muted-foreground">
        <span>Time</span>
        <span>Input</span>
        <span>Output</span>
        <span>Duration</span>
        <span>Status</span>
      </div>
      <div className="divide-y divide-border rounded-md border border-border">
        {records.map((rec) => (
          <div
            key={rec.id}
            className="grid grid-cols-[1fr_2fr_2fr_80px_80px] gap-2 px-3 py-2.5 text-xs"
          >
            <span className="text-muted-foreground truncate" title={rec.created_at}>
              {formatTime(rec.created_at)}
            </span>
            <span className="truncate" title={rec.input_summary ?? ""}>
              {rec.input_summary || "—"}
            </span>
            <span className="truncate" title={rec.output_summary ?? ""}>
              {rec.output_summary || "—"}
            </span>
            <span className="text-muted-foreground">
              {rec.duration_ms != null ? `${rec.duration_ms}ms` : "—"}
            </span>
            <span>
              {rec.error ? (
                <Badge variant="destructive">Error</Badge>
              ) : (
                <Badge variant="success">OK</Badge>
              )}
            </span>
          </div>
        ))}
      </div>
    </div>
  );
}

// --- Page ---

export function AgentsPage() {
  const [configs, setConfigs] = useState<AgentConfig[]>([]);
  const [profiles, setProfiles] = useState<LlmProfile[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [activeTab, setActiveTab] = useState<Tab>("config");
  const [saving, setSaving] = useState(false);
  const [toast, setToast] = useState("");

  const loadConfigs = useCallback(() => {
    fetchAgentConfigs()
      .then((data) => {
        setConfigs(data);
        setError("");
        setSelectedId((prev) => prev ?? (data.length > 0 ? data[0].agent_id : null));
      })
      .catch((e) => setError(e.message))
      .finally(() => setLoading(false));
  }, []);

  const loadProfiles = useCallback(() => {
    fetchLlmProfiles().then(setProfiles).catch(() => {});
  }, []);

  useEffect(() => {
    loadConfigs();
    loadProfiles();
  }, [loadConfigs, loadProfiles]);

  const selected = configs.find((c) => c.agent_id === selectedId) ?? null;
  const profileMap = new Map(profiles.map((p) => [p.id, p]));

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
          onClick={() => { loadConfigs(); loadProfiles(); }}
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
                setSelectedId(cfg.agent_id);
                setActiveTab("config");
              }}
              profile={cfg.llm_profile_id ? profileMap.get(cfg.llm_profile_id) ?? null : null}
            />
          ))}
        </div>

        {/* Editor panel */}
        {selected && (
          <div className="flex-1 rounded-lg border border-border bg-card">
            <div className="flex items-center gap-1 border-b border-border px-4">
              {(["config", "prompt", "history"] as Tab[]).map((tab) => (
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
                  {tab === "config" ? "Config" : tab === "prompt" ? "Prompt" : "History"}
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
                  onSave={handleSave}
                  onProfilesChanged={loadProfiles}
                  saving={saving}
                />
              )}
              {activeTab === "prompt" && (
                <PromptTab config={selected} onSave={handleSave} saving={saving} />
              )}
              {activeTab === "history" && <HistoryTab agentId={selected.agent_id} />}
            </div>
          </div>
        )}
      </div>
    </div>
  );
}
