import { useEffect, useId, useState } from "react";
import { AlertCircle, ChevronRight, FileText, FolderOpen, Loader2, Pencil, Plus, Trash2, Upload } from "lucide-react";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import {
  addKnowledgeDocument,
  deleteKnowledgeDocument,
  fetchKnowledge,
  fetchKnowledgeDocument,
  updateKnowledgeDocument,
  type KnowledgeDocument,
  type KnowledgeDocumentBody,
  type KnowledgeDocumentUpdate,
  type KnowledgeSourceKind,
  type NewKnowledgeDocument,
  type ProjectKnowledge,
} from "@/lib/api";
import { useFileDrop } from "@/lib/use-file-drop";
import { cn, formatBytes } from "@/lib/utils";

const SOURCES: { kind: KnowledgeSourceKind; label: string; icon: typeof FileText }[] = [
  { kind: "text", label: "Text", icon: FileText },
  { kind: "upload", label: "Upload", icon: Upload },
  { kind: "path", label: "Project file", icon: FolderOpen },
];

const SOURCE_LABEL: Record<KnowledgeSourceKind, string> = { text: "Text", upload: "Upload", path: "Project file" };

const TEXTAREA =
  "w-full resize-y rounded-md border border-input bg-transparent px-3 py-2 font-mono text-xs leading-relaxed text-foreground shadow-sm focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring";

/** Bytes as base64, in chunks small enough for String.fromCharCode. */
function bytesBase64(bytes: Uint8Array) {
  let binary = "";
  for (let i = 0; i < bytes.length; i += 0x8000) binary += String.fromCharCode(...bytes.subarray(i, i + 0x8000));
  return btoa(binary);
}

const errorText = (err: unknown) => (err instanceof Error ? err.message : String(err));

/** One document: its row, its body when opened, and its editor. */
function DocumentRow({
  projectId,
  document,
  busy,
  onApply,
}: {
  projectId: string;
  document: KnowledgeDocument;
  busy: boolean;
  /** Run a change; resolves true when it was saved. */
  onApply: (change: () => Promise<ProjectKnowledge>) => Promise<boolean>;
}) {
  const id = useId();
  const [open, setOpen] = useState(false);
  const [editing, setEditing] = useState(false);
  const [confirming, setConfirming] = useState(false);
  const [body, setBody] = useState<KnowledgeDocumentBody | null>(null);
  const [bodyError, setBodyError] = useState("");
  const [title, setTitle] = useState("");
  const [summary, setSummary] = useState("");
  const [content, setContent] = useState("");
  const [path, setPath] = useState("");
  const stored = document.source_kind !== "path";

  // The body is fetched when the row opens or its editor needs it, and again after the document changes.
  const needBody = open || (editing && stored);
  useEffect(() => {
    if (!needBody) return;
    let alive = true;
    setBodyError("");
    fetchKnowledgeDocument(projectId, document.document_id)
      .then((data) => alive && setBody(data))
      .catch((err) => alive && setBodyError(errorText(err)));
    return () => {
      alive = false;
    };
  }, [needBody, projectId, document.document_id, document.sha256, document.updated_at]);

  useEffect(() => {
    if (editing && stored && body?.content != null) setContent(body.content);
  }, [editing, stored, body]);

  const startEdit = () => {
    setTitle(document.title);
    setSummary(document.summary);
    setPath(document.path ?? "");
    setContent(body?.content ?? "");
    setConfirming(false);
    setEditing(true);
  };

  const save = async (e: React.FormEvent) => {
    e.preventDefault();
    const fields: KnowledgeDocumentUpdate = {};
    if (title.trim() !== document.title) fields.title = title.trim();
    if (summary.trim() !== document.summary) fields.summary = summary.trim();
    if (document.source_kind === "path" && path.trim() !== document.path) fields.path = path.trim();
    if (stored && body?.content != null && content !== body.content) {
      if (document.source_kind === "text") fields.content = content;
      else {
        fields.filename = document.filename ?? "upload.txt";
        fields.content_base64 = bytesBase64(new TextEncoder().encode(content));
      }
    }
    if (Object.keys(fields).length === 0) return setEditing(false);
    if (await onApply(() => updateKnowledgeDocument(projectId, document.document_id, fields))) setEditing(false);
  };

  if (editing) {
    const loadingBody = stored && body?.content == null && !bodyError;
    return (
      <li className="border-t border-border py-3 first:border-t-0">
        <form onSubmit={save} className="space-y-2" aria-label={`Edit ${document.title}`}>
          <div className="grid gap-2 sm:grid-cols-2">
            <div className="space-y-1">
              <label htmlFor={`${id}-title`} className="text-xs font-medium text-muted-foreground">Title *</label>
              <Input id={`${id}-title`} value={title} maxLength={120} onChange={(e) => setTitle(e.target.value)} autoFocus />
            </div>
            <div className="space-y-1">
              <label htmlFor={`${id}-summary`} className="text-xs font-medium text-muted-foreground">
                One-line summary *
              </label>
              <Input id={`${id}-summary`} value={summary} maxLength={300} onChange={(e) => setSummary(e.target.value)} />
            </div>
          </div>
          {document.source_kind === "path" ? (
            <div className="space-y-1">
              <label htmlFor={`${id}-path`} className="text-xs font-medium text-muted-foreground">Path *</label>
              <Input id={`${id}-path`} value={path} onChange={(e) => setPath(e.target.value)} className="font-mono" />
              <p className="text-xs text-muted-foreground">
                Relative to the project&apos;s default directory. Edit the file itself in the repository.
              </p>
            </div>
          ) : (
            <div className="space-y-1">
              <label htmlFor={`${id}-content`} className="text-xs font-medium text-muted-foreground">
                Content{document.source_kind === "upload" ? ` (${document.filename})` : ""} *
              </label>
              {loadingBody ? (
                <Loader2 className="h-4 w-4 animate-spin text-muted-foreground" aria-label="Loading the content" />
              ) : (
                <textarea
                  id={`${id}-content`}
                  value={content}
                  onChange={(e) => setContent(e.target.value)}
                  rows={14}
                  spellCheck={false}
                  className={TEXTAREA}
                />
              )}
            </div>
          )}
          {bodyError && <p role="alert" className="text-xs text-destructive">{bodyError}</p>}
          <div className="flex justify-end gap-2">
            <Button type="button" variant="outline" size="sm" onClick={() => setEditing(false)}>Cancel</Button>
            <Button
              type="submit"
              size="sm"
              disabled={busy || !title.trim() || !summary.trim() || (stored ? loadingBody || !content.trim() : !path.trim())}
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
      <div className="flex items-start gap-2">
        <button
          type="button"
          onClick={() => setOpen(!open)}
          aria-expanded={open}
          aria-controls={`${id}-body`}
          className="group flex min-w-0 flex-1 items-start gap-1.5 rounded-md text-left focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring"
        >
          <ChevronRight
            className={cn(
              "mt-0.5 h-4 w-4 shrink-0 text-muted-foreground transition-transform duration-150 motion-reduce:transition-none",
              open && "rotate-90",
            )}
            aria-hidden="true"
          />
          <span className="min-w-0 flex-1 space-y-0.5">
            <span className="block truncate text-sm font-medium group-hover:underline">{document.title}</span>
            <span className="block break-words text-xs text-muted-foreground">{document.summary}</span>
            <span className="flex flex-wrap items-center gap-x-2 gap-y-1 font-mono text-[11px] text-muted-foreground">
              <span>{SOURCE_LABEL[document.source_kind]}</span>
              <span aria-hidden="true">·</span>
              <span className="break-all">{document.path ?? document.filename}</span>
              <span aria-hidden="true">·</span>
              <span className="tabular-nums">{formatBytes(document.size_bytes)}</span>
              {document.missing && (
                <Badge variant="warning" className="gap-1 font-sans">
                  <AlertCircle className="h-3 w-3" aria-hidden="true" />
                  Missing
                </Badge>
              )}
            </span>
          </span>
        </button>
        {!confirming && (
          <div className="flex shrink-0 items-center">
            <Button type="button" variant="ghost" size="sm" onClick={startEdit} aria-label={`Edit ${document.title}`}>
              <Pencil className="h-3.5 w-3.5" />
            </Button>
            <Button
              type="button"
              variant="ghost"
              size="sm"
              onClick={() => setConfirming(true)}
              aria-label={`Delete ${document.title}`}
              className="text-muted-foreground hover:bg-destructive/10 hover:text-destructive"
            >
              <Trash2 className="h-3.5 w-3.5" />
            </Button>
          </div>
        )}
      </div>
      {confirming && (
        <div className="flex flex-wrap items-center justify-end gap-2">
          <span className="text-xs text-muted-foreground">Delete this document?</span>
          <Button
            type="button"
            variant="destructive"
            size="sm"
            onClick={() => onApply(() => deleteKnowledgeDocument(projectId, document.document_id))}
            disabled={busy}
          >
            Delete
          </Button>
          <Button type="button" variant="ghost" size="sm" onClick={() => setConfirming(false)}>
            Cancel
          </Button>
        </div>
      )}
      {open && (
        <div id={`${id}-body`} className="ml-5">
          {bodyError ? (
            <p role="alert" className="text-xs text-destructive">{bodyError}</p>
          ) : !body ? (
            <Loader2 className="h-4 w-4 animate-spin text-muted-foreground" aria-label="Loading the content" />
          ) : body.content == null ? (
            <p className="text-xs text-warning">{body.content_error ?? "The content is unavailable."}</p>
          ) : (
            <>
              {document.source_kind === "path" && (
                <p className="mb-1 text-[11px] text-muted-foreground">The file as it is now; agents read it live.</p>
              )}
              <pre className="max-h-[28rem] overflow-auto whitespace-pre-wrap break-words rounded-md border border-border bg-muted/40 p-3 font-mono text-xs leading-relaxed text-foreground/90">
                {body.content}
              </pre>
            </>
          )}
        </div>
      )}
    </li>
  );
}

/**
 * The project's knowledge: documents its Planner, Workers and Reviewers read when they need them. Opening a row
 * shows its body; adding (also by dropping a file on the section), editing and deleting apply at once.
 */
export function ProjectKnowledgeSection({ projectId }: { projectId: string }) {
  const id = useId();
  const [knowledge, setKnowledge] = useState<ProjectKnowledge | null>(null);
  const [loadError, setLoadError] = useState("");
  const [adding, setAdding] = useState<KnowledgeSourceKind | null>(null);
  const [title, setTitle] = useState("");
  const [summary, setSummary] = useState("");
  const [content, setContent] = useState("");
  const [path, setPath] = useState("");
  const [file, setFile] = useState<File | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");

  useEffect(() => {
    let alive = true;
    fetchKnowledge(projectId)
      .then((data) => alive && setKnowledge(data))
      .catch((err) => alive && setLoadError(errorText(err)));
    return () => {
      alive = false;
    };
  }, [projectId]);

  const startAdding = (kind: KnowledgeSourceKind, chosen: File | null = null) => {
    setAdding(kind);
    setTitle(chosen?.name ?? "");
    setSummary("");
    setContent("");
    setPath("");
    setFile(chosen);
    setError("");
  };

  const { dragging, dropProps } = useFileDrop((dropped) => startAdding("upload", dropped), !!knowledge);

  /** Run one change; the view it answers with replaces the list, and a refusal shows the server's reason. */
  const apply = async (change: () => Promise<ProjectKnowledge>) => {
    setBusy(true);
    setError("");
    try {
      setKnowledge(await change());
      return true;
    } catch (err) {
      setError(errorText(err));
      return false;
    } finally {
      setBusy(false);
    }
  };

  const submit = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!adding || busy) return;
    setError("");
    const common = { title: title.trim(), summary: summary.trim() };
    let document: NewKnowledgeDocument;
    if (adding === "upload") {
      if (!file) return setError("Choose a file to upload.");
      const cap = knowledge?.caps.document_max_bytes;
      if (cap !== undefined && file.size > cap) {
        return setError(`A document has at most ${formatBytes(cap)}; ${file.name} has ${formatBytes(file.size)}.`);
      }
      const bytes = new Uint8Array(await file.arrayBuffer());
      document = { ...common, source_kind: "upload", filename: file.name, content_base64: bytesBase64(bytes) };
    } else if (adding === "path") {
      document = { ...common, source_kind: "path", path: path.trim() };
    } else {
      document = { ...common, source_kind: "text", content };
    }
    if (await apply(() => addKnowledgeDocument(projectId, document))) setAdding(null);
  };

  const caps = knowledge?.caps;
  const ready =
    title.trim() &&
    summary.trim() &&
    (adding === "text" ? content.trim() : adding === "upload" ? file : path.trim());

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
        <h2 id={`${id}-title`} className="text-sm font-semibold">Knowledge</h2>
        {knowledge && caps && (
          <span className="text-[11px] tabular-nums text-muted-foreground">
            {knowledge.count} / {caps.project_max_documents} documents · {formatBytes(knowledge.stored_bytes)} /{" "}
            {formatBytes(caps.project_max_bytes)} stored · index {knowledge.index_chars.toLocaleString()} /{" "}
            {caps.index_max_chars.toLocaleString()} chars
          </span>
        )}
      </div>
      <p className="text-xs text-muted-foreground">
        Documents this project&apos;s Planner, Workers and Reviewers read when they need them; their prompt lists each
        title and summary. Changes apply at once.
      </p>
      {loadError && <p role="alert" className="text-xs text-destructive">{loadError}</p>}
      {!knowledge && !loadError && <Loader2 className="h-4 w-4 animate-spin text-muted-foreground" aria-label="Loading" />}
      {knowledge &&
        (knowledge.documents.length ? (
          <ul className="rounded-md border border-border px-3">
            {knowledge.documents.map((doc) => (
              <DocumentRow key={doc.document_id} projectId={projectId} document={doc} busy={busy} onApply={apply} />
            ))}
          </ul>
        ) : (
          <p className="text-xs text-muted-foreground">No documents</p>
        ))}
      {knowledge && !adding && (
        <div className="flex flex-wrap items-center gap-2">
          {SOURCES.map(({ kind, label, icon: Icon }) => (
            <Button key={kind} type="button" variant="outline" size="sm" onClick={() => startAdding(kind)}>
              <Icon className="h-3.5 w-3.5" />
              {label}
            </Button>
          ))}
          <span className="text-xs text-muted-foreground">or drop a text file here</span>
        </div>
      )}
      {adding && (
        <form onSubmit={submit} className="space-y-2 rounded-md border border-border p-3">
          <p className="flex items-center gap-1.5 text-xs font-medium">
            <Plus className="h-3.5 w-3.5" aria-hidden="true" />
            New document · {SOURCE_LABEL[adding]}
          </p>
          <div className="grid gap-2 sm:grid-cols-2">
            <div className="space-y-1">
              <label htmlFor={`${id}-doc-title`} className="text-xs font-medium text-muted-foreground">Title *</label>
              <Input id={`${id}-doc-title`} value={title} maxLength={120} onChange={(e) => setTitle(e.target.value)} autoFocus />
            </div>
            <div className="space-y-1">
              <label htmlFor={`${id}-doc-summary`} className="text-xs font-medium text-muted-foreground">
                One-line summary *
              </label>
              <Input id={`${id}-doc-summary`} value={summary} maxLength={300} onChange={(e) => setSummary(e.target.value)} />
            </div>
          </div>
          {adding === "text" && (
            <div className="space-y-1">
              <label htmlFor={`${id}-doc-content`} className="text-xs font-medium text-muted-foreground">Content *</label>
              <textarea
                id={`${id}-doc-content`}
                value={content}
                onChange={(e) => setContent(e.target.value)}
                rows={6}
                spellCheck={false}
                className={TEXTAREA}
              />
            </div>
          )}
          {adding === "upload" && (
            <div className="space-y-1">
              <label htmlFor={`${id}-doc-file`} className="text-xs font-medium text-muted-foreground">File *</label>
              {file && (
                <p className="font-mono text-xs">
                  {file.name} · {formatBytes(file.size)}
                </p>
              )}
              <input
                id={`${id}-doc-file`}
                type="file"
                onChange={(e) => {
                  const chosen = e.target.files?.[0] ?? null;
                  setFile(chosen);
                  if (chosen && !title.trim()) setTitle(chosen.name);
                }}
                className="block w-full text-xs text-muted-foreground file:mr-3 file:rounded-md file:border file:border-border file:bg-transparent file:px-3 file:py-1.5 file:text-xs file:font-medium file:text-foreground"
              />
              <p className="text-xs text-muted-foreground">
                A text file (Markdown, plain text, code, CSV, JSON) of at most{" "}
                {caps ? formatBytes(caps.document_max_bytes) : "2 MB"}; you can also drop it on this section.
              </p>
            </div>
          )}
          {adding === "path" && (
            <div className="space-y-1">
              <label htmlFor={`${id}-doc-path`} className="text-xs font-medium text-muted-foreground">Path *</label>
              <Input
                id={`${id}-doc-path`}
                value={path}
                onChange={(e) => setPath(e.target.value)}
                placeholder="docs/architecture.md"
                className="font-mono"
              />
              <p className="text-xs text-muted-foreground">
                A file in the project&apos;s default directory, relative to it. Agents read it as it is when they run.
              </p>
            </div>
          )}
          {error && <p role="alert" className="text-xs text-destructive">{error}</p>}
          <div className="flex justify-end gap-2">
            <Button type="button" variant="outline" size="sm" onClick={() => setAdding(null)}>Cancel</Button>
            <Button type="submit" size="sm" disabled={busy || !ready}>
              {busy && <Loader2 className="h-3.5 w-3.5 animate-spin" />}
              Add document
            </Button>
          </div>
        </form>
      )}
      {!adding && error && <p role="alert" className="text-xs text-destructive">{error}</p>}
      {dragging && (
        <div
          aria-hidden="true"
          className="pointer-events-none absolute inset-0 flex items-center justify-center rounded-lg bg-background/70 text-sm font-medium text-primary"
        >
          Drop to add it as a document
        </div>
      )}
    </section>
  );
}
