import { useEffect, useState, useCallback } from "react";
import { Bot, Loader2, RefreshCw, PowerOff, Clock, Cpu, Settings2 } from "lucide-react";
import { Badge } from "@/components/ui/badge";
import { cn } from "@/lib/utils";
import {
  fetchAgentSettings,
  fetchLlmRecords,
  fetchRoleConfigs,
  type AgentRole,
  type AgentSettings,
  type LlmRecord,
  type RoleConfig,
} from "@/lib/api";
import { AGENT_PROFILE, settingsHref } from "@/lib/settings-pages";
import { profileName } from "@/lib/use-registry";
import { historyHref, runRoleLabel } from "@/pages/agent-history";

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
  profile,
}: {
  config: RoleConfig;
  selected: boolean;
  onSelect: () => void;
  /** The Agent Profile this role runs with outside any project; null while it loads. */
  profile: string | null;
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
              {config.display_name || config.role}
            </span>
            {!config.enabled && <PowerOff className="h-3.5 w-3.5 text-muted-foreground" />}
          </div>
          <div className="mt-1.5 flex flex-wrap items-center gap-1.5">
            {roleBadge(config.role)}
          </div>
          <div className="mt-2 flex items-center gap-3 text-xs text-muted-foreground">
            {profile && (
              <span className="flex min-w-0 items-center gap-1">
                <Cpu className="h-3 w-3 shrink-0" />
                <span className="truncate">{profile}</span>
              </span>
            )}
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

const ROLES: readonly AgentRole[] = ["manager", "planner", "worker", "reviewer"];
const isRole = (role: string): role is AgentRole => (ROLES as readonly string[]).includes(role);

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
            {run.task_role && (
              <Badge variant="outline" className="font-mono">
                {runRoleLabel(run.task_role)}
              </Badge>
            )}
            {run.metadata?.session_reset && <Badge variant="outline">new session</Badge>}
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

interface RunsPageProps {
  sub?: string;
  onNavigate?: (sub: string) => void;
}

/** Each role's runs: choose a role, then its sessions and run history. Configuration is in Settings → Roles. */
export function RunsPage({ sub, onNavigate }: RunsPageProps) {
  const [configs, setConfigs] = useState<RoleConfig[]>([]);
  const [settings, setSettings] = useState<AgentSettings | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");

  const selectedId = (sub || "").split("/").filter(Boolean)[0] || null;

  const loadConfigs = useCallback(() => {
    fetchRoleConfigs()
      .then((rows) => {
        const data = [...rows].sort((a, b) => agentRank(a.role) - agentRank(b.role));
        setConfigs(data);
        setError("");
        if (!selectedId && data.length > 0) {
          onNavigate?.(data[0].role);
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

  const selected = configs.find((c) => c.role === selectedId) ?? null;

  if (loading) {
    return (
      <div className="flex h-64 items-center justify-center">
        <Loader2 className="h-6 w-6 animate-spin text-muted-foreground" />
      </div>
    );
  }

  return (
    <div className="space-y-6 p-6">
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-2xl font-bold">Runs</h1>
          <p className="text-sm text-muted-foreground mt-1">
            Each role&apos;s sessions and run history. Its {AGENT_PROFILE.name}, prompt and limits are in Settings →
            Roles.
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
          Failed to load the roles: {error}
        </div>
      )}

      {!error && configs.length === 0 && (
        <div className="flex h-64 items-center justify-center">
          <p className="text-sm text-muted-foreground">No roles configured. Start the server with a database connection to seed defaults.</p>
        </div>
      )}

      <div className="flex flex-col gap-6 lg:flex-row">
        {/* Card list */}
        <div className="space-y-3 lg:w-72 lg:shrink-0">
          {configs.map((cfg) => (
            <AgentCard
              key={cfg.role}
              config={cfg}
              selected={cfg.role === selectedId}
              onSelect={() => {
                onNavigate?.(cfg.role);
              }}
              profile={isRole(cfg.role) && settings?.roles[cfg.role] ? profileName(settings.roles[cfg.role].effective.profile.value) : null}
            />
          ))}
        </div>

        {selected && (
          <div className="min-w-0 flex-1 rounded-lg border border-border bg-card">
            <div className="flex flex-wrap items-center gap-x-3 gap-y-1 border-b border-border px-4 py-3">
              <h2 className="text-sm font-semibold">{selected.display_name || selected.role}</h2>
              <span className="font-mono text-xs text-muted-foreground">{selected.role}</span>
              <a
                href={settingsHref("roles", selected.role)}
                className="ml-auto flex items-center gap-1 text-xs text-primary underline-offset-2 hover:underline"
              >
                <Settings2 className="h-3.5 w-3.5" aria-hidden="true" />
                Configure in Settings → Roles
              </a>
            </div>
            <div className="p-5">
              <RunsTab agentId={selected.role} />
            </div>
          </div>
        )}
      </div>
    </div>
  );
}
