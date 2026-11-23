/** The Needs attention row's line when a required check failed. It opens that step's card. */
export function FailedChecksLine({ taskId, checks, stepId }: {
  taskId: string;
  checks: readonly string[];
  stepId?: string | null;
}) {
  const query = new URLSearchParams({ tab: "pipeline" });
  if (stepId) query.set("step", stepId);
  return (
    <a
      href={`#tasks/${encodeURIComponent(taskId)}?${query}`}
      onClick={(event) => event.stopPropagation()}
      className="mt-1 inline-flex items-center text-xs text-destructive underline decoration-dotted underline-offset-2 pointer-coarse:min-h-11"
    >
      required check failed: {checks.join(", ")}
    </a>
  );
}
