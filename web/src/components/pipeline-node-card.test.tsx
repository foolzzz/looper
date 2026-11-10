import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";
import { NodeCardView } from "@/components/pipeline-node-card";
import { type NodeCard, clipText, stepCard } from "@/lib/node-cards";
import type { TaskStep } from "@/lib/api";

const LONG = Array.from({ length: 8 }, (_, i) => `line ${i + 1}`).join("\n");
const card: NodeCard = {
  name: "s2",
  look: { label: "Done", border: "var(--color-success)", borderStyle: "solid", background: "bg", color: "fg" },
  roles: ["tester · work v2"],
  sections: [
    { kind: "text", title: "Instruction", text: "Write the test.\nRun it." },
    { kind: "facts", title: "Runs", facts: [{ label: "Worker", value: "worker-a", href: "#history/worker-a?task=t1" }] },
    { kind: "text", title: "Result", text: LONG, preview: clipText(LONG) },
  ],
};

describe("NodeCardView", () => {
  it("renders structured panel findings as readable text", () => {
    const step: TaskStep = {
      id: "dev", task_id: "t1", step_index: 0, step_key: "dev", kind: "work", instruction: "", status: "completed",
      acceptance_criteria: [], target_files: [], depends_on: [], review: false, role_key: null, role_version: null,
      review_roles: [], review_stage: null, review_position: null, retries: 0, worker_id: null, reviewer_id: null,
      created_at: "2026-01-01T00:00:00Z", updated_at: "2026-01-01T00:00:00Z", completed_at: null, started_at: null,
      result: {
        status: "completed", verify_passed: false,
        verify_findings: [{ member: "security", severity: "blocking", where: "src/server.py:24", text: "Validate the request." }],
      },
    };
    const panelCard = stepCard(step, { taskId: "t1", active: true, look: card.look,
      lookOf: () => card.look, stepsById: new Map([[step.id, step]]) });
    const result = renderToStaticMarkup(<NodeCardView card={panelCard} />);
    expect(result).toContain("security · blocking · src/server.py:24: Validate the request.");
    expect(result).not.toContain("[object Object]");
  });

  it.each(["Codex", "Custom runtime"])("draws an Agent fact with %s and the model", (runtime) => {
    const result = renderToStaticMarkup(<NodeCardView card={{ ...card, sections: [
      { kind: "facts", title: "Runs", facts: [{ label: "Agent", value: `${runtime} · gpt-5 · high` }] },
    ] }} />);
    expect(result).toContain("Agent");
    expect(result).toContain(`${runtime} · gpt-5 · high`);
  });

  it("draws the task's result as Markdown, and a failure's title in the destructive color", () => {
    const result = renderToStaticMarkup(<NodeCardView card={{ ...card, sections: [
      { kind: "text", title: "Error", text: "## Why it ended\nthe step cannot be done", markdown: true, tone: "destructive" },
    ] }} />);
    expect(result).toContain("<h2>Why it ended</h2>");
    expect(result).not.toContain("## Why");
    expect(result).toMatch(/<h4 class="[^"]*text-destructive[^"]*">Error<\/h4>/);
  });

  const html = renderToStaticMarkup(<NodeCardView card={card} />);

  it("draws the status in the node's colors, in words", () => {
    expect(html).toContain("border:1px solid var(--color-success)");
    expect(html).toContain(">Done<");
    expect(html).toContain("tester · work v2");
  });

  it("keeps the instruction's line breaks", () => {
    expect(html).toContain("whitespace-pre-wrap");
    expect(html).toContain("Write the test.\nRun it.");
  });

  it("links an agent to its history", () => {
    expect(html).toContain('href="#history/worker-a?task=t1"');
  });

  it("shows a long result clipped, with a toggle for the rest", () => {
    expect(html).toContain("line 4…");
    expect(html).not.toContain("line 5");
    expect(html).toContain('aria-expanded="false"');
    expect(html).toContain("Show more");
  });
});
