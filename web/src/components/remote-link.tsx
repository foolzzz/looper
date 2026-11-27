import { createContext, useContext, type AnchorHTMLAttributes, type ReactNode } from "react";

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

function absoluteWebAddress(text: ReactNode): URL | null {
  if (typeof text !== "string") return null;
  try {
    const url = new URL(text.trim());
    return /^https?:$/.test(url.protocol) ? url : null;
  } catch {
    return null;
  }
}

// The label is dropped only when the text is an address with the very same origin as the destination (scheme, host and
// port), neither side carries credentials, and the ASCII host appears literally in the text (a Unicode, full-width or
// encoded spelling parses to the same host but reads differently).
function namesTheDestination(text: ReactNode, destination: URL): boolean {
  const shown = absoluteWebAddress(text);
  return shown !== null && typeof text === "string" && shown.origin === destination.origin
    && !shown.username && !shown.password && !destination.username && !destination.password
    && text.toLowerCase().includes(destination.host);
}

/**
 * A link to another site: it shows the real host, opens in a new tab and sends no referrer. An address that is
 * neither this site nor http(s) renders as plain text.
 */
export function RemoteLink({ href, className, children, ...extra }: { href: string; className?: string; children: ReactNode }
  & Omit<AnchorHTMLAttributes<HTMLAnchorElement>, "href" | "target" | "rel" | "referrerPolicy" | "className" | "children">) {
  const target = linkTarget(href);
  if (target.kind === "blocked") return <span>{children}</span>;
  if (target.kind === "internal") {
    // Text that reads as another site's address must not pass for a link that stays here.
    const shown = absoluteWebAddress(children);
    const elsewhere = shown !== null && shown.origin !== new URL(globalThis.location?.href || "http://localhost/").origin;
    return (
      <a href={href} className={className} {...extra}>
        {children}
        {elsewhere && <span className="text-muted-foreground"> (this site)</span>}
      </a>
    ); // footnote ids, aria and title stay
  }
  return (
    <a href={href} title={extra.title} target="_blank" rel="noopener noreferrer" referrerPolicy="no-referrer" className={className}>
      {children}
      {!namesTheDestination(children, target.url) && <span className="text-muted-foreground"> ({target.host})</span>}
    </a>
  );
}
