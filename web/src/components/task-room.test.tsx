import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";
import { RoomMessageItem } from "@/components/task-room";
import type { RoomMessage } from "@/lib/api";

const message = (extra: Partial<RoomMessage>): RoomMessage => ({
  id: 7, from: "fe_rd@s2", to: ["backend_rd@s1", "planner"], kind: "info", body: "line one\nline two",
  at: "2026-10-17T08:00:00Z", cc_planner: false, answered: false, ...extra,
});

const text = (html: string) => html.replace(/<[^>]+>/g, "");
const render = (m: RoomMessage, known = true) =>
  renderToStaticMarkup(<RoomMessageItem message={m} known={known} onShow={() => {}} />);

describe("RoomMessageItem", () => {
  it("names the sender and the audience, and keeps the body's line breaks", () => {
    const html = render(message({}));
    expect(text(html)).toContain("fe_rd@s2 →to backend_rd@s1, planner");
    expect(html).toContain("line one\nline two");
    expect(text(render(message({ to: "all" })))).toContain("→to all");
  });

  it.each([
    ["an unanswered question", { kind: "question" }, "Unanswered"],
    ["the Planner's reply", { from: "planner", kind: "answer", reply_to: 3, via: "planner_reply" }, "Planner&#x27;s reply"],
    ["a Reviewer's question the Planner sees", { kind: "question", answered: true, cc_planner: true }, "Seen by the Planner"],
  ] as const)("marks %s", (_, extra, mark) => {
    expect(render(message(extra))).toContain(mark);
  });

  it("does not mark an answered question unanswered", () => {
    expect(render(message({ kind: "question", answered: true }))).not.toContain("Unanswered");
  });

  it("says in words why Looper wrote a notice", () => {
    const html = render(message({ from: "looper", via: "looper", kind: "escalation", reason: "consult_fallback" }));
    expect(text(html)).not.toContain("consult_fallback");
    expect(text(html)).toContain("went to the Planner");
  });

  it("links what a message answers only when that message is loaded", () => {
    expect(render(message({ kind: "answer", reply_to: 3 }), true)).toMatch(/<button[^>]*>re #3<\/button>/);
    expect(render(message({ kind: "answer", reply_to: 3 }), false)).not.toContain("<button");
  });
});
