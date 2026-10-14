import { useCallback, useEffect, useRef, useState } from "react";
import { fetchRegistry, REGISTRY_CHANGED_EVENT, type Registry, type RegistryAgent } from "@/lib/api";

/**
 * The agent registry, loaded once and again at once when this dashboard changes it. `registry` is null until
 * the first load succeeds.
 */
export function useRegistry() {
  const [registry, setRegistry] = useState<Registry | null>(null);
  const [error, setError] = useState("");
  const latest = useRef(0);

  const reload = useCallback((): Promise<void> => {
    // A slow answer must not overwrite the answer to a newer request.
    const seq = ++latest.current;
    return fetchRegistry()
      .then((next) => {
        if (seq !== latest.current) return;
        setRegistry(next);
        setError("");
      })
      .catch((err) => {
        if (seq !== latest.current) return;
        setError(err instanceof Error ? err.message : String(err));
      });
  }, []);

  useEffect(() => {
    reload();
    window.addEventListener(REGISTRY_CHANGED_EVENT, reload);
    return () => window.removeEventListener(REGISTRY_CHANGED_EVENT, reload);
  }, [reload]);

  return { registry, error, reload };
}

/** An agent as a select option names it: "claude-code/sonnet — claude-code · claude-subscription · sonnet". */
export function agentLabel(agent: RegistryAgent) {
  return `${agent.name} — ${agent.runtime_name} · ${agent.provider_name} · ${agent.model}`;
}
