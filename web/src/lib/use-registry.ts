import { useCallback, useEffect, useRef, useState } from "react";
import { fetchRegistry, fetchRegistryV3, REGISTRY_CHANGED_EVENT, type AgentProfile, type AgentProfileV3, type RegistryV3 } from "@/lib/api";

/**
 * The registry (runtimes, providers, Agent Profiles), loaded once and again at once when this dashboard changes it. `registry` is null until
 * the first load succeeds.
 */
function useRegistryData<T>(fetchData: () => Promise<T>) {
  const [registry, setRegistry] = useState<T | null>(null);
  const [error, setError] = useState("");
  const latest = useRef(0);

  const reload = useCallback((): Promise<void> => {
    // A slow answer must not overwrite the answer to a newer request.
    const seq = ++latest.current;
    return fetchData()
      .then((next) => {
        if (seq !== latest.current) return;
        setRegistry(next);
        setError("");
      })
      .catch((err) => {
        if (seq !== latest.current) return;
        setError(err instanceof Error ? err.message : String(err));
      });
  }, [fetchData]);

  useEffect(() => {
    reload();
    window.addEventListener(REGISTRY_CHANGED_EVENT, reload);
    return () => window.removeEventListener(REGISTRY_CHANGED_EVENT, reload);
  }, [reload]);

  return { registry, error, reload };
}

/** Legacy consumers keep their existing return type until they migrate together. */
export function useRegistry() {
  return useRegistryData(fetchRegistry);
}

/** Read normalized accounts/models without adapting models or prices into an account catalog. */
export function useRegistryV3() {
  return useRegistryData<RegistryV3>(fetchRegistryV3);
}

/** A profile as the dashboard names it: its name, then " · <effort>" when it has an effort its name does not end
 * with already (the upgrade named the profiles it added "claude-code/opus · xhigh"). */
export function profileName(profile: { name: string; effort: string | null }) {
  const suffix = profile.effort ? ` · ${profile.effort}` : "";
  return suffix && !profile.name.endsWith(suffix) ? `${profile.name}${suffix}` : profile.name;
}

/** A profile as a select option names it: "claude-code/sonnet · high — claude-code · claude-subscription · sonnet". */
export function profileLabel(profile: AgentProfile) {
  return `${profileName(profile)} — ${profile.runtime_name} · ${profile.provider_name} · ${profile.model}`;
}

/** The form-local V3 label uses the model's derived account and exact protocol. */
export function profileLabelV3(profile: AgentProfileV3) {
  return `${profileName(profile)} — ${profile.runtime_name} · ${profile.provider.name} / ${profile.model.name} / ${profile.model.protocol}`;
}
