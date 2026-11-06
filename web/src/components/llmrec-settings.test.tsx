// @vitest-environment happy-dom
import { act } from "react";
import { createRoot } from "react-dom/client";
import { afterEach, expect, it, vi } from "vitest";
import { LlmrecSettingsSection } from "./llmrec-settings";
import type { LlmrecSettings } from "@/lib/api";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
afterEach(() => vi.unstubAllGlobals());

function settingsOf(fields: Partial<LlmrecSettings> = {}): LlmrecSettings {
  return {
    enabled: true, include_oauth: false,
    retention_days: { value: 7, built_in: 7, source: "built-in", minimum: 1, maximum: 365 },
    max_total_mb: { value: 2048, built_in: 2048, source: "built-in", minimum: 102.4, maximum: 102400 },
    stored_bytes: 3 * 1024 * 1024, request_count: 12,
    proxy: { state: "stopped", port: null, pid: null, requests: 0, recorded: 0, gaps: 0,
      gap_reasons: {}, dropped: 0, write_errors: 0 }, ...fields,
  };
}

async function mount() {
  const container = document.createElement("div");
  document.body.append(container);
  const root = createRoot(container);
  await act(async () => root.render(<LlmrecSettingsSection />));
  return { container, button: (label: string) => Array.from(container.querySelectorAll<HTMLButtonElement>("button"))
    .find((item) => item.textContent === label)!,
  close: () => { act(() => root.unmount()); container.remove(); } };
}

async function enter(input: HTMLInputElement, value: string) {
  await act(async () => {
    Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value")!.set!.call(input, value);
    input.dispatchEvent(new Event("input", { bubbles: true }));
  });
}

it("confirms turning recording on, disables directly, and preserves saved values on failure", async () => {
  let settings = settingsOf({ enabled: false });
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
    ...settingsOf(),
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

it("shows the built-in limits, validates their ranges and converts GiB to MiB", async () => {
  let settings = settingsOf();
  const writes: unknown[] = [];
  let fail = false;
  vi.stubGlobal("fetch", vi.fn(async (path: string, init: RequestInit) => {
    if (path.endsWith("/update")) {
      if (fail) return Response.json({ status: "error", error: { code: "unavailable", message: "Save failed" } }, { status: 503 });
      const body = JSON.parse(String(init.body));
      writes.push(body);
      settings = { ...settings,
        retention_days: { ...settings.retention_days, value: body.retention_days ?? 7, source: body.retention_days === null ? "built-in" : "setting" },
        max_total_mb: { ...settings.max_total_mb, value: body.max_total_mb ?? 2048, source: body.max_total_mb === null ? "built-in" : "setting" } };
    }
    return Response.json({ status: "ok", result: settings });
  }));
  const page = await mount();
  try {
    const [days, size] = Array.from(page.container.querySelectorAll<HTMLInputElement>('input[type="number"]'));
    expect([days.value, days.min, days.max, days.step]).toEqual(["7", "1", "365", "1"]);
    expect([size.value, size.min, size.max]).toEqual(["2", "0.1", "100"]);
    expect(page.container.textContent).toContain("On by default");
    expect(page.container.textContent).toContain("Built-in: 7 days. Range: 1–365 days.");
    expect(page.container.textContent).toContain("Built-in: 2 GiB. Range: 0.1–100 GiB.");
    expect(page.container.textContent).toContain("3.0 MiB of 2048 MiB");
    expect(page.container.textContent).toContain("12 recorded requests");
    expect(page.button("Save limits").disabled).toBe(true);
    for (const invalid of ["0", "366", "1.5", ""]) {
      await enter(days, invalid);
      expect(page.button("Save limits").disabled).toBe(true);
    }
    await enter(days, "365");
    for (const invalid of ["0.09", "100.1", ""]) {
      await enter(size, invalid);
      expect(page.button("Save limits").disabled).toBe(true);
    }
    await enter(size, "0.1");
    await act(async () => page.button("Save limits").click());
    expect(writes).toEqual([{ retention_days: 365, max_total_mb: 102.4 }]);
    expect(page.button("Save limits").disabled).toBe(true);
    await enter(days, "30");
    await enter(size, "100");
    fail = true;
    await act(async () => page.button("Save limits").click());
    expect(page.container.querySelector('[role="alert"]')?.textContent).toContain("Save failed");
    expect([days.value, size.value]).toEqual(["30", "100"]);
    expect(page.container.textContent).toContain("3.0 MiB of 102.4 MiB");
    fail = false;
    await act(async () => page.button("Restore built-in limits").click());
    expect(writes.at(-1)).toEqual({ retention_days: null, max_total_mb: null });
    expect([days.value, size.value]).toEqual(["7", "2"]);
  } finally { page.close(); }
});

it("opens a local confirmation and only sends purge after explicit confirmation", async () => {
  let settings = settingsOf();
  const fetcher = vi.fn(async (path: string) => {
    if (path.endsWith("/purge")) {
      settings = settingsOf({ stored_bytes: 0, request_count: 0 });
      return Response.json({ status: "ok", result: { requests: 12, routes: 2 } });
    }
    return Response.json({ status: "ok", result: settings });
  });
  vi.stubGlobal("fetch", fetcher);
  const page = await mount();
  try {
    await act(async () => page.button("Delete recorded requests").click());
    expect(page.container.querySelector('[aria-label="Confirm recorded request deletion"]')?.textContent)
      .toContain("Permanently delete all 12 recorded requests");
    expect(document.activeElement).toBe(page.button("Cancel"));
    expect(fetcher.mock.calls.filter(([path]) => path.endsWith("/purge"))).toHaveLength(0);
    await act(async () => page.button("Cancel").click());
    expect(fetcher.mock.calls.filter(([path]) => path.endsWith("/purge"))).toHaveLength(0);
    await act(async () => page.button("Delete recorded requests").click());
    await act(async () => page.button("Delete permanently").click());
    const calls = fetcher.mock.calls as unknown as [string, RequestInit][];
    const purges = calls.filter(([path]) => path.endsWith("/purge"));
    expect(purges).toHaveLength(1);
    expect(JSON.parse(String(purges[0][1].body))).toEqual({ all: true });
    expect(calls.filter(([path]) => path.endsWith("/confirm"))).toHaveLength(0);
    expect(page.container.textContent).toContain("Deleted 12 requests and 2 routes.");
    expect(page.container.textContent).toContain("0.0 MiB of 2048 MiB · 0 recorded requests");
    expect(page.container.querySelector('[role="switch"]')?.getAttribute("aria-checked")).toBe("true");
  } finally { page.close(); }
});

it("keeps counts after a failed purge and requires a fresh confirmation to retry", async () => {
  vi.stubGlobal("fetch", vi.fn(async (path: string) => {
    if (path.endsWith("/purge")) return Response.json({ status: "error", error: { code: "unavailable", message: "Purge failed" } }, { status: 503 });
    return Response.json({ status: "ok", result: settingsOf() });
  }));
  const page = await mount();
  try {
    await act(async () => page.button("Delete recorded requests").click());
    await act(async () => page.button("Delete permanently").click());
    expect(page.container.querySelector('[role="alert"]')?.textContent).toContain("Purge failed");
    expect(page.container.textContent).toContain("12 recorded requests");
    expect(page.container.textContent).not.toContain("Deleted 12");
    expect(page.container.querySelector('[aria-label="Confirm recorded request deletion"]')).toBeNull();
    expect(page.button("Delete recorded requests").disabled).toBe(false);
  } finally { page.close(); }
});
