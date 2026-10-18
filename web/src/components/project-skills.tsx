import { useCallback, useEffect, useId, useRef, useState } from "react";
import { ChevronRight, FileUp, Loader2, Pencil, Plus, Trash2 } from "lucide-react";
import { OriginTag, RoleChecks } from "@/components/install-controls";
import { McpSummary } from "@/components/mcp-servers";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Switch } from "@/components/ui/switch";
import {
  addProjectSkill,
  deleteProjectSkill,
  defaultInstallRoles,
  installRoleLabel,
  fetchCatalog,
  fetchProjectInstalls,
  fetchProjectSkill,
  installItem,
  uninstall,
  updateInstall,
  updateProjectSkill,
  type InstallRole,
  type Catalog,
  type CatalogItem,
  type NewProjectSkill,
  type ProjectInstall,
  type ProjectInstalls,
  type SkillCaps,
} from "@/lib/api";
import { parseSkillFile } from "@/lib/skill-file";
import { useFileDrop } from "@/lib/use-file-drop";
import { cn, formatBytes } from "@/lib/utils";

const errorText = (err: unknown) => (err instanceof Error ? err.message : String(err));

const SELECT = "flex h-9 w-full rounded-md border border-input bg-transparent px-3 py-1 font-mono text-xs text-foreground";

/** Where a refusal is shown: under the installed list, in the install form or with the new skill. */
type ErrorAt = "list" | "install" | "add";

type Apply = (change: () => Promise<ProjectInstalls>) => Promise<boolean>;

/** The name, description and SKILL.md body of a skill being added or edited. */
export function SkillFields({
  idPrefix,
  caps,
  draft,
  onChange,
}: {
  idPrefix: string;
  caps: SkillCaps | undefined;
  draft: NewProjectSkill;
  onChange: (draft: NewProjectSkill) => void;
}) {
  return (
    <>
      <div className="grid gap-2 sm:grid-cols-[minmax(0,1fr)_minmax(0,2fr)]">
        <div className="space-y-1">
          <label htmlFor={`${idPrefix}-name`} className="text-xs font-medium text-muted-foreground">Name *</label>
          <Input
            id={`${idPrefix}-name`}
            value={draft.name}
            maxLength={caps?.name_max_chars ?? 64}
            onChange={(e) => onChange({ ...draft, name: e.target.value })}
            placeholder="deploy-check"
            spellCheck={false}
            autoCapitalize="none"
            aria-describedby={`${idPrefix}-name-hint`}
            className="font-mono"
            autoFocus
          />
          <p id={`${idPrefix}-name-hint`} className="text-xs text-muted-foreground">
            Lowercase letters, digits and hyphens.
          </p>
        </div>
        <div className="space-y-1">
          <label htmlFor={`${idPrefix}-description`} className="text-xs font-medium text-muted-foreground">
            Description *
          </label>
          <Input
            id={`${idPrefix}-description`}
            value={draft.description}
            maxLength={caps?.description_max_chars ?? 1024}
            onChange={(e) => onChange({ ...draft, description: e.target.value })}
            aria-describedby={`${idPrefix}-description-hint`}
          />
          <p id={`${idPrefix}-description-hint`} className="text-xs text-muted-foreground">
            One line on what it does and when to use it; agents choose skills by it.
          </p>
        </div>
      </div>
      <div className="space-y-1">
        <label htmlFor={`${idPrefix}-content`} className="text-xs font-medium text-muted-foreground">
          Instructions (SKILL.md body) *
        </label>
        <textarea
          id={`${idPrefix}-content`}
          value={draft.content}
          onChange={(e) => onChange({ ...draft, content: e.target.value })}
          rows={12}
          spellCheck={false}
          aria-describedby={`${idPrefix}-content-hint`}
          className="w-full resize-y rounded-md border border-input bg-transparent px-3 py-2 font-mono text-xs leading-relaxed text-foreground shadow-sm focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring"
        />
        <p id={`${idPrefix}-content-hint`} className="text-xs text-muted-foreground">
          Markdown without frontmatter; the name and description make it. At most{" "}
          {caps ? formatBytes(caps.content_max_bytes) : "64 KB"}.
        </p>
      </div>
    </>
  );
}

/** An item's name, kind, origin, size and description. */
function ItemHeading({ item, projectId }: { item: CatalogItem; projectId: string }) {
  return (
    <span className="block min-w-0 flex-1 space-y-0.5">
      <span className="flex flex-wrap items-center gap-x-2 gap-y-0.5 font-mono text-xs">
        <span className="break-all font-medium">{item.qualified_name}</span>
        <span className="font-sans text-[11px] text-muted-foreground">{item.kind}</span>
        <OriginTag item={item} projectId={projectId} />
        <span className="text-[11px] tabular-nums text-muted-foreground">{formatBytes(item.size_bytes)}</span>
      </span>
      {item.description && <span className="block break-words text-xs text-muted-foreground">{item.description}</span>}
      <McpSummary item={item} />
    </span>
  );
}

/** A global install that reaches the project's task roles: shown here, changed in Settings. */
function InheritedRow({ entry, projectId, taskRoles }: { entry: ProjectInstall; projectId: string; taskRoles: InstallRole[] }) {
  const roles = entry.roles.filter((role) => taskRoles.includes(role));
  return (
    <li className={cn("space-y-1 border-t border-border py-2 first:border-t-0", !entry.enabled && "opacity-60")}>
      <ItemHeading item={entry.item} projectId={projectId} />
      <p className="flex flex-wrap items-center gap-x-2 gap-y-1 text-[11px] text-muted-foreground">
        <Badge variant="outline" className="py-0 text-[10px]">Global</Badge>
        <span>
          {roles.map(installRoleLabel).join(", ")}
          {!entry.enabled && " (off)"}
        </span>
        <a href="#settings/catalog" className="underline-offset-2 hover:text-foreground hover:underline">
          Change in Settings → Skills &amp; MCP
        </a>
      </p>
    </li>
  );
}

/**
 * One of the project's own installs: its roles, its switch and uninstalling. A skill written here also opens to its
 * SKILL.md body and can be edited or deleted.
 */
function InstallRow({
  projectId,
  entry,
  taskRoles,
  caps,
  busy,
  onApply,
}: {
  projectId: string;
  entry: ProjectInstall;
  taskRoles: InstallRole[];
  caps: SkillCaps;
  busy: boolean;
  onApply: Apply;
}) {
  const id = useId();
  const { item } = entry;
  const writtenHere = item.origin === "project" && item.owner_project_id === projectId;
  const [open, setOpen] = useState(false);
  const [editing, setEditing] = useState(false);
  const [confirming, setConfirming] = useState<"uninstall" | "delete" | null>(null);
  const [body, setBody] = useState<string | null>(null);
  const [bodyError, setBodyError] = useState("");
  const [draft, setDraft] = useState<NewProjectSkill>({ name: "", description: "", content: "" });

  const needBody = writtenHere && (open || editing);
  useEffect(() => {
    if (!needBody) return;
    let alive = true;
    setBodyError("");
    fetchProjectSkill(projectId, item.item_id)
      .then((data) => alive && setBody(data.content))
      .catch((err) => alive && setBodyError(errorText(err)));
    return () => {
      alive = false;
    };
  }, [needBody, projectId, item.item_id, item.updated_at]);

  useEffect(() => {
    if (editing && body != null) setDraft((d) => (d.content ? d : { ...d, content: body }));
  }, [editing, body]);

  /** An install change answers with the install; the project's view is read again. */
  const changeInstall = (fields: { roles?: InstallRole[]; enabled?: boolean }) =>
    onApply(() => updateInstall(entry.install_id, fields).then(() => fetchProjectInstalls(projectId)));

  const startEdit = () => {
    setDraft({ name: item.name, description: item.description, content: body ?? "" });
    setConfirming(null);
    setEditing(true);
  };

  const save = async (e: React.FormEvent) => {
    e.preventDefault();
    const fields: Partial<NewProjectSkill> = {};
    if (draft.name.trim() !== item.name) fields.name = draft.name.trim();
    if (draft.description.trim() !== item.description) fields.description = draft.description.trim();
    if (body != null && draft.content !== body) fields.content = draft.content;
    if (Object.keys(fields).length === 0) return setEditing(false);
    if (await onApply(() => updateProjectSkill(projectId, item.item_id, fields))) setEditing(false);
  };

  if (editing) {
    const loadingBody = body == null && !bodyError;
    return (
      <li className="border-t border-border py-3 first:border-t-0">
        <form onSubmit={save} className="space-y-2" aria-label={`Edit ${item.name}`}>
          {loadingBody ? (
            <Loader2 className="h-4 w-4 animate-spin text-muted-foreground" aria-label="Loading the skill" />
          ) : (
            <SkillFields idPrefix={id} caps={caps} draft={draft} onChange={setDraft} />
          )}
          {bodyError && <p role="alert" className="text-xs text-destructive">{bodyError}</p>}
          <div className="flex justify-end gap-2">
            <Button type="button" variant="outline" size="sm" onClick={() => setEditing(false)}>Cancel</Button>
            <Button
              type="submit"
              size="sm"
              disabled={busy || loadingBody || !draft.name.trim() || !draft.description.trim() || !draft.content.trim()}
            >
              {busy && <Loader2 className="h-3.5 w-3.5 animate-spin" />}
              Save
            </Button>
          </div>
        </form>
      </li>
    );
  }

  return (
    <li className="space-y-2 border-t border-border py-2 first:border-t-0">
      {writtenHere ? (
        <button
          type="button"
          onClick={() => setOpen(!open)}
          aria-expanded={open}
          aria-controls={`${id}-body`}
          className={cn(
            "flex w-full min-w-0 items-start gap-1.5 rounded-md text-left focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring",
            !entry.enabled && "opacity-60",
          )}
        >
          <ChevronRight
            className={cn(
              "mt-0.5 h-4 w-4 shrink-0 text-muted-foreground transition-transform duration-150 motion-reduce:transition-none",
              open && "rotate-90",
            )}
            aria-hidden="true"
          />
          <ItemHeading item={item} projectId={projectId} />
        </button>
      ) : (
        <div className={cn("flex", !entry.enabled && "opacity-60")}>
          <ItemHeading item={item} projectId={projectId} />
        </div>
      )}
      {/* In a narrow column the switch and the buttons wrap below the roles instead of squeezing them. */}
      <div className={cn("flex flex-wrap items-center gap-x-3 gap-y-1", writtenHere && "pl-5")}>
        <RoleChecks
          roles={taskRoles}
          value={entry.roles}
          disabled={busy}
          label={`Roles of ${item.qualified_name} in this project`}
          lastHint="An install needs a role: uninstall it instead"
          onChange={(roles) => changeInstall({ roles })}
        />
        {!entry.enabled && <span className="text-[11px] text-muted-foreground">Off</span>}
        <div className="ml-auto flex shrink-0 items-center gap-1">
          <Switch
            checked={entry.enabled}
            disabled={busy}
            onChange={(enabled) => changeInstall({ enabled })}
            label={`Load ${item.qualified_name}`}
          />
          {writtenHere && (
            <>
              <Button type="button" variant="ghost" size="sm" onClick={startEdit} aria-label={`Edit ${item.name}`}>
                <Pencil className="h-3.5 w-3.5" />
              </Button>
              <Button
                type="button"
                variant="ghost"
                size="sm"
                onClick={() => setConfirming("delete")}
                aria-label={`Delete ${item.name}`}
                className="text-muted-foreground hover:bg-destructive/10 hover:text-destructive"
              >
                <Trash2 className="h-3.5 w-3.5" />
              </Button>
            </>
          )}
          <Button
            type="button"
            variant="ghost"
            size="sm"
            onClick={() => setConfirming("uninstall")}
            aria-label={`Uninstall ${item.qualified_name} from this project`}
          >
            Uninstall
          </Button>
        </div>
      </div>
      {confirming && (
        <div className="flex flex-wrap items-center justify-end gap-2">
          <span className="text-xs text-muted-foreground">
            {confirming === "delete"
              ? "Delete this skill from the catalog and every project it is installed in?"
              : "Uninstall it from this project? It stays in the catalog."}
          </span>
          <Button
            type="button"
            variant="destructive"
            size="sm"
            disabled={busy}
            onClick={() =>
              onApply(() =>
                confirming === "delete"
                  ? deleteProjectSkill(projectId, item.item_id)
                  : uninstall(entry.install_id).then(() => fetchProjectInstalls(projectId)),
              )
            }
          >
            {confirming === "delete" ? "Delete" : "Uninstall"}
          </Button>
          <Button type="button" variant="ghost" size="sm" onClick={() => setConfirming(null)}>
            Cancel
          </Button>
        </div>
      )}
      {open && writtenHere && (
        <div id={`${id}-body`} className="ml-5">
          {bodyError ? (
            <p role="alert" className="text-xs text-destructive">{bodyError}</p>
          ) : body == null ? (
            <Loader2 className="h-4 w-4 animate-spin text-muted-foreground" aria-label="Loading the skill" />
          ) : (
            <pre className="max-h-[28rem] overflow-auto whitespace-pre-wrap break-words rounded-md border border-border bg-muted/40 p-3 font-mono text-xs leading-relaxed text-foreground/90">
              {body}
            </pre>
          )}
        </div>
      )}
    </li>
  );
}

/** Pick a catalog item not installed in the project yet and the roles to install it for. */
function InstallFromCatalog({
  projectId,
  catalog,
  catalogError,
  installed,
  taskRoles,
  busy,
  error,
  onInstall,
}: {
  projectId: string;
  catalog: Catalog | null;
  catalogError: string;
  /** The items the project installs itself. */
  installed: ReadonlySet<string>;
  taskRoles: InstallRole[];
  busy: boolean;
  error: string;
  onInstall: (itemId: string, roles: InstallRole[]) => Promise<boolean>;
}) {
  const id = useId();
  const [itemId, setItemId] = useState("");
  const [roles, setRoles] = useState<InstallRole[]>(defaultInstallRoles(taskRoles));
  const candidates = catalog?.items.filter((item) => !installed.has(item.item_id)) ?? [];
  // A pick that got installed or left the catalog meanwhile is no pick.
  const picked = candidates.find((item) => item.item_id === itemId);

  const optionLabel = (item: CatalogItem) => {
    if (item.origin !== "project") return item.qualified_name;
    const owner =
      item.owner_project_id === projectId ? "written here" : `from ${item.owner_project_name ?? item.owner_project_id}`;
    return `${item.qualified_name} — ${owner}`;
  };

  const submit = async (e: React.FormEvent) => {
    e.preventDefault();
    if (picked && !busy && (await onInstall(picked.item_id, roles))) setItemId("");
  };

  return (
    <div className="space-y-2">
      <h3 className="text-xs font-medium text-muted-foreground">Install from the catalog</h3>
      {catalogError ? (
        <p role="alert" className="text-xs text-destructive">{catalogError}</p>
      ) : !catalog ? (
        <Loader2 className="h-4 w-4 animate-spin text-muted-foreground" aria-label="Loading the catalog" />
      ) : catalog.items.length === 0 ? (
        <p className="text-xs text-muted-foreground">
          The catalog is empty. Import plugins and skills in{" "}
          <a href="#settings/catalog" className="underline underline-offset-2 hover:text-foreground">
            Settings → Skills &amp; MCP
          </a>
          .
        </p>
      ) : candidates.length === 0 ? (
        <p className="text-xs text-muted-foreground">Everything in the catalog is installed here.</p>
      ) : (
        <form onSubmit={submit} className="space-y-2">
          <label htmlFor={`${id}-item`} className="sr-only">Plugin, skill or MCP server to install</label>
          <select id={`${id}-item`} value={picked ? itemId : ""} onChange={(e) => setItemId(e.target.value)} className={SELECT}>
            <option value="">Choose one of {candidates.length}…</option>
            {(["plugin", "skill", "mcp"] as const).map((kind) => {
              const group = candidates.filter((item) => item.kind === kind);
              return group.length ? (
                <optgroup key={kind} label={kind === "plugin" ? "Plugins" : kind === "skill" ? "Skills" : "MCP servers"}>
                  {group.map((item) => (
                    <option key={item.item_id} value={item.item_id}>{optionLabel(item)}</option>
                  ))}
                </optgroup>
              ) : null;
            })}
          </select>
          <div className="flex flex-wrap items-center gap-x-3 gap-y-1">
            <RoleChecks
              roles={taskRoles}
              value={roles}
              onChange={setRoles}
              disabled={busy}
              label="Roles to install it for"
              lastHint="An install needs at least one role"
            />
            <Button type="submit" size="sm" className="ml-auto" disabled={busy || !picked}>
              {busy && <Loader2 className="h-3.5 w-3.5 animate-spin" />}
              Install
            </Button>
          </div>
        </form>
      )}
      {error && <p role="alert" className="text-xs text-destructive">{error}</p>}
    </div>
  );
}

/**
 * What this project's Planner, Workers and Reviewers load, from their next run on: its own installs, the global ones
 * that reach them (changed in Settings), installing more from the catalog, and writing skills (also from a SKILL.md
 * file), which are installed here as they are written.
 */
export function ProjectSkillsSection({ projectId }: { projectId: string }) {
  const id = useId();
  const fileInput = useRef<HTMLInputElement>(null);
  const [data, setData] = useState<ProjectInstalls | null>(null);
  const [loadError, setLoadError] = useState("");
  const [catalog, setCatalog] = useState<Catalog | null>(null);
  const [catalogError, setCatalogError] = useState("");
  const [adding, setAdding] = useState(false);
  const [draft, setDraft] = useState<NewProjectSkill>({ name: "", description: "", content: "" });
  const [fromFile, setFromFile] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<{ at: ErrorAt; text: string } | null>(null);

  const loadCatalog = useCallback(() => {
    fetchCatalog()
      .then((view) => {
        setCatalog(view);
        setCatalogError("");
      })
      .catch((err) => setCatalogError(errorText(err)));
  }, []);

  useEffect(() => {
    let alive = true;
    fetchProjectInstalls(projectId)
      .then((view) => alive && setData(view))
      .catch((err) => alive && setLoadError(errorText(err)));
    return () => {
      alive = false;
    };
  }, [projectId]);

  useEffect(loadCatalog, [loadCatalog]);

  const startAdding = (start: NewProjectSkill = { name: "", description: "", content: "" }, source = "") => {
    setAdding(true);
    setDraft(start);
    setFromFile(source);
    setError(null);
  };

  /** A SKILL.md opens the add form with its frontmatter's name and description and the body below it. */
  const readSkillFile = async (file: File) => {
    const cap = data?.skill_caps.content_max_bytes ?? 64 * 1024;
    // The frontmatter is not part of the body, so allow it some room over the body's cap.
    if (file.size > cap + 8 * 1024) {
      setError({ at: "add", text: `A skill's body has at most ${formatBytes(cap)}; ${file.name} has ${formatBytes(file.size)}.` });
      return;
    }
    startAdding(parseSkillFile(await file.text()), file.name);
  };

  const caps = data?.skill_caps;
  const full = data && caps ? data.owned_count >= caps.project_max_skills : false;
  const { dragging, dropProps } = useFileDrop((file) => void readSkillFile(file), !!data && !full);

  /**
   * Run one change; the project's view it answers with replaces the list (and the catalog is read again for the
   * install form), and a refusal shows the server's reason where `at` says.
   */
  const apply = async (change: () => Promise<ProjectInstalls>, at: ErrorAt = "list") => {
    setBusy(true);
    setError(null);
    try {
      setData(await change());
      loadCatalog();
      return true;
    } catch (err) {
      setError({ at, text: errorText(err) });
      return false;
    } finally {
      setBusy(false);
    }
  };

  const submit = async (e: React.FormEvent) => {
    e.preventDefault();
    if (busy) return;
    const skill = { name: draft.name.trim(), description: draft.description.trim(), content: draft.content };
    if (await apply(() => addProjectSkill(projectId, skill), "add")) setAdding(false);
  };

  const install = (itemId: string, roles: InstallRole[]) =>
    apply(
      () =>
        installItem({ item_id: itemId, scope: "project", project_id: projectId, roles }).then(() =>
          fetchProjectInstalls(projectId),
        ),
      "install",
    );

  const errorAt = (at: ErrorAt) => (error?.at === at ? error.text : "");
  const own = data?.installs.filter((entry) => !entry.inherited) ?? [];
  const inherited = (data?.installs.length ?? 0) - own.length;
  const ready = draft.name.trim() && draft.description.trim() && draft.content.trim();

  return (
    <section
      aria-labelledby={`${id}-title`}
      {...dropProps}
      className={cn(
        "relative space-y-3 rounded-lg border border-border bg-card px-5 py-4 transition-colors",
        dragging && "border-primary bg-primary/5",
      )}
    >
      <div className="flex flex-wrap items-baseline justify-between gap-x-3 gap-y-1">
        <h2 id={`${id}-title`} className="text-sm font-semibold">Skills</h2>
        {data && caps && (
          <span className="text-[11px] tabular-nums text-muted-foreground">
            {own.length} installed here{inherited > 0 && ` · ${inherited} global`} · {data.owned_count} /{" "}
            {caps.project_max_skills} written here
          </span>
        )}
      </div>
      <p className="text-xs text-muted-foreground">
        Plugins, skills and MCP servers this project&apos;s Planner, Workers and Reviewers load. Skills written here
        load as{" "}
        <span className="font-mono">project:&lt;name&gt;</span>. Nothing is written to the project&apos;s directory.
        Changes apply to their next run.
      </p>
      {loadError && <p role="alert" className="text-xs text-destructive">{loadError}</p>}
      {!data && !loadError && <Loader2 className="h-4 w-4 animate-spin text-muted-foreground" aria-label="Loading" />}
      {data && caps && (
        <>
          <div className="space-y-2">
            <h3 className="text-xs font-medium text-muted-foreground">Installed here</h3>
            {data.installs.length ? (
              <ul className="rounded-md border border-border px-3">
                {data.installs.map((entry) =>
                  entry.inherited ? (
                    <InheritedRow key={entry.install_id} entry={entry} projectId={projectId} taskRoles={data.task_roles} />
                  ) : (
                    <InstallRow
                      key={entry.install_id}
                      projectId={projectId}
                      entry={entry}
                      taskRoles={data.task_roles}
                      caps={caps}
                      busy={busy}
                      onApply={apply}
                    />
                  ),
                )}
              </ul>
            ) : (
              <p className="text-xs text-muted-foreground">Nothing installed here.</p>
            )}
            {errorAt("list") && <p role="alert" className="text-xs text-destructive">{errorAt("list")}</p>}
          </div>
          <InstallFromCatalog
            projectId={projectId}
            catalog={catalog}
            catalogError={catalogError}
            installed={new Set(own.map((entry) => entry.item_id))}
            taskRoles={data.task_roles}
            busy={busy}
            error={errorAt("install")}
            onInstall={install}
          />
        </>
      )}
      {data && !adding && (
        <div className="flex flex-wrap items-center gap-2">
          <Button type="button" variant="outline" size="sm" onClick={() => startAdding()} disabled={full}>
            <Plus className="h-3.5 w-3.5" />
            New skill
          </Button>
          <Button type="button" variant="outline" size="sm" onClick={() => fileInput.current?.click()} disabled={full}>
            <FileUp className="h-3.5 w-3.5" />
            Upload SKILL.md
          </Button>
          {!full && <span className="text-xs text-muted-foreground">or drop a SKILL.md here</span>}
          <input
            ref={fileInput}
            type="file"
            accept=".md,text/markdown,text/plain"
            className="hidden"
            tabIndex={-1}
            aria-hidden="true"
            onChange={(e) => {
              const file = e.target.files?.[0];
              e.target.value = "";
              if (file) void readSkillFile(file);
            }}
          />
        </div>
      )}
      {adding && (
        <form onSubmit={submit} className="space-y-2 rounded-md border border-border p-3">
          <p className="flex items-center gap-1.5 text-xs font-medium">
            <Plus className="h-3.5 w-3.5" aria-hidden="true" />
            New skill{fromFile && <span className="font-normal text-muted-foreground"> · from {fromFile}; check the fields</span>}
          </p>
          <SkillFields idPrefix={`${id}-skill`} caps={caps} draft={draft} onChange={setDraft} />
          {errorAt("add") && <p role="alert" className="text-xs text-destructive">{errorAt("add")}</p>}
          <div className="flex justify-end gap-2">
            <Button type="button" variant="outline" size="sm" onClick={() => setAdding(false)}>Cancel</Button>
            <Button type="submit" size="sm" disabled={busy || !ready}>
              {busy && <Loader2 className="h-3.5 w-3.5 animate-spin" />}
              Add skill
            </Button>
          </div>
        </form>
      )}
      {!adding && errorAt("add") && <p role="alert" className="text-xs text-destructive">{errorAt("add")}</p>}
      {full && !adding && caps && (
        <p className="text-xs text-muted-foreground">A project writes at most {caps.project_max_skills} skills.</p>
      )}
      {dragging && (
        <div
          aria-hidden="true"
          className="pointer-events-none absolute inset-0 flex items-center justify-center rounded-lg bg-background/70 text-sm font-medium text-primary"
        >
          Drop to add it as a skill
        </div>
      )}
    </section>
  );
}
