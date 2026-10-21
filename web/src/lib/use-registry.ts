import { useSyncExternalStore } from "react";
import { fetchRegistry, REGISTRY_CHANGED_EVENT, type AgentProfile, type Registry } from "@/lib/api";

type Snapshot = { registry: Registry | null; error: string };
const EMPTY: Snapshot = { registry: null, error: "" };
let snapshot = EMPTY;
let latest = 0;
const listeners = new Set<() => void>();

function publish(next: Snapshot) {
  snapshot = next;
  for (const listener of listeners) listener();
}

async function reload(): Promise<void> {
  // A slow answer must not overwrite a newer refresh or a new set of consumers.
  const seq = ++latest;
  try {
    const registry = await fetchRegistry();
    if (seq === latest) publish({ registry, error: "" });
  } catch (err) {
    if (seq === latest) publish({ registry: snapshot.registry, error: err instanceof Error ? err.message : String(err) });
  }
}

function subscribe(listener: () => void) {
  listeners.add(listener);
  if (listeners.size === 1) {
    window.addEventListener(REGISTRY_CHANGED_EVENT, reload);
    void reload();
  }
  return () => {
    listeners.delete(listener);
    if (listeners.size === 0) {
      window.removeEventListener(REGISTRY_CHANGED_EVENT, reload);
      // Nothing remains to observe mutations. Start fresh on the next mount.
      ++latest;
      snapshot = EMPTY;
    }
  };
}

const getSnapshot = () => snapshot;

/** All mounted registry consumers share one load and one refresh per dashboard mutation. */
export function useRegistry() {
  const current = useSyncExternalStore(subscribe, getSnapshot, getSnapshot);
  return { ...current, reload };
}

/** Append the profile's effort once, including profiles named by the upgrade. */
export function profileName(profile: { name: string; effort: string | null }) {
  const suffix = profile.effort ? ` · ${profile.effort}` : "";
  return suffix && !profile.name.endsWith(suffix) ? `${profile.name}${suffix}` : profile.name;
}

/** The model's derived provider and exact protocol disambiguate same-name models. */
export function profileLabel(profile: AgentProfile) {
  return `${profileName(profile)} — ${profile.runtime_name} · ${profile.provider.name} / ${profile.model.name} / ${profile.model.protocol}`;
}
