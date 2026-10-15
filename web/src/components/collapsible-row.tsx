import { useState } from "react";
import { ChevronDown } from "lucide-react";
import { Badge } from "@/components/ui/badge";
import type { RowState } from "@/lib/use-collapsible-rows";
import { cn } from "@/lib/utils";

/** Where a row's forms say whether they hold unsaved edits. */
export interface DirtyMarks {
  layer: (dirty: boolean) => void;
  config: (dirty: boolean) => void;
}

/**
 * One row of a settings list (Settings → Roles, a project's Agent Profiles): a one-line summary that opens and
 * closes the row's settings, with "unsaved" while closed over unsaved edits. Its state comes from
 * `useCollapsibleRows`.
 */
export function CollapsibleRow({
  id,
  name,
  detail,
  summary,
  marks,
  state,
  onToggle,
  rowRef,
  children,
}: {
  /** Unique on the page: the row's button is `<id>-button`, its content `<id>-panel`. */
  id: string;
  name: string;
  detail?: string;
  summary: string;
  /** Badges shown open or closed, after "unsaved". */
  marks?: React.ReactNode;
  state: RowState | undefined;
  onToggle: () => void;
  rowRef: (element: HTMLElement | null) => void;
  children: (dirty: DirtyMarks) => React.ReactNode;
}) {
  // State setters are stable, so the forms can report through them from an effect.
  const [layerDirty, setLayerDirty] = useState(false);
  const [configDirty, setConfigDirty] = useState(false);
  const open = state === "open";
  const buttonId = `${id}-button`;
  const panelId = `${id}-panel`;

  return (
    <section ref={rowRef} className="scroll-mt-4 rounded-lg border border-border bg-card">
      <h2>
        <button
          id={buttonId}
          type="button"
          aria-expanded={open}
          aria-controls={panelId}
          onClick={onToggle}
          className="flex min-h-11 w-full items-center gap-3 rounded-lg px-5 py-2.5 text-left transition-colors hover:bg-accent/50 focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring"
        >
          <ChevronDown
            aria-hidden="true"
            className={cn(
              "h-4 w-4 shrink-0 text-muted-foreground transition-transform duration-150 motion-reduce:transition-none",
              !open && "-rotate-90",
            )}
          />
          <span className="min-w-0 flex-1 truncate">
            <span className="text-sm font-semibold">{name}</span>
            {detail && <span className="text-sm text-muted-foreground"> · {detail}</span>}
            {summary && <span className="text-xs text-muted-foreground"> · {summary}</span>}
          </span>
          {!open && (layerDirty || configDirty) && (
            <Badge variant="warning" className="shrink-0">unsaved</Badge>
          )}
          {marks}
        </button>
      </h2>
      <div
        id={panelId}
        role="region"
        aria-labelledby={buttonId}
        hidden={!open}
        className="space-y-5 border-t border-border px-5 py-4"
      >
        {state && children({ layer: setLayerDirty, config: setConfigDirty })}
      </div>
    </section>
  );
}
