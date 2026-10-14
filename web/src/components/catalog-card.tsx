import { useEffect, useId, useState } from "react";
import { ChevronDown, ChevronRight, Download, Loader2, RefreshCw } from "lucide-react";
import { CatalogSources, SkillForm } from "@/components/catalog-sources";
import { OriginTag, RoleChecks } from "@/components/install-controls";
import { McpItemActions, McpSummary, McpToolbar } from "@/components/mcp-servers";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import {
  deleteCatalogItem,
  fetchCatalog,
  importCatalog,
  installItem,
  uninstall,
  updateInstall,
  type AgentRole,
  type Catalog,
  type CatalogItem,
  type Install,
  type LibraryImport,
  type LibraryKind,
} from "@/lib/api";
import { formatBytes } from "@/lib/utils";

const GROUPS: { kind: LibraryKind; title: string }[] = [
  { kind: "plugin", title: "Plugins" },
  { kind: "skill", title: "Skills" },
  { kind: "mcp", title: "MCP servers" },
];

const errorText = (err: unknown) => (err instanceof Error ? err.message : String(err));

/** Where an item is installed, global first: "Global: worker, reviewer", "calc: planner (off)". */
function installSummary(installs: Install[]) {
  const ordered = [...installs].sort(
    (a, b) =>
      Number(a.scope !== "global") - Number(b.scope !== "global") ||
      (a.project_name ?? "").localeCompare(b.project_name ?? ""),
  );
  return ordered.map(
    (install) =>
      `${install.scope === "global" ? "Global" : (install.project_name ?? install.project_id)}: ${install.roles.join(", ")}${
        install.enabled ? "" : " (off)"
      }`,
  );
}

/** Items uploaded, taken from git or written in Settings are edited (skills) and deleted here. */
const OWN_ORIGINS = new Set(["upload", "git", "ui"]);

function ItemRow({
  item,
  roles,
  busy,
  error,
  fileMaxBytes,
  onChange,
  mask,
  onReplace,
  onRemove,
}: {
  item: CatalogItem;
  roles: AgentRole[];
  busy: boolean;
  error: string | undefined;
  fileMaxBytes: number;
  onChange: (request: () => Promise<{ item: CatalogItem }>) => void;
  mask: string;
  onReplace: (item: CatalogItem) => void;
  onRemove: () => void;
}) {
  const [editing, setEditing] = useState(false);
  const [confirming, setConfirming] = useState(false);
  const [removing, setRemoving] = useState(false);
  const [removeError, setRemoveError] = useState("");
  const global = item.installs.find((install) => install.scope === "global");
  const where = installSummary(item.installs);
  const own = OWN_ORIGINS.has(item.origin) && item.kind !== "mcp";
  const remove = async () => {
    setConfirming(false);
    setRemoving(true);
    setRemoveError("");
    try {
      await deleteCatalogItem(item.item_id);
      onRemove();
    } catch (err) {
      setRemoveError(errorText(err));
      setRemoving(false);
    }
  };
  return (
    <li className="space-y-1 border-t border-border px-5 py-2">
      <div className="flex flex-wrap items-center gap-x-2 gap-y-1">
        <span className="min-w-0 break-all font-mono text-xs" title={item.description || item.qualified_name}>
          {item.qualified_name}
        </span>
        {item.version && <span className="font-mono text-[11px] text-muted-foreground">v{item.version}</span>}
        <OriginTag item={item} />
        <span className="ml-auto text-[11px] tabular-nums text-muted-foreground">{formatBytes(item.size_bytes)}</span>
        {own && (
          <span className="flex gap-0.5">
            {item.kind === "skill" && (
              <Button type="button" variant="ghost" size="sm" disabled={busy || editing} onClick={() => setEditing(true)}
                      aria-label={`Edit ${item.qualified_name}`} className="h-6 px-2">
                Edit
              </Button>
            )}
            <Button type="button" variant="ghost" size="sm" disabled={busy || removing || confirming}
                    onClick={() => setConfirming(true)} aria-label={`Delete ${item.qualified_name}`}
                    className="h-6 px-2 text-muted-foreground hover:bg-destructive/10 hover:text-destructive">
              Delete
            </Button>
          </span>
        )}
      </div>
      <McpSummary item={item} />
      <p className="break-words text-[11px] text-muted-foreground">
        {where.length ? where.join(" · ") : "Installed nowhere"}
      </p>
      {item.origin === "project" ? (
        <p className="text-[11px] text-muted-foreground">Project skill — install it from a project&apos;s Skills tab</p>
      ) : global ? (
        <div className="flex flex-wrap items-center gap-x-3">
          <span className="text-[11px] text-muted-foreground">Global:</span>
          <RoleChecks
            roles={roles}
            value={global.roles}
            disabled={busy}
            label={`Global roles of ${item.qualified_name}`}
            lastHint="An install needs a role: uninstall it instead"
            onChange={(next) => onChange(() => updateInstall(global.install_id, { roles: next }))}
          />
          <Button
            type="button"
            variant="ghost"
            size="sm"
            disabled={busy}
            onClick={() => onChange(() => uninstall(global.install_id))}
            aria-label={`Uninstall ${item.qualified_name} globally`}
            className="ml-auto text-muted-foreground hover:bg-destructive/10 hover:text-destructive"
          >
            Uninstall
          </Button>
        </div>
      ) : (
        <Button
          type="button"
          variant="outline"
          size="sm"
          disabled={busy}
          onClick={() => onChange(() => installItem({ item_id: item.item_id, scope: "global", roles }))}
          aria-label={`Install ${item.qualified_name} globally`}
        >
          {busy && <Loader2 className="h-3.5 w-3.5 animate-spin" />}
          Install globally
        </Button>
      )}
      {confirming && (
        <div className="flex flex-wrap items-center justify-end gap-2">
          <span className="text-xs text-muted-foreground">
            Delete it from the catalog{item.installs.length ? " and every place it is installed" : ""}?
          </span>
          <Button type="button" variant="destructive" size="sm" disabled={busy} onClick={() => void remove()}>
            Delete
          </Button>
          <Button type="button" variant="ghost" size="sm" onClick={() => setConfirming(false)}>Cancel</Button>
        </div>
      )}
      {editing && (
        <div className="rounded-md border border-border p-3">
          <SkillForm
            item={item}
            contentMax={item.origin === "ui" ? 64 * 1024 : fileMaxBytes}
            onSaved={(saved) => {
              setEditing(false);
              onReplace(saved);
            }}
            onCancel={() => setEditing(false)}
          />
        </div>
      )}
      {item.kind === "mcp" && <McpItemActions item={item} mask={mask} onSaved={onReplace} onDeleted={onRemove} />}
      {(error || removeError) && <p role="alert" className="text-xs text-destructive">{error || removeError}</p>}
    </li>
  );
}

/** What the last import changed and left out. */
function ImportReport({ report }: { report: LibraryImport }) {
  const [open, setOpen] = useState(false);
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

/**
 * The capability catalog: import this machine's plugins and skills into it, and install items globally for some
 * roles. Project skills are installed from their project's Skills tab.
 */
export function CatalogCard() {
  const id = useId();
  const [catalog, setCatalog] = useState<Catalog | null>(null);
  const [filter, setFilter] = useState("");
  const [importing, setImporting] = useState(false);
  const [pending, setPending] = useState<ReadonlySet<string>>(new Set());
  const [rowErrors, setRowErrors] = useState<Record<string, string>>({});
  const [error, setError] = useState("");

  const refresh = () => fetchCatalog().then(setCatalog).catch((e) => setError(errorText(e)));
  useEffect(() => {
    void refresh();
  }, []);

  const reimport = async () => {
    setImporting(true);
    setError("");
    try {
      setCatalog(await importCatalog());
      setRowErrors({});
    } catch (e) {
      setError(errorText(e));
    } finally {
      setImporting(false);
    }
  };

  /** Put an added or changed item in the list, or take a deleted one (`item` null) out of it. */
  const replace = (itemId: string, item: CatalogItem | null) =>
    setCatalog((c) => {
      if (!c) return c;
      const kept = c.items.filter((other) => other.item_id !== itemId);
      const items = item
        ? c.items.some((other) => other.item_id === itemId)
          ? c.items.map((other) => (other.item_id === itemId ? item : other))
          : [...kept, item].sort((a, b) => a.kind.localeCompare(b.kind) || a.name.localeCompare(b.name))
        : kept;
      return { ...c, items, installed_count: items.filter((other) => other.installs.length).length };
    });

  /** Run one install change; the item it answers with replaces the row, and a refusal shows the server's reason. */
  const change = async (itemId: string, request: () => Promise<{ item: CatalogItem }>) => {
    setPending((p) => new Set(p).add(itemId));
    setRowErrors((errors) => {
      const next = { ...errors };
      delete next[itemId];
      return next;
    });
    try {
      const { item } = await request();
      replace(item.item_id, item);
    } catch (e) {
      setRowErrors((errors) => ({ ...errors, [itemId]: errorText(e) }));
    } finally {
      setPending((p) => {
        const next = new Set(p);
        next.delete(itemId);
        return next;
      });
    }
  };

  if (!catalog) {
    return error ? (
      <div role="alert" className="rounded-lg border border-destructive/50 bg-destructive/10 px-5 py-4 text-sm text-destructive">
        {error}
      </div>
    ) : (
      <div className="flex h-32 items-center justify-center">
        <Loader2 className="h-6 w-6 animate-spin text-muted-foreground" aria-label="Loading" />
      </div>
    );
  }

  const empty = catalog.items.length === 0;
  const imported =
    catalog.last_import != null || catalog.items.some((item) => item.origin === "machine" && item.kind !== "mcp");
  const needle = filter.trim().toLowerCase();
  const shown = needle
    ? catalog.items.filter((item) => item.qualified_name.toLowerCase().includes(needle))
    : catalog.items;

  return (
    <div className="rounded-lg border border-border bg-card">
      <div className="flex flex-wrap items-start justify-between gap-3 px-5 py-4">
        <div className="min-w-0 space-y-1">
          <p className="text-sm font-medium">
            {empty
              ? "Nothing in the catalog yet"
              : `${catalog.items.length} items · ${catalog.installed_count} installed · ${formatBytes(catalog.total_bytes)}`}
          </p>
          <p className="text-xs text-muted-foreground">
            An item loads only where it is installed: globally for the roles checked here, or in a project from its
            Skills tab, from each agent&apos;s next run on. Items imported before this version are installed nowhere
            until you install them.
          </p>
        </div>
        <Button type="button" size="sm" variant={imported ? "outline" : "default"} onClick={reimport} disabled={importing}>
          {importing ? (
            <Loader2 className="h-3.5 w-3.5 animate-spin" />
          ) : imported ? (
            <RefreshCw className="h-3.5 w-3.5" />
          ) : (
            <Download className="h-3.5 w-3.5" />
          )}
          {imported ? "Re-import" : "Import from this machine"}
        </Button>
      </div>
      <CatalogSources catalog={catalog} onAdded={() => void refresh()} />
      {error && <p role="alert" className="border-t border-border px-5 py-2 text-xs text-destructive">{error}</p>}
      {catalog.last_import && <ImportReport report={catalog.last_import} />}
      <McpToolbar catalog={catalog} onCatalog={setCatalog} onAdded={(item) => replace(item.item_id, item)} />
      {!empty && (
        <>
          <div className="border-t border-border px-5 py-2">
            <Input
              id={`${id}-filter`}
              aria-label="Filter plugins, skills and MCP servers"
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
                  </h3>
                  <ul>
                    {items.map((item) => (
                      <ItemRow
                        key={item.item_id}
                        item={item}
                        roles={catalog.roles}
                        busy={pending.has(item.item_id)}
                        error={rowErrors[item.item_id]}
                        fileMaxBytes={catalog.caps.file_max_bytes}
                        onChange={(request) => void change(item.item_id, request)}
                        mask={catalog.mcp_caps.mask}
                        onReplace={(next) => replace(item.item_id, next)}
                        onRemove={() => replace(item.item_id, null)}
                      />
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
