import { useEffect, useState } from "react";
import { AlertTriangle } from "lucide-react";
import { fetchStatus, type SelfCheck } from "@/lib/api";

/** Says, on every page, when the server started without the Claude Code CLI or its login. */
export function ClaudeCheckBanner() {
  const [problems, setProblems] = useState<SelfCheck[]>([]);

  useEffect(() => {
    fetchStatus()
      .then((s) => setProblems((s.checks ?? []).filter((c) => c.id.startsWith("claude_") && c.status === "fail")))
      .catch(() => setProblems([]));
  }, []);

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
            Agents cannot run until this is fixed. To fix: {c.fix}; then restart{" "}
            <code className="font-mono text-xs">looper serve</code>.
          </span>
        </p>
      ))}
    </div>
  );
}
