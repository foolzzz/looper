import { useEffect, useId, useState } from "react";
import { AlertCircle, FileText, FolderOpen, Loader2, Plus, Trash2, Upload } from "lucide-react";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import {
  addKnowledgeDocument,
  deleteKnowledgeDocument,
  fetchKnowledge,
  type KnowledgeDocument,
  type KnowledgeSourceKind,
  type NewKnowledgeDocument,
  type ProjectKnowledge,
} from "@/lib/api";
import { formatBytes } from "@/lib/utils";

const SOURCES: { kind: KnowledgeSourceKind; label: string; icon: typeof FileText }[] = [
  { kind: "text", label: "Text", icon: FileText },
  { kind: "upload", label: "Upload", icon: Upload },
  { kind: "path", label: "Project file", icon: FolderOpen },
];

const SOURCE_LABEL: Record<KnowledgeSourceKind, string> = { text: "Text", upload: "Upload", path: "Project file" };

/** A file's bytes as base64, in chunks small enough for String.fromCharCode. */
async function fileBase64(file: File) {
  const bytes = new Uint8Array(await file.arrayBuffer());
  let binary = "";
  for (let i = 0; i < bytes.length; i += 0x8000) binary += String.fromCharCode(...bytes.subarray(i, i + 0x8000));
  return btoa(binary);
}

function DocumentRow({
  document,
  busy,
  onDelete,
}: {
  document: KnowledgeDocument;
  busy: boolean;
  onDelete: () => void;
}) {
  const [confirming, setConfirming] = useState(false);
  return (
    <li className="space-y-1 border-t border-border py-2 first:border-t-0">
      <div className="flex items-start gap-2">
        <div className="min-w-0 flex-1 space-y-0.5">
          <p className="truncate text-sm font-medium">{document.title}</p>
          <p className="break-words text-xs text-muted-foreground">{document.summary}</p>
          <p className="flex flex-wrap items-center gap-x-2 gap-y-1 font-mono text-[11px] text-muted-foreground">
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
          </p>
        </div>
        {!confirming && (
          <Button
            type="button"
            variant="ghost"
            size="sm"
            onClick={() => setConfirming(true)}
            aria-label={`Delete ${document.title}`}
            className="shrink-0 text-muted-foreground hover:bg-destructive/10 hover:text-destructive"
          >
            <Trash2 className="h-3.5 w-3.5" />
          </Button>
        )}
      </div>
      {confirming && (
        <div className="flex flex-wrap items-center justify-end gap-2">
          <span className="text-xs text-muted-foreground">Delete this document?</span>
          <Button type="button" variant="destructive" size="sm" onClick={onDelete} disabled={busy}>
            Delete
          </Button>
          <Button type="button" variant="ghost" size="sm" onClick={() => setConfirming(false)}>
            Cancel
          </Button>
        </div>
      )}
    </li>
  );
}

/**
 * The project's knowledge, managed in the project editor: documents its Planner, Workers and Reviewers read when
 * they need them. Adding and deleting apply at once, apart from the editor's Save.
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
      .catch((err) => alive && setLoadError(err instanceof Error ? err.message : String(err)));
    return () => {
      alive = false;
    };
  }, [projectId]);

  const startAdding = (kind: KnowledgeSourceKind) => {
    setAdding(kind);
    setTitle("");
    setSummary("");
    setContent("");
    setPath("");
    setFile(null);
    setError("");
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
      document = { ...common, source_kind: "upload", filename: file.name, content_base64: await fileBase64(file) };
    } else if (adding === "path") {
      document = { ...common, source_kind: "path", path: path.trim() };
    } else {
      document = { ...common, source_kind: "text", content };
    }
    setBusy(true);
    try {
      setKnowledge(await addKnowledgeDocument(projectId, document));
      setAdding(null);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setBusy(false);
    }
  };

  const remove = async (documentId: string) => {
    setBusy(true);
    setError("");
    try {
      setKnowledge(await deleteKnowledgeDocument(projectId, documentId));
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setBusy(false);
    }
  };

  const caps = knowledge?.caps;
  const ready =
    title.trim() &&
    summary.trim() &&
    (adding === "text" ? content.trim() : adding === "upload" ? file : path.trim());

  return (
    <section aria-labelledby={`${id}-title`} className="space-y-2 border-t border-border pt-3">
      <div className="flex flex-wrap items-baseline justify-between gap-x-3 gap-y-1">
        <h3 id={`${id}-title`} className="text-xs font-medium text-muted-foreground">Knowledge</h3>
        {knowledge && caps && (
          <span className="text-[11px] tabular-nums text-muted-foreground">
            {knowledge.count} / {caps.project_max_documents} documents · {formatBytes(knowledge.stored_bytes)} /{" "}
            {formatBytes(caps.project_max_bytes)} stored · index {knowledge.index_chars.toLocaleString()} /{" "}
            {caps.index_max_chars.toLocaleString()} chars
          </span>
        )}
      </div>
      <p className="text-xs text-muted-foreground">
        Documents this project's Planner, Workers and Reviewers read when they need them; their prompt lists each
        title and summary. Changes here apply at once, without Save.
      </p>
      {loadError && <p role="alert" className="text-xs text-destructive">{loadError}</p>}
      {!knowledge && !loadError && <Loader2 className="h-4 w-4 animate-spin text-muted-foreground" aria-label="Loading" />}
      {knowledge &&
        (knowledge.documents.length ? (
          <ul className="rounded-md border border-border px-3">
            {knowledge.documents.map((doc) => (
              <DocumentRow key={doc.document_id} document={doc} busy={busy} onDelete={() => remove(doc.document_id)} />
            ))}
          </ul>
        ) : (
          <p className="text-xs text-muted-foreground">No documents</p>
        ))}
      {knowledge && !adding && (
        <div className="flex flex-wrap gap-2">
          {SOURCES.map(({ kind, label, icon: Icon }) => (
            <Button key={kind} type="button" variant="outline" size="sm" onClick={() => startAdding(kind)}>
              <Icon className="h-3.5 w-3.5" />
              {label}
            </Button>
          ))}
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
                className="w-full resize-y rounded-md border border-input bg-transparent px-3 py-2 font-mono text-xs leading-relaxed text-foreground shadow-sm focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring"
              />
            </div>
          )}
          {adding === "upload" && (
            <div className="space-y-1">
              <label htmlFor={`${id}-doc-file`} className="text-xs font-medium text-muted-foreground">File *</label>
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
                {caps ? formatBytes(caps.document_max_bytes) : "2 MB"}.
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
                A file in the project's default directory, relative to it. Agents read it as it is when they run.
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
    </section>
  );
}
