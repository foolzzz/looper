import { useEffect, useRef, useState, type KeyboardEvent as ReactKey } from "react";
import { Input } from "@/components/ui/input";
import { errorText } from "@/lib/use-action";
import { taskTitle, tasksSelect, workStatusLabel, type SelectCandidate, type SelectResult } from "@/lib/work";

export const SELECT_DEBOUNCE_MS = 150;

const FOCUSABLE = 'a[href], button:not([disabled]), input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])';

export function WorkSwitcher({
  open,
  onClose,
  onOpen,
  restore,
}: {
  open: boolean;
  onClose: () => void;
  onOpen: (taskId: string) => void;
  restore: () => void;
}) {
  const [query, setQuery] = useState("");
  const [answer, setAnswer] = useState<{ query: string; result: SelectResult | null; error: string } | null>(null);
  const [index, setIndex] = useState(0);
  const inputRef = useRef<HTMLInputElement>(null);
  const panelRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!open) return;
    setQuery("");
    setAnswer(null);
    setIndex(0);
    const timer = window.setTimeout(() => inputRef.current?.focus(), 0);
    return () => window.clearTimeout(timer);
  }, [open]);

  // Escape is caught before the sidebar drawer, the same way the project dialog does, so it closes this layer only.
  useEffect(() => {
    if (!open) return;
    const onKey = (event: KeyboardEvent) => {
      if (event.key !== "Escape") return;
      event.preventDefault();
      event.stopPropagation();
      onClose();
      queueMicrotask(restore);
    };
    window.addEventListener("keydown", onKey, true);
    return () => window.removeEventListener("keydown", onKey, true);
  }, [open, onClose, restore]);

  useEffect(() => {
    if (!open) return;
    if (!query.trim()) {
      setAnswer(null);
      return;
    }
    const ctrl = new AbortController();
    const timer = window.setTimeout(() => {
      tasksSelect({ selection: query }, ctrl.signal).then((answer) => {
        if (ctrl.signal.aborted) return;
        setAnswer({ query, result: answer, error: "" });
        setIndex(0);
      }).catch((err: unknown) => {
        if (ctrl.signal.aborted) return;
        setAnswer({ query, result: null, error: errorText(err) });
      });
    }, SELECT_DEBOUNCE_MS);
    return () => {
      window.clearTimeout(timer);
      ctrl.abort();
    };
  }, [open, query]);

  if (!open) return null;
  const current = answer?.query === query ? answer : null;
  const result = current?.result;
  const error = current?.error;
  const loading = !!query.trim() && !current;
  const rows: SelectCandidate[] = result?.kind === "one" && result.task
    ? [result.task]
    : result?.kind === "candidates" ? result.candidates ?? [] : [];

  const close = () => {
    onClose();
    queueMicrotask(restore);
  };
  const choose = (taskId: string) => {
    onOpen(taskId);
    close();
  };
  const onKey = (event: ReactKey) => {
    if (event.key === "Escape") {
      event.preventDefault();
      close();
      return;
    }
    if (event.key === "ArrowDown" || event.key === "ArrowUp") {
      event.preventDefault();
      if (!rows.length) return;
      setIndex((current) => {
        const next = event.key === "ArrowDown" ? current + 1 : current - 1;
        return Math.min(Math.max(next, 0), rows.length - 1);
      });
      return;
    }
    if (event.key === "Enter" && rows[index]) {
      event.preventDefault();
      choose(rows[index].task_id);
      return;
    }
    if (event.key !== "Tab") return;
    const nodes = [...(panelRef.current?.querySelectorAll<HTMLElement>(FOCUSABLE) ?? [])];
    if (nodes.length === 0) return;
    const first = nodes[0];
    const last = nodes[nodes.length - 1];
    if (event.shiftKey && document.activeElement === first) {
      event.preventDefault();
      last.focus();
    } else if (!event.shiftKey && document.activeElement === last) {
      event.preventDefault();
      first.focus();
    }
  };

  return (
    <div className="fixed inset-0 z-50 bg-black/50 pt-[10dvh]" onMouseDown={close}>
      <div
        ref={panelRef}
        role="dialog"
        aria-modal="true"
        aria-labelledby="work-find-title"
        onMouseDown={(event) => event.stopPropagation()}
        onKeyDown={onKey}
        className="mx-auto flex max-h-[70dvh] w-full max-w-lg flex-col overflow-hidden rounded-lg border border-border bg-card"
      >
        <h2 id="work-find-title" className="px-3 pt-3 text-sm font-medium">查找任务</h2>
        <div className="p-3">
          <Input ref={inputRef} value={query} aria-label="选择任务" placeholder="查找任务"
            onChange={(event) => {
              setQuery(event.target.value);
              setAnswer(null);
            }} />
        </div>
        {loading && <p role="status" className="px-3 pb-3 text-sm text-muted-foreground">正在加载…</p>}
        {error && <p role="alert" className="px-3 pb-3 text-sm text-destructive">{error}</p>}
        {result?.kind === "none" && (
          <div className="space-y-2 px-3 pb-3 text-sm">
            <p>没有匹配的任务。</p>
            {result.hint && <p>{result.hint}</p>}
            <pre className="overflow-auto font-mono text-xs text-muted-foreground">{JSON.stringify(result.searched ?? {}, null, 2)}</pre>
          </div>
        )}
        <ul role="listbox" aria-label="匹配任务" className="min-h-0 flex-1 overflow-y-auto px-2 pb-2">
          {rows.map((row, rowIndex) => (
            <li key={row.task_id}>
              <button
                type="button"
                role="option"
                aria-selected={rowIndex === index}
                onMouseEnter={() => setIndex(rowIndex)}
                onClick={() => choose(row.task_id)}
                className={`flex w-full items-center gap-2 rounded-md px-2 py-2 text-left text-sm pointer-coarse:min-h-11 focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring ${rowIndex === index ? "bg-accent" : ""}`}
              >
                <span className="min-w-0 flex-1 truncate">{taskTitle(row.title)}</span>
                {row.project_name && <span className="text-xs text-muted-foreground">{row.project_name}</span>}
                <span className="text-xs text-muted-foreground">{workStatusLabel(row.status)}</span>
              </button>
            </li>
          ))}
        </ul>
        {!!result?.more && result.more > 0 && (
          <p className="border-t border-border px-3 py-2 text-xs text-muted-foreground">还有 {result.more} 项未显示</p>
        )}
      </div>
    </div>
  );
}
