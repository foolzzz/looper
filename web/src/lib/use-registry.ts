import { useCallback, useEffect, useRef, useState } from "react";
import { fetchRegistry, REGISTRY_CHANGED_EVENT, type AgentProfile, type Registry } from "@/lib/api";

/**
 * The registry (runtimes, providers, Agent Profiles), loaded once and again at once when this dashboard changes it. `registry` is null until
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

/** A profile as a select option names it: "claude-code/sonnet — claude-code · claude-subscription · sonnet · effort
 * high", its effort left out when it has none or its name already ends with it ("claude-code/opus · xhigh"). */
export function profileLabel(profile: AgentProfile) {
  const effort = profile.effort && !profile.name.endsWith(` · ${profile.effort}`) ? ` · effort ${profile.effort}` : "";
  return `${profile.name} — ${profile.runtime_name} · ${profile.provider_name} · ${profile.model}${effort}`;
}
