import { AlertTriangle } from "lucide-react";
import { type SelfCheck } from "@/lib/api";
import { useStartupChecks } from "@/lib/use-startup-checks";

/** The checks of the runtimes' own logins Agent Profiles may use. */
const LOGIN_CHECKS = ["codex_login", "coco_login", "grok_login", "cursor_login", "ttadk"];

const shown = (c: SelfCheck) => (c.id.startsWith("claude_") || LOGIN_CHECKS.includes(c.id)) && c.status === "fail";

/** Says, on every page, when the server started without the Claude Code CLI or its login, or when a runtime
 * login or launcher an Agent Profile uses cannot run. */
export function ClaudeCheckBanner() {
  const problems = (useStartupChecks() ?? []).filter(shown);

  if (problems.length === 0) return null;
  return (
    <div role="alert" className="shrink-0 space-y-1 border-b border-destructive/50 bg-destructive/10 px-4 py-2 text-sm">
      {problems.map((c) => (
        <p key={c.id} className="flex gap-2">
          <AlertTriangle className="mt-0.5 h-4 w-4 shrink-0 text-destructive" aria-hidden="true" />
          <span>
            <span className="font-medium text-destructive">
              {c.label}: {c.detail}.
            </span>{" "}
            {LOGIN_CHECKS.includes(c.id) ? (
              <>Agent Profiles with the {c.label} cannot run until this is fixed. To fix: {c.fix}.</>
            ) : (
              <>
                Agents cannot run until this is fixed. To fix: {c.fix}; then restart{" "}
                <code className="font-mono text-xs">looper serve</code>.
              </>
            )}
          </span>
        </p>
      ))}
    </div>
  );
}
