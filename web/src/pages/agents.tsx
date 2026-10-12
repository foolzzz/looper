import { useEffect, useState, useCallback, useRef } from "react";
import {
  Bot,
  User,
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
  ArrowLeft,
  MessageSquare,
  Terminal,
  AlertCircle,
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
  fetchAgentSessions,
  fetchSessionMessages,
  type AgentConfig,
  type LlmRecord,
  type LlmProfile,
  type AgentSession,
  type SessionMessage,
  type ActivityEvent,
} from "@/lib/api";

type Tab = "config" | "sessions";

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

      <div className="space-y-1.5">
        <div className="flex items-center justify-between">
          <label className="text-xs font-medium text-muted-foreground">System Prompt</label>
          <span className="text-[10px] text-muted-foreground">{form.system_prompt.length} chars</span>
        </div>
        <textarea
          value={form.system_prompt}
          onChange={(e) => setForm({ ...form, system_prompt: e.target.value })}
          className="w-full min-h-[160px] resize-y rounded-md border border-input bg-transparent p-3 text-sm text-foreground font-mono leading-relaxed placeholder:text-muted-foreground focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring"
          placeholder="Enter system prompt..."
        />
      </div>

      <Button onClick={handleSave} disabled={saving} className="w-full">
        {saving ? <Loader2 className="h-4 w-4 animate-spin" /> : <Save className="h-4 w-4" />}
        Save Config
      </Button>
    </div>
  );
}

// --- Sessions Tab ---

function SessionsTab({ agentId }: { agentId: string }) {
  const [sessions, setSessions] = useState<AgentSession[]>([]);
  const [loading, setLoading] = useState(true);
  const [activeSession, setActiveSession] = useState<string | null>(null);

  useEffect(() => {
    setLoading(true);
    setActiveSession(null);
    fetchAgentSessions(agentId)
      .then(setSessions)
      .catch(() => setSessions([]))
      .finally(() => setLoading(false));
  }, [agentId]);

  if (loading) {
    return (
      <div className="flex h-40 items-center justify-center">
        <Loader2 className="h-5 w-5 animate-spin text-muted-foreground" />
      </div>
    );
  }

  if (activeSession) {
    return (
      <SessionDetail
        agentId={agentId}
        taskId={activeSession}
        onBack={() => setActiveSession(null)}
      />
    );
  }

  if (!sessions.length) {
    return (
      <div className="flex h-40 items-center justify-center">
        <p className="text-sm text-muted-foreground">No sessions yet</p>
      </div>
    );
  }

  return (
    <div className="space-y-2">
      {sessions.map((s) => (
        <button
          key={s.task_id}
          type="button"
          onClick={() => setActiveSession(s.task_id)}
          className="w-full text-left rounded-lg border border-border bg-card px-4 py-3 hover:bg-accent/50 transition-colors"
        >
          <div className="flex items-start justify-between gap-3">
            <div className="min-w-0 flex-1">
              <p className="text-sm font-medium truncate">
                {s.requirement || s.task_id}
              </p>
              <div className="mt-1 flex items-center gap-3 text-xs text-muted-foreground">
                <span className="flex items-center gap-1">
                  <MessageSquare className="h-3 w-3" />
                  {s.turn_count} turns
                </span>
                {s.total_duration_ms != null && (
                  <span className="flex items-center gap-1">
                    <Clock className="h-3 w-3" />
                    {Math.round(s.total_duration_ms / 1000)}s
                  </span>
                )}
                <span>{formatTime(s.last_at)}</span>
              </div>
            </div>
            <div className="flex flex-col items-end gap-1">
              {s.task_phase && s.task_phase !== "pending" && (
                <Badge
                  variant={
                    s.task_phase === "completed" ? "success" :
                    s.task_phase === "failed" ? "destructive" : "default"
                  }
                  className="text-[10px]"
                >
                  {s.task_phase}
                </Badge>
              )}
              {s.task_status && (
                <Badge variant="outline" className="text-[10px]">{s.task_status}</Badge>
              )}
            </div>
          </div>
        </button>
      ))}
    </div>
  );
}

function SessionDetail({
  agentId,
  taskId,
  onBack,
}: {
  agentId: string;
  taskId: string;
  onBack: () => void;
}) {
  const [messages, setMessages] = useState<SessionMessage[]>([]);
  const [activity, setActivity] = useState<ActivityEvent[]>([]);
  const [loading, setLoading] = useState(true);
  const [showFull, setShowFull] = useState<Record<number, boolean>>({});
  const scrollRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    setLoading(true);
    fetchSessionMessages(agentId, taskId)
      .then((data) => {
        setMessages(data.messages || []);
        setActivity(data.activity || []);
      })
      .catch(() => {})
      .finally(() => setLoading(false));
  }, [agentId, taskId]);

  useEffect(() => {
    scrollRef.current?.scrollTo({ top: scrollRef.current.scrollHeight, behavior: "smooth" });
  }, [messages]);

  if (loading) {
    return (
      <div className="flex h-40 items-center justify-center">
        <Loader2 className="h-5 w-5 animate-spin text-muted-foreground" />
      </div>
    );
  }

  const toolEvents = activity.filter((a) => a.kind === "tool_use");

  return (
    <div className="space-y-3">
      <button
        type="button"
        onClick={onBack}
        className="flex items-center gap-1.5 text-sm text-muted-foreground hover:text-foreground transition-colors"
      >
        <ArrowLeft className="h-3.5 w-3.5" />
        Back to sessions
      </button>

      <div className="text-xs text-muted-foreground mb-2">
        {messages.length} LLM calls · {toolEvents.length} tool uses
      </div>

      <div ref={scrollRef} className="max-h-[500px] overflow-y-auto space-y-4 pr-1">
        {messages.map((msg) => {
          const inputText = showFull[msg.id] ? (msg.input_text || msg.input_summary) : msg.input_summary;
          const outputText = showFull[msg.id] ? (msg.output_text || msg.output_summary) : msg.output_summary;
          const hasMore = (msg.input_text && msg.input_text.length > 500) ||
                          (msg.output_text && msg.output_text.length > 500);

          return (
            <div key={msg.id} className="space-y-2">
              {inputText && (
                <div className="flex gap-3 justify-end">
                  <div className="max-w-[75%] rounded-lg px-4 py-2.5 text-sm bg-primary text-primary-foreground">
                    <p className="whitespace-pre-wrap break-words">{inputText}</p>
                  </div>
                  <div className="flex h-7 w-7 shrink-0 items-center justify-center rounded-full bg-accent">
                    <User className="h-3.5 w-3.5 text-muted-foreground" />
                  </div>
                </div>
              )}

              <div className="flex gap-3 justify-start">
                <div className="flex h-7 w-7 shrink-0 items-center justify-center rounded-full bg-primary/10">
                  <Bot className="h-3.5 w-3.5 text-primary" />
                </div>
                <div
                  className={cn(
                    "max-w-[75%] rounded-lg px-4 py-2.5 text-sm",
                    msg.error
                      ? "bg-destructive/10 text-destructive border border-destructive/30"
                      : "bg-muted text-foreground",
                  )}
                >
                  <p className="whitespace-pre-wrap break-words">
                    {msg.error || outputText || "—"}
                  </p>
                </div>
              </div>

              <div className="flex items-center gap-3 pl-10 text-[10px] text-muted-foreground/60">
                <span>{formatTime(msg.created_at)}</span>
                {msg.duration_ms != null && <span>{msg.duration_ms}ms</span>}
                {msg.model && <span>{msg.model}</span>}
                {msg.input_tokens != null && (
                  <span>{msg.input_tokens}→{msg.output_tokens} tok</span>
                )}
                {hasMore && (
                  <button
                    type="button"
                    onClick={() => setShowFull((prev) => ({ ...prev, [msg.id]: !prev[msg.id] }))}
                    className="hover:text-foreground underline"
                  >
                    {showFull[msg.id] ? "show less" : "show full"}
                  </button>
                )}
              </div>
            </div>
          );
        })}

        {toolEvents.length > 0 && (
          <div className="border-t border-border pt-3 mt-3">
            <p className="text-xs font-medium text-muted-foreground mb-2 flex items-center gap-1.5">
              <Terminal className="h-3.5 w-3.5" />
              Tool use trace ({toolEvents.length})
            </p>
            <div className="space-y-1">
              {toolEvents.map((ev) => (
                <div
                  key={ev.seq}
                  className="flex items-start gap-2 text-xs text-muted-foreground py-1"
                >
                  <Terminal className="h-3 w-3 mt-0.5 shrink-0 text-primary/60" />
                  <span className="break-all">{ev.summary}</span>
                  <span className="ml-auto shrink-0 text-[10px]">
                    {formatTime(ev.created_at)}
                  </span>
                </div>
              ))}
            </div>
          </div>
        )}
      </div>
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
      .then((data) => {
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
                onNavigate?.(cfg.agent_id);
              }}
              profile={cfg.llm_profile_id ? profileMap.get(cfg.llm_profile_id) ?? null : null}
            />
          ))}
        </div>

        {/* Editor panel */}
        {selected && (
          <div className="flex-1 rounded-lg border border-border bg-card">
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
                  {tab === "config" ? "Config" : "Sessions"}
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
              {activeTab === "sessions" && <SessionsTab agentId={selected.agent_id} />}
            </div>
          </div>
        )}
      </div>
    </div>
  );
}
