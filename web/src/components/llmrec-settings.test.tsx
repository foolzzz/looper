// @vitest-environment happy-dom
import { act } from "react";
import { createRoot } from "react-dom/client";
import { afterEach, expect, it, vi } from "vitest";
import { LlmrecSettingsSection } from "./llmrec-settings";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
afterEach(() => vi.unstubAllGlobals());

it("confirms turning recording on, disables directly, and preserves saved values on failure", async () => {
  let settings = { enabled: false, include_oauth: false,
    proxy: { state: "stopped", port: null, requests: 0, recorded: 0, dropped: 0, write_errors: 0 } };
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
  const button = (label: string) => Array.from(container.querySelectorAll<HTMLButtonElement>("button"))
    .find((item) => item.textContent === label)!;
  const recording = () => container.querySelector<HTMLButtonElement>('[role="switch"]')!;
  try {
    await act(async () => root.render(<LlmrecSettingsSection />));
    expect(recording().getAttribute("aria-checked")).toBe("false");
    await act(async () => recording().click());
    expect(writes).toEqual([]);
    expect(container.textContent).toContain("Every model request of agents started from now on (prompts, code, tool output, model replies) will be stored in this Looper's database. Only you can read them here or through the CLI. Running agents are not affected until they restart.");
    await act(async () => button("Cancel").click());
    expect(writes).toEqual([]);
    await act(async () => recording().click());
    await act(async () => button("Turn on recording").click());
    const oauth = container.querySelector<HTMLInputElement>('input[type="checkbox"]')!;
    await act(async () => oauth.click());
    expect(writes).toEqual([{ enabled: true }, { include_oauth: true }]);
    expect(recording().getAttribute("aria-checked")).toBe("true");
    expect(oauth.checked).toBe(true);
    await act(async () => recording().click());
    expect(writes.at(-1)).toEqual({ enabled: false });
    expect(container.textContent).not.toContain("Every model request of agents started from now on");
    expect(container.textContent).toContain("Stored requests stay in the database");
    fail = true;
    await act(async () => oauth.click());
    expect(oauth.checked).toBe(true);
    expect(container.querySelector('[role="alert"]')?.textContent).toContain("Save failed");
  } finally {
    act(() => root.unmount());
    container.remove();
  }
});

it.each(["running", "stopped", "unavailable", "outdated"])("shows proxy %s, port and all counters", async (state) => {
  vi.stubGlobal("fetch", vi.fn(async () => Response.json({ status: "ok", result: {
    enabled: true, include_oauth: false,
    proxy: { state, port: 32123, requests: 12, recorded: 9, dropped: 2, write_errors: 1 },
  } })));
  const container = document.createElement("div");
  const root = createRoot(container);
  try {
    await act(async () => root.render(<LlmrecSettingsSection />));
    expect(container.textContent).toContain(state);
    expect(container.textContent).toContain("32123");
    const facts = Array.from(container.querySelectorAll("dl > div"))
      .map((entry) => [entry.querySelector("dt")?.textContent, entry.querySelector("dd")?.textContent]);
    expect(facts).toEqual([["Requests", "12"], ["Recorded", "9"], ["Dropped", "2"], ["Write errors", "1"]]);
  } finally {
    act(() => root.unmount());
  }
});
