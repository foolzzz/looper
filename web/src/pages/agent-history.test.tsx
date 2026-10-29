// @vitest-environment happy-dom
import { act } from "react";
import { createRoot } from "react-dom/client";
import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it, vi } from "vitest";
import { RunCard, sourceName } from "@/pages/agent-history";
import { runProfile, type LlmRecord } from "@/lib/api";

function render(mode?: "append" | "replace", metadata: object = {}, extra: object = {}) {
  const record = {
    id: 1, agent_id: "worker", status: "completed", created_at: "2026-10-18T10:00:00Z", duration_ms: 10,
    metadata: { presentation: { system_prompt: { text: "Looper prompt", mode: mode ?? null, sha256: null } }, ...metadata }, ...extra,
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

  it.each(["append", undefined] as const)("shows only the evidenced mode for %s runs", (mode) => {
    const html = render(mode);
    expect(html).toContain(mode ? "appended by Looper" : "mode unknown");
    expect(html).toContain("hash unknown");
    expect(html).not.toContain("replaced default");
  });
  it("uses normalized sources and canonical Profiles without interpreting historical labels", () => {
    expect(sourceName({ code: null, label: "Manager override" })).toBe("Manager override");
    expect(sourceName({ code: "planner_step", label: null })).toBe("Planner's choice for this step");
    expect(sourceName({ code: null, label: null })).toBe("unknown");
    expect(runProfile({ metadata: { profile: { name: "Custom runtime" } } } as LlmRecord)?.name).toBe("Custom runtime");
  });
  it("keeps absent-transcript output and the current waiting state", () => {
    expect(render(undefined, {}, { output_text: "retained evidence" })).toContain("retained evidence");
    expect(render(undefined, {}, { status: "running" })).toContain("Waiting for the first event");
  });
  it("renders unknown library evidence and historical labels from the DTO", async () => {
    const host = document.createElement("div"), root = createRoot(host);
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(Response.json({ status: "ok", result: { events: [] } })));
    const record = { id: 1, status: "completed", created_at: "2026-10-18T10:00:00Z", metadata: {
      profile: { source: "project" },
      presentation: { sources: { profile: { code: null, label: "Manager override" } },
        system_prompt: { text: "stored evidence", mode: null, sha256: null },
        library: { items: [{ kind: "skill", name: "old", plugin: null, scope: null, project_id: null, sha256: null }],
          sha256: null, error: null, project_error: "unavailable" } },
    } } as LlmRecord;
    try {
      await act(async () => { root.render(<RunCard initial={record} defaultOpen />); });
      await act(async () => { for (const button of host.querySelectorAll("button")) {
        if (/System prompt|Plugins & skills/.test(button.textContent ?? "")) button.click();
      } });
      for (const value of ["unknown plugin:old", "Unknown scope or kind", "hash unknown", "Manager override", "mode unknown", "stored evidence", "unknown runtime", "unknown provider"])
        expect(host.textContent).toContain(value);
      expect(host.textContent).not.toContain("Skills (global)");
    } finally { await act(async () => root.unmount()); vi.unstubAllGlobals(); }
  });
});
