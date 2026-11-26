import { createContext, useContext, type ReactNode } from "react";

const LinkContext = createContext(false);

/** Marks its children as sitting inside a link, so an image label there is not a second link. */
export function InsideLink({ children }: { children: ReactNode }) {
  return <LinkContext.Provider value>{children}</LinkContext.Provider>;
}

export function useInsideLink(): boolean {
  return useContext(LinkContext);
}

type Target = { kind: "internal" } | { kind: "external"; host: string; url: URL } | { kind: "blocked" };

/**
 * Where a link really goes, resolved the way the browser resolves it (so `https:host`, `//host`, a tab inside the
 * scheme and the like cannot hide the destination): this origin is internal, another http(s) origin is external
 * with its resolved host, and any other scheme is blocked.
 */
export function linkTarget(href: string | undefined): Target {
  if (!href) return { kind: "blocked" };
  const here = globalThis.location?.href || "http://localhost/";
  try {
    const url = new URL(href, here);
    if (!/^https?:$/.test(url.protocol)) return { kind: "blocked" };
    return url.origin === new URL(here).origin ? { kind: "internal" } : { kind: "external", host: url.host, url };
  } catch {
    return { kind: "blocked" };
  }
}

/** The host of an external web address, or null. */
export function remoteHost(href: string | undefined): string | null {
  const target = linkTarget(href);
  return target.kind === "external" ? target.host : null;
}

// The label is dropped only when the text is itself an address on that host without credentials.
function namesTheHost(text: ReactNode, host: string): boolean {
  if (typeof text !== "string") return false;
  try {
    const url = new URL(text.trim());
    return /^https?:$/.test(url.protocol) && !url.username && !url.password && url.host === host;
  } catch {
    return false;
  }
}

/**
 * A link to another site: it shows the real host, opens in a new tab and sends no referrer. An address that is
 * neither this site nor http(s) renders as plain text.
 */
export function RemoteLink({ href, className, children }: { href: string; className?: string; children: ReactNode }) {
  const target = linkTarget(href);
  if (target.kind === "blocked") return <span>{children}</span>;
  if (target.kind === "internal") return <a href={href} className={className}>{children}</a>;
  return (
    <a href={href} target="_blank" rel="noopener noreferrer" referrerPolicy="no-referrer" className={className}>
      {children}
      {!namesTheHost(children, target.host) && <span className="text-muted-foreground"> ({target.host})</span>}
    </a>
  );
}
