import type { ReactNode } from "react";

const REMOTE = /^(?:https?:)?\/\//i;

/** The host of an absolute or protocol-relative web address, or null for a task link, anchor or other scheme. */
export function remoteHost(href: string | undefined): string | null {
  if (!href || !REMOTE.test(href)) return null;
  try {
    return new URL(href, "https://placeholder.invalid").host || null;
  } catch {
    return null;
  }
}

export function RemoteLink({ href, host, children }: { href: string; host: string; children: ReactNode }) {
  // Display text can name another site, so the real host always shows unless the text is the address itself.
  const same = typeof children === "string" && (children === href || children.replace(/^https?:\/\//i, "").replace(/\/$/, "") === host);
  return (
    <a href={href} target="_blank" rel="noopener noreferrer" referrerPolicy="no-referrer">
      {children}
      {!same && <span className="text-muted-foreground"> ({host})</span>}
    </a>
  );
}
