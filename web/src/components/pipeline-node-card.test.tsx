import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";
import { NodeCardView } from "@/components/pipeline-node-card";
import { type NodeCard, clipText } from "@/lib/node-cards";

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
