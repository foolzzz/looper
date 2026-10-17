import { useEffect } from "react";
import { Panel, useReactFlow } from "@xyflow/react";
import { Maximize2, Minimize2 } from "lucide-react";

/** The Pipeline graph's own controls (a child of `ReactFlow`), at its bottom right, clear of its zoom controls:
 * whether the steps a replan dropped are drawn, with how many are left out, and full screen; full screen, the way
 * out at its top right. The graph fits its view again when it enters or leaves full screen, or the dropped steps
 * come or go. */
export function PipelineToolbar({
  skipped,
  hideSkipped,
  onHideSkipped,
  full,
  onToggleFull,
}: {
  /** How many steps a replan dropped; without any, the toggle is left out. */
  skipped: number;
  hideSkipped: boolean;
  onHideSkipped: (hide: boolean) => void;
  full: boolean;
  onToggleFull: () => void;
}) {
  const { fitView } = useReactFlow();
  useEffect(() => {
    // After the new size or nodes are measured.
    let next = 0;
    const first = requestAnimationFrame(() => {
      next = requestAnimationFrame(() => void fitView({ padding: 0.12 }));
    });
    return () => {
      cancelAnimationFrame(first);
      cancelAnimationFrame(next);
    };
  }, [full, hideSkipped, fitView]);

  const button =
    "flex h-9 items-center gap-1.5 rounded-md px-2 text-muted-foreground pointer-coarse:h-11 pointer-coarse:min-w-11 hover:bg-accent hover:text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring";
  // Over a node's card (z-10), so the controls and the way out of full screen are never covered.
  const panel = "flex items-center rounded-md border border-border bg-card text-xs";
  return (
    <>
      {(skipped > 0 || !full) && (
        <Panel position="bottom-right" style={{ zIndex: 20 }}
               className={`${panel} max-w-[calc(100%-5rem)] flex-wrap justify-end gap-x-3 px-2 py-1`}>
          {skipped > 0 && (
            <span className="flex flex-wrap items-center gap-x-2">
              <label className="flex min-h-9 cursor-pointer items-center gap-1.5 pointer-coarse:min-h-11">
                <input
                  type="checkbox"
                  checked={hideSkipped}
                  onChange={(event) => onHideSkipped(event.target.checked)}
                  className="h-4 w-4 accent-primary"
                />
                Hide skipped steps
              </label>
              {hideSkipped && (
                <span className="text-muted-foreground" aria-live="polite">
                  {skipped} skipped hidden
                </span>
              )}
            </span>
          )}
          {!full && (
            <button type="button" onClick={onToggleFull} aria-label="Show the graph full screen" className={button}>
              <Maximize2 className="h-4 w-4" aria-hidden="true" />
            </button>
          )}
        </Panel>
      )}
      {full && (
        <Panel position="top-right" style={{ zIndex: 20 }} className={`${panel} p-1`}>
          <button type="button" onClick={onToggleFull} className={button}>
            <Minimize2 className="h-4 w-4" aria-hidden="true" />
            Exit full screen
          </button>
        </Panel>
      )}
    </>
  );
}
