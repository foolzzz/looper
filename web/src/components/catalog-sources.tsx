import { useEffect, useId, useRef, useState, type FormEvent } from "react";
import { FileUp, GitBranch, Loader2, Plus } from "lucide-react";
import { bytesBase64 } from "@/components/project-knowledge";
import { SkillFields } from "@/components/project-skills";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import {
  createCatalogSkill,
  fetchCatalogItem,
  importGitRepo,
  previewGitRepo,
  updateCatalogItem,
  uploadCatalogItem,
  type Catalog,
  type CatalogItem,
  type GitPreview,
  type GitPreviewItem,
  type LibrarySkipped,
  type NewProjectSkill,
  type SkillCaps,
} from "@/lib/api";
import { useFileDrop } from "@/lib/use-file-drop";
import { cn, formatBytes } from "@/lib/utils";

const errorText = (err: unknown) => (err instanceof Error ? err.message : String(err));

type Panel = "upload" | "git" | "skill" | null;

/** The caps of a skill's fields: Settings skills like project skills, uploaded and git ones up to a file's cap. */
function skillCaps(contentMax: number): SkillCaps {
  return { name_max_chars: 64, description_max_chars: 1024, content_max_bytes: contentMax, project_max_skills: 0 };
}

function Skipped({ skipped }: { skipped: LibrarySkipped[] }) {
  if (!skipped.length) return null;
  return (
    <details className="text-[11px] text-muted-foreground">
      <summary className="cursor-pointer text-warning">{skipped.length} left out</summary>
      <ul className="mt-1 space-y-0.5 font-mono">
        {skipped.map((s, i) => (
          <li key={i} className="break-all">
            {s.item}
            {s.path && `/${s.path}`} — {s.reason}
          </li>
        ))}
      </ul>
    </details>
  );
}

/** Upload one SKILL.md or zip, optionally under another name. */
function UploadForm({
  file,
  maxBytes,
  onDone,
  onCancel,
}: {
  file: File;
  maxBytes: number;
  onDone: (message: string) => void;
  onCancel: () => void;
}) {
  const id = useId();
  const [name, setName] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState(file.size > maxBytes ? `${file.name} is over the ${formatBytes(maxBytes)} cap` : "");

  const submit = async (e: FormEvent) => {
    e.preventDefault();
    setBusy(true);
    setError("");
    try {
      const bytes = new Uint8Array(await file.arrayBuffer());
      const { item, skipped } = await uploadCatalogItem({
        filename: file.name,
        content_base64: bytesBase64(bytes),
        ...(name.trim() ? { name: name.trim() } : {}),
      });
      onDone(
        `Added ${item.kind} ${item.qualified_name}, installed nowhere` +
          (skipped.length ? ` · ${skipped.length} binary file${skipped.length === 1 ? "" : "s"} left out` : ""),
      );
    } catch (err) {
      setError(errorText(err));
    } finally {
      setBusy(false);
    }
  };

  return (
    <form onSubmit={submit} className="space-y-2">
      <p className="text-xs">
        <span className="font-mono">{file.name}</span>{" "}
        <span className="text-muted-foreground">· {formatBytes(file.size)}</span>
      </p>
      <div className="space-y-1">
        <label htmlFor={`${id}-name`} className="text-xs font-medium text-muted-foreground">
          Name (optional)
        </label>
        <Input
          id={`${id}-name`}
          value={name}
          onChange={(e) => setName(e.target.value)}
          placeholder="the name its SKILL.md or plugin.json gives"
          spellCheck={false}
          autoCapitalize="none"
          className="h-8 font-mono text-xs"
        />
      </div>
      {error && <p role="alert" className="text-xs text-destructive">{error}</p>}
      <div className="flex justify-end gap-2">
        <Button type="button" variant="outline" size="sm" onClick={onCancel}>Cancel</Button>
        <Button type="submit" size="sm" disabled={busy || file.size > maxBytes}>
          {busy && <Loader2 className="h-3.5 w-3.5 animate-spin" />}
          Add to catalog
        </Button>
      </div>
    </form>
  );
}

const STATUS_TEXT: Record<GitPreviewItem["status"], string> = {
  new: "new",
  update: "update",
  unchanged: "unchanged",
  conflict: "name taken",
};

const STATUS_ORDER: Record<GitPreviewItem["status"], number> = { new: 0, update: 1, conflict: 2, unchanged: 3 };

/** Preview a repository's plugins and skills, then import the checked ones. */
function GitForm({ onDone, onCancel }: { onDone: (message: string) => void; onCancel: () => void }) {
  const id = useId();
  const [url, setUrl] = useState("");
  const [ref, setRef] = useState("");
  const [subpath, setSubpath] = useState("");
  const [preview, setPreview] = useState<GitPreview | null>(null);
  const [chosen, setChosen] = useState<Record<string, boolean>>({});
  const [names, setNames] = useState<Record<string, string>>({});
  const [busy, setBusy] = useState<"preview" | "import" | null>(null);
  const [error, setError] = useState("");

  const source = () => ({
    url: url.trim(),
    ...(ref.trim() ? { ref: ref.trim() } : {}),
    ...(subpath.trim() ? { subpath: subpath.trim() } : {}),
  });

  const runPreview = async (e: FormEvent) => {
    e.preventDefault();
    setBusy("preview");
    setError("");
    try {
      const result = await previewGitRepo(source());
      // What an import would change first; unchanged items last.
      result.items.sort((a, b) => STATUS_ORDER[a.status] - STATUS_ORDER[b.status]);
      setPreview(result);
      setChosen(Object.fromEntries(result.items.map((item) => [item.path, item.status === "new" || item.status === "update"])));
      setNames(Object.fromEntries(result.items.map((item) => [item.path, item.name])));
    } catch (err) {
      setPreview(null);
      setError(errorText(err));
    } finally {
      setBusy(null);
    }
  };

  const runImport = async () => {
    if (!preview) return;
    setBusy("import");
    setError("");
    try {
      const items = preview.items
        .filter((item) => chosen[item.path])
        .map((item) => {
          const name = (names[item.path] ?? "").trim();
          return name && name !== item.name ? { path: item.path, name } : { path: item.path };
        });
      const result = await importGitRepo({ ...source(), commit: preview.commit, items });
      const parts = [
        result.added.length && `${result.added.length} added`,
        result.updated.length && `${result.updated.length} updated`,
        result.unchanged.length && `${result.unchanged.length} unchanged`,
      ].filter(Boolean);
      onDone(`Imported from ${result.url}@${result.commit.slice(0, 12)}: ${parts.join(", ")}; installed nowhere`);
    } catch (err) {
      setError(errorText(err));
    } finally {
      setBusy(null);
    }
  };

  const count = preview ? preview.items.filter((item) => chosen[item.path]).length : 0;
  const allChecked = !!preview && preview.items.length > 0 && count === preview.items.length;

  return (
    <div className="space-y-3">
      <form onSubmit={runPreview} className="grid gap-2 sm:grid-cols-[minmax(0,3fr)_minmax(0,1fr)_minmax(0,1.5fr)_auto] sm:items-end">
        <div className="space-y-1">
          <label htmlFor={`${id}-url`} className="text-xs font-medium text-muted-foreground">Repository URL *</label>
          <Input
            id={`${id}-url`}
            value={url}
            onChange={(e) => setUrl(e.target.value)}
            placeholder="https://github.com/owner/repo"
            spellCheck={false}
            autoCapitalize="none"
            className="h-8 font-mono text-xs"
            autoFocus
          />
        </div>
        <div className="space-y-1">
          <label htmlFor={`${id}-ref`} className="text-xs font-medium text-muted-foreground">Branch or tag</label>
          <Input
            id={`${id}-ref`}
            value={ref}
            onChange={(e) => setRef(e.target.value)}
            placeholder="default"
            spellCheck={false}
            className="h-8 font-mono text-xs"
          />
        </div>
        <div className="space-y-1">
          <label htmlFor={`${id}-subpath`} className="text-xs font-medium text-muted-foreground">Subpath</label>
          <Input
            id={`${id}-subpath`}
            value={subpath}
            onChange={(e) => setSubpath(e.target.value)}
            placeholder="skills/pdf"
            spellCheck={false}
            className="h-8 font-mono text-xs"
          />
        </div>
        <Button type="submit" size="sm" disabled={!url.trim() || busy !== null}>
          {busy === "preview" && <Loader2 className="h-3.5 w-3.5 animate-spin" />}
          Preview
        </Button>
      </form>
      <p className="text-[11px] text-muted-foreground">
        Public https:// repositories only. A shallow clone is scanned for plugins (.claude-plugin/plugin.json) and
        skills (SKILL.md); nothing in it is run. Importing from the same URL again updates those items in place.
      </p>
      {preview && (
        <div className="space-y-2 rounded-md border border-border">
          <div className="flex flex-wrap items-center gap-x-3 gap-y-1 px-3 pt-2 text-xs">
            <span className="font-mono text-muted-foreground">
              {preview.ref ?? "default branch"} @ {preview.commit.slice(0, 12)}
              {preview.subpath && ` · ${preview.subpath}`}
            </span>
            <span className="text-muted-foreground">
              {preview.items.length} found{preview.items.length ? ` · ${count} selected` : ""}
            </span>
            {preview.items.length > 0 && (
              <label className="ml-auto inline-flex cursor-pointer items-center gap-1.5 text-xs">
                <input
                  type="checkbox"
                  className="h-4 w-4 accent-primary"
                  checked={allChecked}
                  onChange={(e) =>
                    setChosen(Object.fromEntries(preview.items.map((item) => [item.path, e.target.checked])))
                  }
                />
                All
              </label>
            )}
          </div>
          {preview.items.length === 0 ? (
            <p className="px-3 pb-2 text-xs text-muted-foreground">No plugin or skill in this repository.</p>
          ) : (
            <ul className="max-h-80 overflow-y-auto overscroll-contain" aria-label="Items found">
              {preview.items.map((item) => (
                <li key={item.path} className="space-y-1 border-t border-border px-3 py-2">
                  <div className="flex flex-wrap items-center gap-x-2 gap-y-1">
                    <input
                      type="checkbox"
                      className="h-4 w-4 accent-primary"
                      checked={!!chosen[item.path]}
                      onChange={(e) => setChosen((c) => ({ ...c, [item.path]: e.target.checked }))}
                      aria-label={`Import ${item.path}`}
                    />
                    <Input
                      value={names[item.path] ?? ""}
                      onChange={(e) => setNames((n) => ({ ...n, [item.path]: e.target.value }))}
                      aria-label={`Name for ${item.path}`}
                      spellCheck={false}
                      autoCapitalize="none"
                      className="h-7 w-48 font-mono text-xs"
                    />
                    <span className="text-[11px] text-muted-foreground">{item.kind}</span>
                    <Badge
                      variant="outline"
                      className={cn(
                        "py-0 text-[10px] font-normal",
                        item.status === "conflict" && "border-warning text-warning",
                      )}
                    >
                      {STATUS_TEXT[item.status]}
                      {item.modified ? " · edited here" : ""}
                    </Badge>
                    <span className="ml-auto font-mono text-[11px] text-muted-foreground">
                      {item.path} · {item.file_count} files · {formatBytes(item.size_bytes)}
                    </span>
                  </div>
                  {item.description && (
                    <p className="line-clamp-2 break-words pl-6 text-[11px] text-muted-foreground">{item.description}</p>
                  )}
                  {item.reason && <p className="pl-6 text-[11px] text-warning">{item.reason}</p>}
                </li>
              ))}
            </ul>
          )}
          {preview.skipped.length > 0 && (
            <div className="border-t border-border px-3 py-2">
              <Skipped skipped={preview.skipped} />
            </div>
          )}
          <div className="flex justify-end gap-2 border-t border-border px-3 py-2">
            <Button type="button" size="sm" onClick={() => void runImport()} disabled={!count || busy !== null}>
              {busy === "import" && <Loader2 className="h-3.5 w-3.5 animate-spin" />}
              Import {count} selected
            </Button>
          </div>
        </div>
      )}
      {error && <p role="alert" className="text-xs text-destructive">{error}</p>}
      <div className="flex justify-end">
        <Button type="button" variant="outline" size="sm" onClick={onCancel}>Close</Button>
      </div>
    </div>
  );
}

const EMPTY_SKILL: NewProjectSkill = { name: "", description: "", content: "" };

/** Write a new skill, or edit one written in Settings, uploaded or taken from git. */
export function SkillForm({
  item,
  contentMax,
  onSaved,
  onCancel,
}: {
  /** The skill being edited; absent for a new one. */
  item?: CatalogItem;
  contentMax: number;
  onSaved: (item: CatalogItem) => void;
  onCancel: () => void;
}) {
  const id = useId();
  const [draft, setDraft] = useState<NewProjectSkill | null>(item ? null : EMPTY_SKILL);
  const original = useRef<NewProjectSkill | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");

  useEffect(() => {
    if (!item) return;
    fetchCatalogItem(item.item_id)
      .then((detail) => {
        const loaded = { name: item.name, description: item.description, content: detail.content ?? "" };
        original.current = loaded;
        setDraft(loaded);
      })
      .catch((err) => setError(errorText(err)));
  }, [item]);

  const submit = async (e: FormEvent) => {
    e.preventDefault();
    if (!draft) return;
    setBusy(true);
    setError("");
    try {
      if (item) {
        const before = original.current;
        const fields: Partial<NewProjectSkill> = {};
        for (const key of ["name", "description", "content"] as const) {
          if (!before || draft[key] !== before[key]) fields[key] = draft[key];
        }
        if (!Object.keys(fields).length) {
          onCancel();
          return;
        }
        onSaved((await updateCatalogItem(item.item_id, fields)).item);
      } else {
        onSaved((await createCatalogSkill(draft)).item);
      }
    } catch (err) {
      setError(errorText(err));
    } finally {
      setBusy(false);
    }
  };

  const ready = !!draft && !!draft.name.trim() && !!draft.description.trim() && !!draft.content.trim();
  return (
    <form onSubmit={submit} className="space-y-2">
      {item && item.origin !== "ui" && (
        <p className="text-[11px] text-muted-foreground">
          Editing marks this {item.origin === "git" ? "git" : "uploaded"} skill modified; its other files and
          frontmatter stay.{item.origin === "git" && " Re-importing it from its repository replaces the edit."}
        </p>
      )}
      {draft ? (
        <SkillFields idPrefix={id} caps={skillCaps(contentMax)} draft={draft} onChange={setDraft} />
      ) : (
        !error && <Loader2 className="h-4 w-4 animate-spin text-muted-foreground" aria-label="Loading" />
      )}
      {error && <p role="alert" className="text-xs text-destructive">{error}</p>}
      <div className="flex justify-end gap-2">
        <Button type="button" variant="outline" size="sm" onClick={onCancel}>Cancel</Button>
        <Button type="submit" size="sm" disabled={busy || !ready}>
          {busy && <Loader2 className="h-3.5 w-3.5 animate-spin" />}
          {item ? "Save" : "Create skill"}
        </Button>
      </div>
    </form>
  );
}

/**
 * More ways into the catalog: upload a SKILL.md or zip (or drop one here), import from a git repository after a
 * preview, or write a new skill. Nothing added here is installed.
 */
export function CatalogSources({ catalog, onAdded }: { catalog: Catalog; onAdded: () => void }) {
  const [panel, setPanel] = useState<Panel>(null);
  const [file, setFile] = useState<File | null>(null);
  const [notice, setNotice] = useState("");
  const fileInput = useRef<HTMLInputElement>(null);

  const chooseFile = (chosen: File) => {
    setFile(chosen);
    setPanel("upload");
    setNotice("");
  };
  const { dragging, dropProps } = useFileDrop(chooseFile);
  const done = (message: string) => {
    setPanel(null);
    setFile(null);
    setNotice(message);
    onAdded();
  };
  const close = () => {
    setPanel(null);
    setFile(null);
  };
  const open = (next: Panel) => {
    setPanel(panel === next ? null : next);
    setNotice("");
  };

  return (
    <div {...dropProps} className="relative border-t border-border px-5 py-2">
      <div className="flex flex-wrap items-center gap-2">
        <Button type="button" variant="outline" size="sm" onClick={() => fileInput.current?.click()}>
          <FileUp className="h-3.5 w-3.5" />
          Upload
        </Button>
        <Button type="button" variant="outline" size="sm" onClick={() => open("git")}>
          <GitBranch className="h-3.5 w-3.5" />
          Import from git
        </Button>
        <Button type="button" variant="outline" size="sm" onClick={() => open("skill")}>
          <Plus className="h-3.5 w-3.5" />
          New skill
        </Button>
        <span className="text-xs text-muted-foreground">or drop a SKILL.md or .zip here · added items are installed nowhere</span>
        <input
          ref={fileInput}
          type="file"
          accept=".md,.zip,text/markdown,application/zip"
          className="hidden"
          tabIndex={-1}
          aria-hidden="true"
          onChange={(e) => {
            const chosen = e.target.files?.[0];
            e.target.value = "";
            if (chosen) chooseFile(chosen);
          }}
        />
      </div>
      {notice && <p role="status" className="mt-2 text-xs text-success">{notice}</p>}
      {panel && (
        <div className="mt-2 rounded-md border border-border p-3">
          {panel === "upload" && file && (
            <UploadForm key={`${file.name}-${file.size}-${file.lastModified}`} file={file}
                        maxBytes={catalog.caps.upload_max_bytes} onDone={done} onCancel={close} />
          )}
          {panel === "git" && <GitForm onDone={done} onCancel={close} />}
          {panel === "skill" && (
            <SkillForm
              contentMax={64 * 1024}
              onSaved={(item) => done(`Added skill ${item.qualified_name}, installed nowhere`)}
              onCancel={close}
            />
          )}
        </div>
      )}
      {dragging && (
        <div
          aria-hidden="true"
          className="pointer-events-none absolute inset-0 flex items-center justify-center rounded-md bg-background/80 text-sm font-medium text-primary"
        >
          Drop to upload it into the catalog
        </div>
      )}
    </div>
  );
}
