import { useEffect, useRef, useState } from "react";

/** What a link into Settings can open: a provider's or an MCP server's edit form, or a channel's card. */
export type SettingsTargetKind = "provider" | "mcp" | "channel";

/** The link to one of them: #settings/<kind>/<id>. */
export function settingsTargetHash(kind: SettingsTargetKind, id: string): string {
  return `#settings/${kind}/${encodeURIComponent(id)}`;
}

function linked(): string {
  try {
    return decodeURIComponent(window.location.hash.replace(/^#\/?/, ""));
  } catch {
    return "";
  }
}

/**
 * Make a Settings row a link target: when the page's link names it, on mount or when the link changes to it,
 * `onTarget` runs (to open its form) and the element `ref` holds scrolls into view.
 */
export function useSettingsTarget<T extends HTMLElement>(kind: SettingsTargetKind, id: string, onTarget?: () => void) {
  const ref = useRef<T>(null);
  const [hash, setHash] = useState(linked);
  const latest = useRef(onTarget);
  useEffect(() => {
    latest.current = onTarget;
  });

  useEffect(() => {
    const update = () => setHash(linked());
    window.addEventListener("hashchange", update);
    return () => window.removeEventListener("hashchange", update);
  }, []);

  const targeted = hash === `settings/${kind}/${id}`;
  useEffect(() => {
    if (!targeted) return;
    latest.current?.();
    const reduce = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
    ref.current?.scrollIntoView({ block: "center", behavior: reduce ? "auto" : "smooth" });
  }, [targeted]);

  return ref;
}
