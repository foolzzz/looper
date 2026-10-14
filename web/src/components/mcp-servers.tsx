import { useId, useState } from "react";
import { Download, Loader2, Pencil, Plus, Trash2, X } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import {
  createMcpServer,
  deleteCatalogItem,
  importMcpServers,
  updateMcpServer,
  type Catalog,
  type CatalogItem,
  type McpImport,
  type McpTransport,
} from "@/lib/api";
import { useSettingsTarget } from "@/lib/settings-target";

const SELECT = "flex h-9 w-full rounded-md border border-input bg-transparent px-3 py-1 text-sm text-foreground";
const TEXTAREA =
  "w-full resize-y rounded-md border border-input bg-transparent px-3 py-2 font-mono text-xs leading-relaxed text-foreground shadow-sm focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring";

const errorText = (err: unknown) => (err instanceof Error ? err.message : String(err));

type Pair = { name: string; value: string };

interface Draft {
  name: string;
  description: string;
  transport: McpTransport;
  command: string;
  /** One argument per line. */
  args: string;
  env: Pair[];
  url: string;
  headers: Pair[];
}

const pairs = (values: Record<string, string>): Pair[] =>
  Object.entries(values).map(([name, value]) => ({ name, value }));

function draftOf(item: CatalogItem | null): Draft {
  const mcp = item?.mcp;
  return {
    name: item?.name ?? "",
    description: item?.description ?? "",
    transport: mcp?.transport ?? "stdio",
    command: mcp?.command ?? "",
    args: (mcp?.args ?? []).join("\n"),
    env: pairs(mcp?.env ?? {}),
    url: mcp?.url ?? "",
    headers: pairs(mcp?.headers ?? {}),
  };
}

function body(draft: Draft) {
  const values = (rows: Pair[]) => Object.fromEntries(rows.filter((row) => row.name.trim()).map((row) => [row.name.trim(), row.value]));
  const common = { name: draft.name.trim(), description: draft.description.trim(), transport: draft.transport };
  return draft.transport === "stdio"
    ? { ...common, command: draft.command, args: draft.args.split("\n").filter((arg) => arg !== ""), env: values(draft.env) }
    : { ...common, url: draft.url.trim(), headers: values(draft.headers) };
}

/** One line on how Claude Code starts or reaches the server, with the names (never the values) of its env or headers. */
export function McpSummary({ item }: { item: CatalogItem }) {
  const mcp = item.mcp;
  if (!mcp) return null;
  const target = mcp.transport === "stdio" ? [mcp.command, ...mcp.args].join(" ") : mcp.url;
  const names = Object.keys(mcp.transport === "stdio" ? mcp.env : mcp.headers);
  return (
    <p className="break-all font-mono text-[11px] text-muted-foreground">
      {mcp.transport} · {target}
      {names.length > 0 && ` · ${mcp.transport === "stdio" ? "env" : "headers"} ${names.join(", ")}`}
    </p>
  );
}

/** Name/value rows for env or headers; a stored value shows as the mask, which keeps it when saved unchanged. */
function PairRows({
  label,
  rows,
  mask,
  namePlaceholder,
  onChange,
}: {
  label: string;
  rows: Pair[];
  mask: string;
  namePlaceholder: string;
  onChange: (rows: Pair[]) => void;
}) {
  const set = (index: number, change: Partial<Pair>) =>
    onChange(rows.map((row, i) => (i === index ? { ...row, ...change } : row)));
  return (
    <fieldset className="space-y-1">
      <legend className="text-xs font-medium text-muted-foreground">{label}</legend>
      {rows.map((row, index) => (
        <div key={index} className="flex items-center gap-2">
          <Input
            aria-label={`${label} name ${index + 1}`}
            value={row.name}
            onChange={(e) => set(index, { name: e.target.value })}
            placeholder={namePlaceholder}
            spellCheck={false}
            autoCapitalize="none"
            className="h-8 font-mono text-xs"
          />
          <Input
            aria-label={`${label} value ${index + 1}`}
            type="password"
            autoComplete="off"
            value={row.value}
            onChange={(e) => set(index, { value: e.target.value })}
            onFocus={(e) => row.value === mask && e.target.select()}
            placeholder="value"
            className="h-8 font-mono text-xs"
          />
          <Button
            type="button"
            variant="ghost"
            size="icon"
            aria-label={`Remove ${label} ${row.name || index + 1}`}
            onClick={() => onChange(rows.filter((_, i) => i !== index))}
            className="h-8 w-8 shrink-0 text-muted-foreground"
          >
            <X className="h-3.5 w-3.5" />
          </Button>
        </div>
      ))}
      <Button type="button" variant="ghost" size="sm" onClick={() => onChange([...rows, { name: "", value: "" }])}>
        <Plus className="h-3.5 w-3.5" /> Add
      </Button>
    </fieldset>
  );
}

/** Add an MCP server, or edit one (`item`). Values are secrets: stored ones show as the mask. */
export function McpForm({
  item,
  mask,
  onSaved,
  onCancel,
}: {
  item: CatalogItem | null;
  mask: string;
  onSaved: (item: CatalogItem) => void;
  onCancel: () => void;
}) {
  const id = useId();
  const [draft, setDraft] = useState<Draft>(() => draftOf(item));
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const set = (change: Partial<Draft>) => setDraft((d) => ({ ...d, ...change }));

  const save = async (e: React.FormEvent) => {
    e.preventDefault();
    setBusy(true);
    setError("");
    try {
      const saved = item ? await updateMcpServer(item.item_id, body(draft)) : await createMcpServer(body(draft));
      onSaved(saved.item);
    } catch (err) {
      setError(errorText(err));
    } finally {
      setBusy(false);
    }
  };

  return (
    <form onSubmit={save} aria-label={item ? `Edit MCP server ${item.name}` : "Add an MCP server"} className="space-y-3 border-t border-border bg-muted/20 px-5 py-3">
      <div className="grid gap-2 sm:grid-cols-[minmax(0,1fr)_minmax(0,2fr)_8rem]">
        <div className="space-y-1">
          <label htmlFor={`${id}-name`} className="text-xs font-medium text-muted-foreground">Name *</label>
          <Input id={`${id}-name`} value={draft.name} maxLength={64} onChange={(e) => set({ name: e.target.value })} placeholder="search" spellCheck={false} autoCapitalize="none" className="h-8 font-mono text-xs" autoFocus />
        </div>
        <div className="space-y-1">
          <label htmlFor={`${id}-description`} className="text-xs font-medium text-muted-foreground">Description</label>
          <Input id={`${id}-description`} value={draft.description} maxLength={1024} onChange={(e) => set({ description: e.target.value })} className="h-8 text-xs" />
        </div>
        <div className="space-y-1">
          <label htmlFor={`${id}-transport`} className="text-xs font-medium text-muted-foreground">Transport</label>
          <select id={`${id}-transport`} value={draft.transport} onChange={(e) => set({ transport: e.target.value as McpTransport })} className={`${SELECT} h-8 text-xs`}>
            <option value="stdio">stdio</option>
            <option value="http">http</option>
            <option value="sse">sse</option>
          </select>
        </div>
      </div>
      {draft.transport === "stdio" ? (
        <>
          <div className="grid gap-2 sm:grid-cols-2">
            <div className="space-y-1">
              <label htmlFor={`${id}-command`} className="text-xs font-medium text-muted-foreground">Command *</label>
              <Input id={`${id}-command`} value={draft.command} onChange={(e) => set({ command: e.target.value })} placeholder="npx" spellCheck={false} autoCapitalize="none" className="h-8 font-mono text-xs" />
            </div>
            <div className="space-y-1">
              <label htmlFor={`${id}-args`} className="text-xs font-medium text-muted-foreground">Arguments, one per line</label>
              <textarea id={`${id}-args`} value={draft.args} onChange={(e) => set({ args: e.target.value })} rows={2} spellCheck={false} className={TEXTAREA} />
            </div>
          </div>
          <PairRows label="Environment" rows={draft.env} mask={mask} namePlaceholder="API_KEY" onChange={(env) => set({ env })} />
        </>
      ) : (
        <>
          <div className="space-y-1">
            <label htmlFor={`${id}-url`} className="text-xs font-medium text-muted-foreground">URL *</label>
            <Input id={`${id}-url`} value={draft.url} onChange={(e) => set({ url: e.target.value })} placeholder="https://example.com/mcp" spellCheck={false} autoCapitalize="none" className="h-8 font-mono text-xs" />
          </div>
          <PairRows label="Headers" rows={draft.headers} mask={mask} namePlaceholder="Authorization" onChange={(headers) => set({ headers })} />
        </>
      )}
      <p className="text-[11px] text-muted-foreground">
        Values are stored in the database and never shown again; a stored one reads {mask} and stays unless you replace it.
      </p>
      {error && <p role="alert" className="text-xs text-destructive">{error}</p>}
      <div className="flex gap-2">
        <Button type="submit" size="sm" disabled={busy}>
          {busy && <Loader2 className="h-3.5 w-3.5 animate-spin" />}
          {item ? "Save" : "Add server"}
        </Button>
        <Button type="button" size="sm" variant="ghost" onClick={onCancel} disabled={busy}>
          Cancel
        </Button>
      </div>
    </form>
  );
}

/** Edit or delete one MCP server, below its catalog row. */
export function McpItemActions({
  item,
  mask,
  onSaved,
  onDeleted,
}: {
  item: CatalogItem;
  mask: string;
  onSaved: (item: CatalogItem) => void;
  onDeleted: () => void;
}) {
  const [editing, setEditing] = useState(false);
  // An import's "secrets to fill in" links here (#settings/mcp/<id>) to open the form.
  const ref = useSettingsTarget<HTMLDivElement>("mcp", item.item_id, () => setEditing(true));
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const remove = async () => {
    if (!confirm(`Delete MCP server "${item.name}"? Its installs go too.`)) return;
    setBusy(true);
    setError("");
    try {
      await deleteCatalogItem(item.item_id);
      onDeleted();
    } catch (err) {
      setError(errorText(err));
      setBusy(false);
    }
  };
  return (
    <>
      <div ref={ref} className="flex gap-1">
        <Button type="button" variant="ghost" size="sm" onClick={() => setEditing(!editing)} aria-expanded={editing} aria-label={`Edit MCP server ${item.name}`}>
          <Pencil className="h-3.5 w-3.5" /> Edit
        </Button>
        <Button type="button" variant="ghost" size="sm" onClick={remove} disabled={busy} aria-label={`Delete MCP server ${item.name}`} className="text-muted-foreground hover:bg-destructive/10 hover:text-destructive">
          <Trash2 className="h-3.5 w-3.5" /> Delete
        </Button>
      </div>
      {error && <p role="alert" className="text-xs text-destructive">{error}</p>}
      {editing && (
        <div className="-mx-5">
          <McpForm
            item={item}
            mask={mask}
            onSaved={(saved) => {
              setEditing(false);
              onSaved(saved);
            }}
            onCancel={() => setEditing(false)}
          />
        </div>
      )}
    </>
  );
}

function importLine(report: McpImport) {
  const changes = [
    report.added.length && `${report.added.length} added`,
    report.updated.length && `${report.updated.length} updated`,
    report.removed.length && `${report.removed.length} removed`,
    `${report.unchanged} unchanged`,
  ].filter(Boolean);
  return `Last MCP import ${new Date(report.imported_at).toLocaleString()} · ${report.servers} servers · ${changes.join(", ")}`;
}

/** Add an MCP server or import this machine's (~/.claude.json); the catalog lists them with the other items. */
export function McpToolbar({ catalog, onCatalog, onAdded }: { catalog: Catalog; onCatalog: (catalog: Catalog) => void; onAdded: (item: CatalogItem) => void }) {
  const [adding, setAdding] = useState(false);
  const [importing, setImporting] = useState(false);
  const [error, setError] = useState("");
  const report = catalog.last_mcp_import;
  const run = async () => {
    setImporting(true);
    setError("");
    try {
      onCatalog(await importMcpServers());
    } catch (err) {
      setError(errorText(err));
    } finally {
      setImporting(false);
    }
  };
  return (
    <div className="border-t border-border">
      <div className="flex flex-wrap items-center gap-2 px-5 py-2">
        <span className="text-xs font-medium">MCP servers</span>
        <span className="text-[11px] text-muted-foreground">install them like any item; each run gets the ones installed for its role</span>
        <div className="ml-auto flex gap-2">
          <Button type="button" size="sm" variant="outline" onClick={() => setAdding(!adding)} aria-expanded={adding}>
            <Plus className="h-3.5 w-3.5" /> Add MCP server
          </Button>
          <Button type="button" size="sm" variant="outline" onClick={run} disabled={importing}>
            {importing ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <Download className="h-3.5 w-3.5" />}
            Import from ~/.claude.json
          </Button>
        </div>
      </div>
      {report && (
        <div className="px-5 pb-2 text-[11px] text-muted-foreground">
          <p>{importLine(report)}</p>
          {report.skipped_count > 0 && (
            <ul className="mt-0.5 font-mono text-warning">
              {report.skipped.map((s, i) => (
                <li key={i} className="break-all">
                  {s.item}
                  {s.path && `.${s.path}`} — {s.reason}
                </li>
              ))}
            </ul>
          )}
        </div>
      )}
      {error && <p role="alert" className="px-5 pb-2 text-xs text-destructive">{error}</p>}
      {adding && (
        <McpForm
          item={null}
          mask={catalog.mcp_caps.mask}
          onSaved={(item) => {
            setAdding(false);
            onAdded(item);
          }}
          onCancel={() => setAdding(false)}
        />
      )}
    </div>
  );
}
