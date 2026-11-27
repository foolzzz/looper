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

/**
 * An agent's Markdown reply: GitHub-flavored (tables, task lists, strikethrough) with the typography styles, its code
 * blocks wrapping their lines and inline code without the plugin's added backticks.
 */
export function Markdown({ children }: { children: string }) {
  return (
    <div className="prose prose-invert prose-sm max-w-none [&>*:first-child]:mt-0 [&>*:last-child]:mb-0 [&_pre]:whitespace-pre-wrap prose-code:before:content-none prose-code:after:content-none">
      <ReactMarkdown remarkPlugins={[remarkGfm]} components={COMPONENTS}>
        {children.replace(
          /(^## Process record\n\n)((?:- (?:Roles|Reviews|Verification|Escalations): .*\n?)+)/gm,
          // These code-owned lists contain agent fragments, which are literal data.
          (_, heading: string, entries: string) => heading + entries.replace(/[\\`*_~\[\]<>]/g, "\\$&"),
        )}
      </ReactMarkdown>
    </div>
  );
}
