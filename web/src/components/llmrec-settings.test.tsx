// @vitest-environment happy-dom
import { act } from "react";
import { createRoot } from "react-dom/client";
import { afterEach, expect, it, vi } from "vitest";
import { LlmrecSettingsSection } from "./llmrec-settings";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
afterEach(() => vi.unstubAllGlobals());

it("saves recording and subscription separately, and retains saved values on failure", async () => {
  let settings = { enabled: false, include_oauth: false,
    proxy: { state: "stopped", requests: 0, recorded: 0, dropped: 0, write_errors: 0 } };
  const writes: unknown[] = [];
  let fail = false;
  vi.stubGlobal("fetch", vi.fn(async (path: string, init: RequestInit) => {
    if (path.endsWith("/update")) {
      if (fail) return Response.json({ status: "error", error: { code: "unavailable", message: "Save failed" } }, { status: 503 });
      const body = JSON.parse(String(init.body));
      writes.push(body);
      settings = { ...settings, ...body };
    }
    return Response.json({ status: "ok", result: settings });
  }));
  const container = document.createElement("div");
  document.body.append(container);
  const root = createRoot(container);
  try {
    await act(async () => root.render(<LlmrecSettingsSection />));
    const boxes = Array.from(container.querySelectorAll<HTMLInputElement>("input"));
    expect(boxes.map((box) => box.checked)).toEqual([false, false]);
    await act(async () => boxes[0].click());
    await act(async () => boxes[1].click());
    expect(writes).toEqual([{ enabled: true }, { include_oauth: true }]);
    expect(boxes.map((box) => box.checked)).toEqual([true, true]);
    fail = true;
    await act(async () => boxes[0].click());
    expect(boxes[0].checked).toBe(true);
    expect(container.querySelector('[role="alert"]')?.textContent).toContain("Save failed");
  } finally {
    act(() => root.unmount());
    container.remove();
  }
});
