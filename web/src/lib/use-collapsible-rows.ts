import { useEffect, useRef, useState } from "react";

/** A row that has been opened: its content mounts the first time it opens and stays mounted, hidden while the row
 * is closed, so closing a row keeps its unsaved edits. Undefined until then. */
export type RowState = "open" | "closed";

/**
 * Which rows of a list of `CollapsibleRow`s are open. Every row starts closed and any number can be open; `linked`
 * (the row an address names) opens, also while the list is showing, and is scrolled into view once `ready`.
 * Returns each row's state, toggle and element ref by id.
 */
export function useCollapsibleRows(linked: string, ready: boolean) {
  const [rows, setRows] = useState<Record<string, RowState>>(() => (linked ? { [linked]: "open" } : {}));
  const [linkedRow, setLinkedRow] = useState(linked);
  const elements = useRef<Record<string, HTMLElement | null>>({});

  if (linked !== linkedRow) {
    setLinkedRow(linked);
    if (linked) setRows((current) => ({ ...current, [linked]: "open" }));
  }

  useEffect(() => {
    if (!linked || !ready) return;
    const reduce = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
    elements.current[linked]?.scrollIntoView({ block: "start", behavior: reduce ? "auto" : "smooth" });
  }, [linked, ready]);

  return (id: string) => ({
    state: rows[id],
    onToggle: () => setRows((current) => ({ ...current, [id]: current[id] === "open" ? "closed" : "open" })),
    rowRef: (element: HTMLElement | null) => {
      elements.current[id] = element;
    },
  });
}
