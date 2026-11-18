import { useEffect, useRef } from "react";
import { cn } from "@/lib/utils";

/** A page's tab list, kept to one row: when the tabs do not fit, it scrolls sideways and the selected tab is scrolled
 * into view. Below lg it runs edge to edge through the page's side padding (`--gutter`, which the caller sets to that
 * padding), and the edges fade there, so a tab cut off at an edge shows that more follow; at rest the first tab lines
 * up with the page. */
export function TabStrip({
  label,
  selected,
  className,
  children,
}: {
  label: string;
  /** The selected tab's id: when it changes, that tab is scrolled into view. */
  selected: string;
  className?: string;
  children: React.ReactNode;
}) {
  const strip = useRef<HTMLDivElement>(null);
  useEffect(() => {
    // Only a strip that scrolls can hide the selected tab; "nearest" leaves a tab already in view where it is.
    const bar = strip.current;
    if (!bar || bar.scrollWidth <= bar.clientWidth) return;
    bar
      .querySelector<HTMLElement>('[role="tab"][aria-selected="true"]')
      ?.scrollIntoView?.({ block: "nearest", inline: "nearest" });
  }, [selected]);
  return (
    <div
      ref={strip}
      role="tablist"
      aria-label={label}
      className={cn(
        "flex overflow-x-auto",
        "max-lg:-mx-(--gutter) max-lg:scroll-px-(--gutter) max-lg:px-(--gutter)",
        "max-lg:[mask-image:linear-gradient(to_right,transparent,#000_var(--gutter),#000_calc(100%-var(--gutter)),transparent)]",
        className,
      )}
    >
      {children}
    </div>
  );
}
