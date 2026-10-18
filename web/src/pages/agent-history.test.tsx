import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";
import { RunCard } from "@/pages/agent-history";
import type { LlmRecord } from "@/lib/api";

function render(mode?: "append" | "replace") {
  const record = {
    id: 1, agent_id: "worker", status: "completed", created_at: "2026-10-18T10:00:00Z", duration_ms: 10,
    metadata: { system_prompt: "Looper prompt", ...(mode ? { system_prompt_mode: mode } : {}) },
  } as LlmRecord;
  return renderToStaticMarkup(<RunCard initial={record} defaultOpen />);
}

describe("run system prompt mode", () => {
  it("shows a replaced default badge with an explanation for replace runs", () => {
    const html = render("replace");
    expect(html).toContain("replaced default");
    expect(html).toContain('title="Looper replaced the runtime&#x27;s default system prompt for this run"');
    expect(html).not.toContain("appended by Looper");
  });

  it.each(["append", undefined] as const)("keeps the appended by Looper fallback for %s runs", (mode) => {
    const html = render(mode);
    expect(html).toContain("appended by Looper");
    expect(html).not.toContain("replaced default");
  });
});
