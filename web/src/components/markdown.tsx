import ReactMarkdown, { type Components } from "react-markdown";
import remarkGfm from "remark-gfm";
import { InsideLink, RemoteLink, useInsideLink } from "@/components/remote-link";

function ImageLabel({ src, alt }: { src?: string | Blob; alt?: string }) {
  const inLink = useInsideLink();
  const label = `image: ${alt?.trim() || "image"}`;
  return inLink || typeof src !== "string" ? <span>{label}</span> : <RemoteLink href={src}>{label}</RemoteLink>;
}

// An agent's text must not make the browser fetch anything: images become links, and links name their host.
// A wide table scrolls inside its message instead of widening the page.
const COMPONENTS: Components = {
  a: ({ node: _node, href, children, ...rest }) => (
    <InsideLink><RemoteLink href={href ?? ""} {...rest}>{children}</RemoteLink></InsideLink>
  ),
  img: ImageLabel,
  table: (props) => (
    <div className="overflow-x-auto">
      <table {...props} />
    </div>
  ),
};

// Direction controls in agent text could visually reorder a link and the host label next to it.
const BIDI_CONTROLS = /[\u202a-\u202e\u2066-\u2069\u200e\u200f\u061c]/g;

// The same controls written as character references are decoded by the parser, so they go from the parsed text too.
type HastNode = { type: string; value?: string; properties?: Record<string, unknown>; children?: HastNode[] };
function stripBidi() {
  const walk = (node: HastNode) => {
    if (node.type === "text" && node.value) node.value = node.value.replace(BIDI_CONTROLS, "");
    // An image's alt text is shown as link text, so it is cleaned like the text nodes.
    for (const name of ["alt", "title"]) {
      const value = node.properties?.[name];
      if (typeof value === "string") node.properties![name] = value.replace(BIDI_CONTROLS, "");
    }
    node.children?.forEach(walk);
  };
  return walk;
}

/**
 * An agent's Markdown reply: GitHub-flavored (tables, task lists, strikethrough) with the typography styles, its code
 * blocks wrapping their lines and inline code without the plugin's added backticks.
 */
export function Markdown({ children }: { children: string }) {
  return (
    <div className="prose prose-invert prose-sm max-w-none [&>*:first-child]:mt-0 [&>*:last-child]:mb-0 [&_pre]:whitespace-pre-wrap prose-code:before:content-none prose-code:after:content-none">
      <ReactMarkdown remarkPlugins={[remarkGfm]} rehypePlugins={[stripBidi]} components={COMPONENTS}>
        {children.replace(BIDI_CONTROLS, "").replace(
          /(^## Process record\n\n)((?:- (?:Roles|Reviews|Verification|Escalations): .*\n?)+)/gm,
          // These code-owned lists contain agent fragments, which are literal data.
          (_, heading: string, entries: string) => heading + entries.replace(/[\\`*_~\[\]<>]/g, "\\$&"),
        )}
      </ReactMarkdown>
    </div>
  );
}
