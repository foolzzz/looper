import { useCallback, useEffect, useState } from "react";
import { Check, RotateCcw, Save } from "lucide-react";

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
    <div className="personal-agent-config-settings">
      {error ? <div className="personal-machine-error" role="alert">{error}</div> : null}
      {notice ? <div className="personal-machine-notice"><Check size={14} /> {notice}</div> : null}

      <div className="personal-agent-list">
        {agents.map((agent, index) => (
          <fieldset key={agent.id} className="personal-agent-card" disabled={busy !== ""}>
            <legend>
              <strong>{agent.id}</strong>
              <span className="personal-agent-role-badge">{agent.role}</span>
              {!agent.enabled ? <span className="personal-agent-disabled-badge">disabled</span> : null}
            </legend>

            <div className="personal-agent-fields">
              <label>
                <span>Role</span>
                <select value={agent.role} onChange={(e) => updateAgent(index, "role", e.target.value)}>
                  <option value="orchestrator">orchestrator</option>
                  <option value="developer">developer</option>
                  <option value="acceptor">acceptor</option>
                </select>
              </label>

              <label>
                <span>Runtime</span>
                <select value={agent.runtime} onChange={(e) => updateAgent(index, "runtime", e.target.value)}>
                  <option value="claude-code">claude-code</option>
                  <option value="codex-cli">codex-cli</option>
                </select>
              </label>

              <label>
                <span>Provider</span>
                <select value={agent.provider} onChange={(e) => updateAgent(index, "provider", e.target.value)}>
                  {providerNames.map((name) => <option key={name} value={name}>{name}</option>)}
                </select>
              </label>

              <label>
                <span>Model</span>
                <input
                  type="text"
                  value={agent.model}
                  onChange={(e) => updateAgent(index, "model", e.target.value)}
                  placeholder="default"
                />
              </label>

              <label>
                <span>Concurrency</span>
                <input
                  type="number"
                  min={1}
                  max={8}
                  value={agent.max_concurrency}
                  onChange={(e) => updateAgent(index, "max_concurrency", parseInt(e.target.value, 10) || 1)}
                />
              </label>

              <label>
                <span>Permission</span>
                <select
                  value={agent.permission_mode}
                  onChange={(e) => updateAgent(index, "permission_mode", e.target.value)}
                >
                  <option value="">default</option>
                  <option value="acceptEdits">acceptEdits</option>
                  <option value="bypassPermissions">bypassPermissions</option>
                  <option value="plan">plan</option>
                </select>
              </label>

              <label className="personal-agent-toggle">
                <input
                  type="checkbox"
                  checked={agent.enabled}
                  onChange={(e) => updateAgent(index, "enabled", e.target.checked)}
                />
                <span>Enabled</span>
              </label>
            </div>

            <div className="personal-agent-actions">
              <button
                type="button"
                onClick={() => saveAgent(index)}
                disabled={busy !== ""}
              >
                <Save size={14} />
                <span>Save</span>
              </button>
            </div>
          </fieldset>
        ))}
      </div>

      <div className="personal-agent-toolbar">
        <button type="button" onClick={() => void reload()} disabled={busy !== ""}>
          <RotateCcw size={14} />
          <span>Reload</span>
        </button>
      </div>
    </div>
  );
}
