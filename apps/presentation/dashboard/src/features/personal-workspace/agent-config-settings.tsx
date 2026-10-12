import { useCallback, useEffect, useState } from "react";
import { Check, RotateCcw, Save, User } from "lucide-react";

import {
  fetchAgentConfig,
  writeAgentConfig,
  type AgentConfigEntry,
  type ProviderConfigEntry,
} from "../../data/chat";
import { useWorkspaceI18n } from "./i18n";

interface AgentDraft {
  id: string;
  role: string;
  runtime: string;
  provider: string;
  model: string;
  max_concurrency: number;
  permission_mode: string;
  enabled: boolean;
}

function agentToDraft(agent: AgentConfigEntry): AgentDraft {
  return {
    id: agent.id,
    role: agent.role ?? "",
    runtime: agent.runtime ?? "",
    provider: agent.provider ?? "",
    model: agent.model ?? "",
    max_concurrency: agent.max_concurrency ?? 1,
    permission_mode: agent.permission_mode ?? "",
    enabled: agent.enabled !== false,
  };
}

const roleLabels: Record<string, string> = {
  orchestrator: "Orchestrator",
  developer: "Developer",
  acceptor: "Acceptor",
};

export function AgentConfigSettings() {
  const { t } = useWorkspaceI18n();
  const [agents, setAgents] = useState<AgentDraft[]>([]);
  const [providers, setProviders] = useState<ProviderConfigEntry[]>([]);
  const [busy, setBusy] = useState<string>("");
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);

  const reload = useCallback(async () => {
    setBusy("load");
    setError(null);
    try {
      const data = await fetchAgentConfig();
      setAgents(data.agents.filter((a) => a.valid !== false).map(agentToDraft));
      setProviders(data.providers);
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "Failed to load agent config");
    } finally {
      setBusy("");
    }
  }, []);

  useEffect(() => {
    void reload();
  }, [reload]);

  function updateAgent(index: number, field: keyof AgentDraft, value: string | number | boolean) {
    setAgents((prev) => {
      const next = [...prev];
      next[index] = { ...next[index], [field]: value };
      return next;
    });
    setNotice(null);
  }

  async function saveAgent(index: number) {
    const agent = agents[index];
    setBusy(`save-${agent.id}`);
    setError(null);
    setNotice(null);
    try {
      const data = await writeAgentConfig(agent.id, {
        role: agent.role,
        runtime: agent.runtime,
        provider: agent.provider,
        model: agent.model || undefined,
        max_concurrency: agent.max_concurrency,
        permission_mode: agent.permission_mode || undefined,
        enabled: agent.enabled,
      });
      setAgents(data.agents.filter((a) => a.valid !== false).map(agentToDraft));
      setNotice(`${agent.id} saved`);
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "Failed to save");
    } finally {
      setBusy("");
    }
  }

  if (!agents.length && busy === "load") {
    return <div className="personal-machine-loading" role="status">Loading...</div>;
  }

  const providerNames = providers.map((p) => p.name);

  return (
    <div className="agent-config-root">
      {error ? <div className="personal-machine-error" role="alert">{error}</div> : null}
      {notice ? <div className="personal-machine-notice"><Check size={14} /> {notice}</div> : null}

      <div className="agent-config-grid">
        {agents.map((agent, index) => (
          <div key={agent.id} className={`agent-card${!agent.enabled ? " is-disabled" : ""}`}>
            <div className="agent-card-header">
              <span className="agent-card-avatar">
                <User size={18} />
              </span>
              <div className="agent-card-identity">
                <h3>{agent.id}</h3>
                <span className={`agent-card-role-tag is-${agent.role}`}>
                  {roleLabels[agent.role] ?? agent.role}
                </span>
              </div>
              <label className="agent-card-toggle">
                <input
                  type="checkbox"
                  checked={agent.enabled}
                  onChange={(e) => updateAgent(index, "enabled", e.target.checked)}
                  disabled={busy !== ""}
                />
                <span>{agent.enabled ? "ON" : "OFF"}</span>
              </label>
            </div>

            <dl className="agent-card-props">
              <div>
                <dt>Runtime</dt>
                <dd>
                  <select value={agent.runtime} onChange={(e) => updateAgent(index, "runtime", e.target.value)} disabled={busy !== ""}>
                    <option value="claude-code">claude-code</option>
                    <option value="codex-cli">codex-cli</option>
                  </select>
                </dd>
              </div>
              <div>
                <dt>Provider</dt>
                <dd>
                  <select value={agent.provider} onChange={(e) => updateAgent(index, "provider", e.target.value)} disabled={busy !== ""}>
                    {providerNames.map((name) => <option key={name} value={name}>{name}</option>)}
                  </select>
                </dd>
              </div>
              <div>
                <dt>Model</dt>
                <dd>
                  <input
                    type="text"
                    value={agent.model}
                    onChange={(e) => updateAgent(index, "model", e.target.value)}
                    placeholder="default"
                    disabled={busy !== ""}
                  />
                </dd>
              </div>
              <div>
                <dt>Concurrency</dt>
                <dd>
                  <input
                    type="number"
                    min={1}
                    max={8}
                    value={agent.max_concurrency}
                    onChange={(e) => updateAgent(index, "max_concurrency", parseInt(e.target.value, 10) || 1)}
                    disabled={busy !== ""}
                  />
                </dd>
              </div>
              <div>
                <dt>Permission</dt>
                <dd>
                  <select
                    value={agent.permission_mode}
                    onChange={(e) => updateAgent(index, "permission_mode", e.target.value)}
                    disabled={busy !== ""}
                  >
                    <option value="">default</option>
                    <option value="acceptEdits">acceptEdits</option>
                    <option value="bypassPermissions">bypassPermissions</option>
                    <option value="plan">plan</option>
                  </select>
                </dd>
              </div>
            </dl>

            <div className="agent-card-footer">
              <button
                type="button"
                className="personal-primary-action"
                onClick={() => saveAgent(index)}
                disabled={busy !== ""}
              >
                <Save size={13} />
                <span>Save</span>
              </button>
            </div>
          </div>
        ))}
      </div>

      <div className="agent-config-toolbar">
        <button type="button" className="personal-secondary-action" onClick={() => void reload()} disabled={busy !== ""}>
          <RotateCcw size={13} />
          <span>Reload</span>
        </button>
      </div>
    </div>
  );
}
