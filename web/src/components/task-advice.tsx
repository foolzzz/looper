import { Lightbulb } from "lucide-react";
import type { AdvisorConsult } from "@/lib/api";
import { Badge } from "@/components/ui/badge";
import { formatUsd } from "@/lib/usage";
import { ADOPTION_TEXT as ADOPTION, CHECKPOINT_UNFINISHED, SOURCE_TEXT as SOURCE, checkText } from "@/lib/consults";

/** How a consult ended, in words, with the badge colour that goes with it (the words always carry the state). */
const STATE: Record<AdvisorConsult["state"], { text: string; variant: "success" | "warning" | "destructive" | "outline" }> = {
  drafting: { text: "Planner writing the request", variant: "outline" },
  linting: { text: "Checking the request", variant: "outline" },
  asking: { text: "Asking the advisor", variant: "outline" },
  needs_info: { text: "Advisor asked back", variant: "outline" },
  answered: { text: "Answered", variant: "success" },
  blocked: { text: "Blocked: the request failed its checks", variant: "warning" },
  failed: { text: "Failed", variant: "destructive" },
  over_budget: { text: "Over budget", variant: "warning" },
};

/** The checks a request failed, by category and how many times; never what they matched. */
function checksText(consult: AdvisorConsult) {
  if (!consult.checks.length) return null;
  return consult.checks
    .map(({ category, count }) => `${checkText(category)}${count > 1 ? ` ×${count}` : ""}`)
    .join(", ");
}

/** Whether the attempt the advice was for passed; nothing until that attempt is judged. */
function unlockedText(consult: AdvisorConsult) {
  if (consult.unlocked == null) return null;
  return consult.unlocked
    ? `Attempt ${consult.attempt ?? "?"} passed after it`
    : `Attempt ${consult.attempt ?? "?"} did not pass`;
}

/** The marker of a step whose checkpoint consult ended without an answer: the step went on without it. */
export function CheckpointUnfinished() {
  return (
    <Badge variant="warning" title="Its checkpoint consult of the advisor ended without an answer; the step went on without it">
      {CHECKPOINT_UNFINISHED}
    </Badge>
  );
}

/** One step's consults of the advisor (or the plan's, under `title`): where each came from, how it ended, the
 * advisor's conclusions, what the Planner took of them and whether the attempt it was for passed. The whole of
 * each is on its card in the Pipeline. */
export function StepConsults({ consults, title = "Advisor" }: { consults: AdvisorConsult[]; title?: string }) {
  if (!consults.length) return null;
  return (
    <section aria-label={`${title}: consults`} className="mt-2 space-y-2 rounded-md border border-border px-3 py-2">
      <h4 className="flex items-center gap-1.5 text-xs font-medium text-muted-foreground">
        <Lightbulb className="h-3.5 w-3.5" aria-hidden="true" />
        {title}
      </h4>
      {consults.map((consult) => {
        const state = STATE[consult.state];
        const unlocked = unlockedText(consult);
        const checks = checksText(consult);
        const detail = [
          consult.failure && consult.state !== "answered" && consult.state !== "blocked" ? consult.failure : null,
          consult.status === "out_of_scope" ? "the advisor said it was beyond what it can judge" : null,
        ].filter(Boolean);
        return (
          <div key={consult.id} className="space-y-1">
            <div className="flex flex-wrap items-center gap-x-2 gap-y-1 text-xs">
              <span className="text-muted-foreground">
                {SOURCE[consult.source ?? ""] ?? "Consult"}
                {consult.attempt != null && ` · for attempt ${consult.attempt}`}
              </span>
              <Badge variant={state.variant}>
                {state.text}
                {detail.length > 0 && ` (${detail.join("; ")})`}
              </Badge>
              {consult.adoption && <span>{ADOPTION[consult.adoption]}</span>}
              {unlocked && <span className={consult.unlocked ? "text-success" : "text-muted-foreground"}>{unlocked}</span>}
              <span className="text-muted-foreground">
                {formatUsd(consult.spent_usd)}
                {consult.rounds > 1 && ` · ${consult.rounds} rounds`}
                {consult.returns > 0 && ` · request returned ${consult.returns}×`}
              </span>
            </div>
            {checks && <p className="text-xs text-muted-foreground">Checks the request failed: {checks}</p>}
            {consult.answers.map((answer) => (
              <p key={answer.question_id} className="text-sm">
                <span className="font-mono text-xs text-muted-foreground">{answer.question_id}</span>{" "}
                {answer.conclusion}
                {answer.confidence && (
                  <span className="text-xs text-muted-foreground"> · confidence {answer.confidence}</span>
                )}
              </p>
            ))}
          </div>
        );
      })}
    </section>
  );
}
