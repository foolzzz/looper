import ReactMarkdown, { type Components } from "react-markdown";
import remarkGfm from "remark-gfm";

// A wide table scrolls inside its message instead of widening the page.
const COMPONENTS: Components = {
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
        {children}
      </ReactMarkdown>
    </div>
  );
}
