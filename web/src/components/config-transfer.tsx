import { useEffect, useRef, useState } from "react";
import { AlertTriangle, CheckCircle2, Download, FileUp, Loader2, X } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Switch } from "@/components/ui/switch";
import {
  applyConfigImport,
  cancelConfigImport,
  configExportUrl,
  CONFIG_IMPORT_MAX_BYTES,
  uploadConfigImport,
  type ConfigImportAction,
  type ConfigImportPlan,
  type ConfigImportUpload,
} from "@/lib/api";
import { settingsTargetHash } from "@/lib/settings-target";
import { useFileDrop } from "@/lib/use-file-drop";
import { cn, formatBytes } from "@/lib/utils";

const errorText = (err: unknown) => (err instanceof Error ? err.message : String(err));

/** The file's sections, in the order an import takes them (loopx/agents/config_transfer.py SECTIONS). */
const SECTION_LABELS: Record<string, string> = {
  runtimes: "Runtimes",
  providers: "Providers",
  agents: "Agents",
  "settings.layers": "Settings layers",
  "settings.roles": "Role settings",
  channels: "Channels",
  catalog: "Catalog",
  installs: "Global installs",
  projects: "Projects",
  "project.layers": "Project layers",
  "project.items": "Project skills",
  "project.knowledge": "Project knowledge",
  "project.installs": "Project installs",
};
const sectionLabel = (section: string) => SECTION_LABELS[section] ?? section;
const ACTIONS: ConfigImportAction[] = ["added", "changed", "unchanged", "skipped"];
const TARGET_LINKS = { provider: "Edit provider", mcp: "Edit MCP server", channel: "Open channel" } as const;

const writes = (plan: ConfigImportPlan) =>
  Object.values(plan.counts).reduce((sum, counts) => sum + counts.added + counts.changed, 0);

/** Each section's counts, then the entries that change or are skipped, and the added ones folded. */
function PlanView({ plan }: { plan: ConfigImportPlan }) {
  const sections = Object.keys(plan.counts);
  const changed = plan.changes.filter((c) => c.action === "changed");
  const skipped = plan.changes.filter((c) => c.action === "skipped");
  const added = plan.changes.filter((c) => c.action === "added");
  if (!sections.length) return <p className="text-xs text-muted-foreground">The file holds nothing to import.</p>;
  return (
    <div className="space-y-3">
      <div className="max-w-2xl overflow-x-auto">
        <table className="w-full text-xs">
          <thead>
            <tr className="border-b border-border text-left text-muted-foreground">
              <th scope="col" className="py-1 pr-3 font-medium">Section</th>
              {ACTIONS.map((action) => (
                <th key={action} scope="col" className="px-2 py-1 text-right font-medium capitalize">{action}</th>
              ))}
            </tr>
          </thead>
          <tbody>
            {sections.map((section) => (
              <tr key={section} className="border-b border-border/60 last:border-0">
                <th scope="row" className="py-1 pr-3 text-left font-normal">{sectionLabel(section)}</th>
                {ACTIONS.map((action) => {
                  const n = plan.counts[section][action];
                  return (
                    <td
                      key={action}
                      className={cn(
                        "px-2 py-1 text-right tabular-nums",
                        n === 0 && "text-muted-foreground/60",
                        n > 0 && action === "skipped" && "text-warning",
                      )}
                    >
                      {n}
                    </td>
                  );
                })}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      {changed.length > 0 && (
        <div>
          <h4 className="text-xs font-medium">Changed · {changed.length}</h4>
          <ul className="mt-1 space-y-0.5 text-xs">
            {changed.map((c) => (
              <li key={`${c.section} ${c.key}`} className="break-words">
                <span className="text-muted-foreground">{sectionLabel(c.section)} · </span>
                <span className="font-mono">{c.key}</span>
                {c.fields && <span className="text-muted-foreground"> — {c.fields.join(", ")}</span>}
              </li>
            ))}
          </ul>
        </div>
      )}
      {skipped.length > 0 && (
        <div>
          <h4 className="flex items-center gap-1 text-xs font-medium text-warning">
            <AlertTriangle className="h-3.5 w-3.5" /> Skipped · {skipped.length}
          </h4>
          <ul className="mt-1 space-y-0.5 text-xs">
            {skipped.map((c) => (
              <li key={`${c.section} ${c.key}`} className="break-words">
                <span className="text-muted-foreground">{sectionLabel(c.section)} · </span>
                <span className="font-mono">{c.key}</span>
                <span className="text-muted-foreground">: {c.reason}</span>
              </li>
            ))}
          </ul>
        </div>
      )}
      {added.length > 0 && (
        <details className="text-xs">
          <summary className="cursor-pointer font-medium">Added · {added.length}</summary>
          <ul className="mt-1 max-h-48 space-y-0.5 overflow-y-auto">
            {added.map((c) => (
              <li key={`${c.section} ${c.key}`} className="break-words">
                <span className="text-muted-foreground">{sectionLabel(c.section)} · </span>
                <span className="font-mono">{c.key}</span>
              </li>
            ))}
          </ul>
        </details>
      )}
      {plan.notes.length > 0 && (
        <ul className="space-y-0.5 text-xs text-muted-foreground">
          {plan.notes.map((note) => (
            <li key={note}>Note: {note}</li>
          ))}
        </ul>
      )}
    </div>
  );
}

/** The secrets the file left out; once applied, each links to the form that takes it. */
function NeededSecrets({ plan }: { plan: ConfigImportPlan }) {
  if (!plan.need_secret.length) return null;
  return (
    <div className="rounded-md border border-warning/40 bg-warning/10 px-3 py-2">
      <h4 className="flex items-center gap-1 text-xs font-medium text-warning">
        <AlertTriangle className="h-3.5 w-3.5" />
        {plan.applied ? "Secrets to fill in" : "Secrets to fill in after applying"} · {plan.need_secret.length}
      </h4>
      <ul className="mt-1 space-y-1 text-xs">
        {plan.need_secret.map((need) => (
          <li key={`${need.section} ${need.key}`} className="flex flex-wrap items-baseline gap-x-2 break-words">
            <span>
              <span className="text-muted-foreground">{sectionLabel(need.section)} · </span>
              <span className="font-mono">{need.key}</span>
              <span className="text-muted-foreground"> — {need.names.join(", ")}</span>
            </span>
            {need.target && (
              <a
                href={settingsTargetHash(need.target.kind, need.target.id)}
                className="font-medium text-primary underline-offset-2 hover:underline focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring"
              >
                {TARGET_LINKS[need.target.kind]}
              </a>
            )}
          </li>
        ))}
      </ul>
      {plan.need_secret.some((need) => need.names.some((name) => name.startsWith("environment "))) && (
        <p className="mt-1 text-[11px] text-muted-foreground">
          An environment variable is set where the server starts, then the server is restarted.
        </p>
      )}
    </div>
  );
}

type Stage =
  | { step: "idle" }
  | { step: "uploading"; file: File }
  | { step: "preview"; file: File; upload: ConfigImportUpload; applying: boolean }
  | { step: "applied"; file: File; plan: ConfigImportPlan };

/** Upload a file, preview it, then apply it (or cancel); the server keeps the file at most an hour. */
function ImportPanel({ onApplied }: { onApplied: () => void }) {
  const [stage, setStage] = useState<Stage>({ step: "idle" });
  const [error, setError] = useState("");
  const input = useRef<HTMLInputElement>(null);
  // The import waiting on the server, cancelled when another file replaces it or this page goes away.
  const pending = useRef<string | null>(null);
  const drop = (importId: string | null) => {
    if (importId) void cancelConfigImport(importId).catch(() => undefined);
    pending.current = null;
  };
  useEffect(() => () => drop(pending.current), []);

  const choose = async (file: File) => {
    drop(pending.current);
    setError("");
    if (file.size > CONFIG_IMPORT_MAX_BYTES) {
      setStage({ step: "idle" });
      setError(
        `${file.name} is ${formatBytes(file.size)}; the dashboard imports at most ${formatBytes(CONFIG_IMPORT_MAX_BYTES)}. ` +
          "Import it with the command below.",
      );
      return;
    }
    setStage({ step: "uploading", file });
    try {
      const upload = await uploadConfigImport(file);
      pending.current = upload.import_id;
      setStage({ step: "preview", file, upload, applying: false });
    } catch (err) {
      setStage({ step: "idle" });
      setError(`${file.name}: ${errorText(err)}`);
    }
  };

  const apply = async () => {
    if (stage.step !== "preview") return;
    const count = writes(stage.upload.plan);
    if (
      !confirm(
        `Apply ${count} ${count === 1 ? "change" : "changes"} from ${stage.file.name} to this installation? ` +
          "Entries with the same name are updated in place; nothing missing from the file is removed.",
      )
    )
      return;
    setError("");
    setStage({ ...stage, applying: true });
    try {
      const { plan } = await applyConfigImport(stage.upload.import_id);
      pending.current = null;
      setStage({ step: "applied", file: stage.file, plan });
      onApplied();
    } catch (err) {
      setStage({ ...stage, applying: false });
      setError(errorText(err));
    }
  };
  const reset = () => {
    drop(pending.current);
    setStage({ step: "idle" });
    setError("");
  };

  const busy = stage.step === "uploading" || (stage.step === "preview" && stage.applying);
  const { dragging, dropProps } = useFileDrop((file) => void choose(file), !busy);
  return (
    <div {...dropProps} className="relative space-y-3 border-t border-border pt-3">
      <div className="flex flex-wrap items-center gap-2">
        <h3 className="text-sm font-medium">Import a configuration file</h3>
        <Button
          type="button"
          size="sm"
          variant="outline"
          className="ml-auto"
          disabled={busy}
          onClick={() => input.current?.click()}
        >
          <FileUp className="h-3.5 w-3.5" />
          {stage.step === "idle" ? "Choose file" : "Choose another file"}
        </Button>
        <input
          ref={input}
          type="file"
          accept=".json,application/json"
          className="hidden"
          tabIndex={-1}
          aria-hidden="true"
          onChange={(e) => {
            const chosen = e.target.files?.[0];
            e.target.value = "";
            if (chosen) void choose(chosen);
          }}
        />
      </div>

      {stage.step === "idle" && (
        <p className="text-xs text-muted-foreground">
          Choose or drop a file exported here or with <code className="text-foreground">loopx config export</code> (up
          to {formatBytes(CONFIG_IMPORT_MAX_BYTES)}). You see what it adds, changes and skips before anything is
          written.
        </p>
      )}

      {stage.step === "uploading" && (
        <p role="status" className="flex items-center gap-2 text-xs text-muted-foreground">
          <Loader2 className="h-3.5 w-3.5 animate-spin" />
          Uploading and checking {stage.file.name} ({formatBytes(stage.file.size)})…
        </p>
      )}

      {stage.step === "preview" && (
        <div className="space-y-3">
          <p className="text-xs">
            <span className="font-medium">Preview of {stage.file.name}</span>
            <span className="text-muted-foreground">
              {" · "}
              {formatBytes(stage.upload.bytes)}
              {stage.upload.file.exported_at && ` · exported ${new Date(stage.upload.file.exported_at).toLocaleString()}`}
              {stage.upload.file.loopx_version && ` by LoopX ${stage.upload.file.loopx_version}`}
              {stage.upload.file.secrets && ` · secrets ${stage.upload.file.secrets}`}
              {" · nothing written yet"}
            </span>
          </p>
          <PlanView plan={stage.upload.plan} />
          <NeededSecrets plan={stage.upload.plan} />
          <div className="flex flex-wrap items-center gap-2">
            <Button type="button" size="sm" onClick={apply} disabled={stage.applying || writes(stage.upload.plan) === 0}>
              {stage.applying && <Loader2 className="h-3.5 w-3.5 animate-spin" />}
              {writes(stage.upload.plan) === 0
                ? "Nothing to apply"
                : `Apply ${writes(stage.upload.plan)} ${writes(stage.upload.plan) === 1 ? "change" : "changes"}`}
            </Button>
            <Button type="button" size="sm" variant="ghost" onClick={reset} disabled={stage.applying}>
              <X className="h-3.5 w-3.5" />
              Cancel
            </Button>
            <span className="text-[11px] text-muted-foreground">
              Kept on the server until {new Date(stage.upload.expires_at).toLocaleTimeString()}.
            </span>
          </div>
        </div>
      )}

      {stage.step === "applied" && (
        <div className="space-y-3">
          <p role="status" className="flex items-center gap-1.5 text-xs font-medium text-success">
            <CheckCircle2 className="h-3.5 w-3.5" />
            Applied {stage.file.name}: {writes(stage.plan)} {writes(stage.plan) === 1 ? "entry" : "entries"} written.
          </p>
          <PlanView plan={stage.plan} />
          <NeededSecrets plan={stage.plan} />
          {stage.plan.counts.channels && stage.plan.counts.channels.added + stage.plan.counts.channels.changed > 0 && (
            <p className="text-xs text-muted-foreground">Restart the server for the channel changes to take effect.</p>
          )}
          <Button type="button" size="sm" variant="ghost" onClick={reset}>
            Done
          </Button>
        </div>
      )}

      {error && (
        <p role="alert" className="text-xs text-destructive">
          {error}
        </p>
      )}

      {dragging && (
        <div
          aria-hidden="true"
          className="pointer-events-none absolute inset-0 flex items-center justify-center rounded-md bg-background/80 text-sm font-medium text-primary"
        >
          Drop to preview importing it
        </div>
      )}
    </div>
  );
}

/**
 * Export the whole agent configuration as one file (loopx/agents/config_transfer.py), downloaded as the server
 * streams it, and import one: uploaded to the server's import route, previewed, then applied.
 */
export function ConfigTransferCard({ onImported }: { onImported: () => void }) {
  const [projects, setProjects] = useState(false);
  const [secrets, setSecrets] = useState(false);

  return (
    <div className="space-y-4 rounded-lg border border-border bg-card px-5 py-4">
      <div className="space-y-3">
        <div className="flex items-start gap-3">
          <Switch checked={projects} onChange={setProjects} label="Include projects" />
          <div className="text-sm">
            <div className="font-medium">Include projects</div>
            <p className="text-xs text-muted-foreground">
              Each project&apos;s settings, knowledge, own skills and installs. Its directory stays behind; path
              documents are re-pointed on the new machine.
            </p>
          </div>
        </div>
        <div className="flex items-start gap-3">
          <Switch checked={secrets} onChange={setSecrets} label="Include secrets" />
          <div className="text-sm">
            <div className="font-medium">Include secrets</div>
            <p className="text-xs text-muted-foreground">
              Off: MCP environment and header values and channel secrets are placeholders, and the file is safe to
              share.
            </p>
          </div>
        </div>
        {secrets && (
          <div className="flex items-start gap-2 rounded-md border border-amber-500/40 bg-amber-500/10 px-3 py-2 text-xs text-amber-700 dark:text-amber-400">
            <AlertTriangle className="mt-0.5 h-3.5 w-3.5 shrink-0" />
            The file will hold every secret in plain text. Keep it private and delete it once imported.
          </div>
        )}
      </div>
      <Button size="sm" onClick={() => window.location.assign(configExportUrl({ projects, secrets }))}>
        <Download className="h-3.5 w-3.5" />
        Export configuration
      </Button>
      <ImportPanel onApplied={onImported} />
      <div className="border-t border-border pt-3 text-xs text-muted-foreground">
        Or import from a shell: <code className="text-foreground">loopx config import &lt;file&gt;</code> previews,
        then add <code className="text-foreground">--apply</code>. It reads the file from disk, so it takes files of
        any size. Either way, nothing missing from the file is removed, and stored secrets are kept.
      </div>
    </div>
  );
}
