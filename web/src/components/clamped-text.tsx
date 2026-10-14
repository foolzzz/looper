import { useLayoutEffect, useRef, useState } from "react";
import { cn } from "@/lib/utils";

/** Text cut to three lines, with a toggle when it is longer. */
export function ClampedText({ text, className }: { text: string; className?: string }) {
  const ref = useRef<HTMLParagraphElement>(null);
  const [expanded, setExpanded] = useState(false);
  const [overflows, setOverflows] = useState(false);

  useLayoutEffect(() => {
    const el = ref.current;
    if (!el || expanded) return;
    const measure = () => setOverflows(el.scrollHeight > el.clientHeight + 1);
    measure();
    const observer = new ResizeObserver(measure);
    observer.observe(el);
    return () => observer.disconnect();
  }, [text, expanded]);

  return (
    <div>
      <p ref={ref} className={cn("whitespace-pre-wrap break-words", !expanded && "line-clamp-3", className)}>
        {text}
      </p>
      {(overflows || expanded) && (
        <button
          type="button"
          onClick={() => setExpanded(!expanded)}
          aria-expanded={expanded}
          className="mt-1 text-xs font-medium text-muted-foreground underline-offset-2 hover:text-foreground hover:underline"
        >
          {expanded ? "Show less" : "Show more"}
        </button>
      )}
    </div>
  );
}
