import { useState } from "react";
import { AlertTriangle, Download } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Switch } from "@/components/ui/switch";
import { configExportUrl } from "@/lib/api";

/**
 * Export the whole agent configuration as one file (loopx/agents/config_transfer.py), downloaded as the server
 * streams it. Importing is the CLI's: a file with the catalog's files is usually over the 8 MiB a request may carry.
 */
export function ConfigTransferCard() {
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
      <div className="border-t border-border pt-3 text-xs text-muted-foreground">
        To import on another machine, run <code className="text-foreground">loopx config import &lt;file&gt;</code>{" "}
        to preview what it adds and changes, then add <code className="text-foreground">--apply</code>. Nothing missing
        from the file is removed, and stored secrets are kept. Importing is not offered here: with the catalog&apos;s
        files the file is usually over the 8 MiB a request to this server may carry.
      </div>
    </div>
  );
}
