import { useEffect, useId, useState } from "react";
import { ChevronDown, ChevronRight, Download, Loader2, RefreshCw } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Switch } from "@/components/ui/switch";
import {
  fetchLibrary,
  importLibrary,
  setLibraryItemEnabled,
  type Library,
  type LibraryItem,
  type LibraryKind,
} from "@/lib/api";

function formatBytes(bytes: number) {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(bytes < 10 * 1024 ? 1 : 0)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}

const GROUPS: { kind: LibraryKind; title: string }[] = [
  { kind: "plugin", title: "Plugins" },
  { kind: "skill", title: "Skills" },
];

function ItemRow({ item, busy, onToggle }: { item: LibraryItem; busy: boolean; onToggle: (on: boolean) => void }) {
  return (
    <li className="flex items-center gap-3 border-t border-border px-5 py-2">
      <span className="min-w-0 flex-1 truncate font-mono text-xs" title={item.name}>{item.name}</span>
      {item.version && <span className="font-mono text-[11px] text-muted-foreground">v{item.version}</span>}
      <span className="w-16 text-right text-[11px] tabular-nums text-muted-foreground">{formatBytes(item.size_bytes)}</span>
      <Switch checked={item.enabled} disabled={busy} onChange={onToggle} label={`Load ${item.name}`} />
    </li>
  );
}

/** What the last import changed and left out. */
function ImportReport({ library }: { library: Library }) {
  const [open, setOpen] = useState(false);
  const report = library.last_import;
  if (!report) return null;
  const changes = [
    report.added.length && `${report.added.length} added`,
    report.updated.length && `${report.updated.length} updated`,
    report.removed.length && `${report.removed.length} removed`,
    `${report.unchanged} unchanged`,
  ].filter(Boolean);
  return (
    <div className="border-t border-border px-5 py-2 text-xs text-muted-foreground">
      <p>
        Last import {new Date(report.imported_at).toLocaleString()} · {report.plugins} plugins, {report.skills} skills ·{" "}
        {changes.join(", ")}
      </p>
      {report.skipped_count > 0 && (
        <div className="mt-1">
          <button
            type="button"
            onClick={() => setOpen(!open)}
            aria-expanded={open}
            className="flex items-center gap-1 text-warning hover:underline"
          >
            {open ? <ChevronDown className="h-3 w-3" /> : <ChevronRight className="h-3 w-3" />}
            {report.skipped_count} left out
          </button>
          {open && (
            <ul className="mt-1 space-y-0.5 font-mono text-[11px]">
              {report.skipped.map((s, i) => (
                <li key={i} className="break-all">
                  {s.item}
                  {s.path && `/${s.path}`} — {s.reason}
                  {s.size_bytes != null && ` (${formatBytes(s.size_bytes)})`}
                </li>
              ))}
              {report.skipped_count > report.skipped.length && (
                <li>… and {report.skipped_count - report.skipped.length} more</li>
              )}
            </ul>
          )}
        </div>
      )}
    </div>
  );
}

/** The imported plugins and skills every agent loads: import from this machine, then enable or disable each. */
export function LibraryCard() {
  const id = useId();
  const [library, setLibrary] = useState<Library | null>(null);
  const [filter, setFilter] = useState("");
  const [importing, setImporting] = useState(false);
  const [pending, setPending] = useState<string | null>(null);
  const [error, setError] = useState("");

  useEffect(() => {
    fetchLibrary().then(setLibrary).catch((e) => setError(e.message));
  }, []);

  const run = async (request: () => Promise<Library>) => {
    setError("");
    try {
      setLibrary(await request());
    } catch (e) {
      setError((e as Error).message);
    }
  };

  const reimport = async () => {
    setImporting(true);
    await run(importLibrary);
    setImporting(false);
  };

  const toggle = async (item: LibraryItem, enabled: boolean) => {
    setPending(item.id);
    await run(() => setLibraryItemEnabled(item.id, enabled));
    setPending(null);
  };

  if (!library) {
    return error ? (
      <div role="alert" className="rounded-lg border border-destructive/50 bg-destructive/10 px-5 py-4 text-sm text-destructive">
        {error}
      </div>
    ) : (
      <div className="flex h-32 items-center justify-center">
        <Loader2 className="h-6 w-6 animate-spin text-muted-foreground" />
      </div>
    );
  }

  const empty = library.items.length === 0;
  const needle = filter.trim().toLowerCase();
  const shown = needle ? library.items.filter((item) => item.name.toLowerCase().includes(needle)) : library.items;

  return (
    <div className="rounded-lg border border-border bg-card">
      <div className="flex flex-wrap items-start justify-between gap-3 px-5 py-4">
        <div className="min-w-0 space-y-1">
          <p className="text-sm font-medium">
            {empty
              ? "Nothing imported yet"
              : `${library.enabled_count} of ${library.items.length} loaded · ${formatBytes(library.total_bytes)}`}
          </p>
          <p className="text-xs text-muted-foreground">
            Loaded into every agent (Manager, Planner, Workers, Reviewers) on its next run.
          </p>
        </div>
        <Button type="button" size="sm" variant={empty ? "default" : "outline"} onClick={reimport} disabled={importing}>
          {importing ? (
            <Loader2 className="h-3.5 w-3.5 animate-spin" />
          ) : empty ? (
            <Download className="h-3.5 w-3.5" />
          ) : (
            <RefreshCw className="h-3.5 w-3.5" />
          )}
          {empty ? "Import from this machine" : "Re-import"}
        </Button>
      </div>
      {error && <p role="alert" className="border-t border-border px-5 py-2 text-xs text-destructive">{error}</p>}
      <ImportReport library={library} />
      {!empty && (
        <>
          <div className="border-t border-border px-5 py-2">
            <Input
              id={`${id}-filter`}
              aria-label="Filter plugins and skills"
              placeholder="Filter by name"
              value={filter}
              onChange={(e) => setFilter(e.target.value)}
              className="h-8 font-mono text-xs"
            />
          </div>
          <div className="max-h-[28rem] overflow-y-auto overscroll-contain">
            {GROUPS.map(({ kind, title }) => {
              const items = shown.filter((item) => item.kind === kind);
              if (!items.length) return null;
              return (
                <section key={kind} aria-label={title}>
                  <h3 className="sticky top-0 z-10 border-t border-border bg-card px-5 py-1.5 text-[11px] font-medium text-muted-foreground">
                    {title} · {items.length}
                    {kind === "skill" && (
                      <span className="font-normal"> · named {library.skills_plugin}:&lt;skill&gt;</span>
                    )}
                  </h3>
                  <ul>
                    {items.map((item) => (
                      <ItemRow key={item.id} item={item} busy={pending === item.id} onToggle={(on) => toggle(item, on)} />
                    ))}
                  </ul>
                </section>
              );
            })}
            {!shown.length && <p className="border-t border-border px-5 py-3 text-xs text-muted-foreground">No match.</p>}
          </div>
        </>
      )}
    </div>
  );
}
