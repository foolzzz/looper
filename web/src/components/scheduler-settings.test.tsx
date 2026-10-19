// @vitest-environment happy-dom
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { SchedulerSection } from "@/components/scheduler-settings";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

const view = (min: number, source: string, timeout = 3600) => ({
  min_interval_seconds: { value: min, source, built_in: 300, minimum: 1, gated_value: Math.min(60, min) },
  run_timeout_seconds: { value: timeout, source: "built-in", built_in: 3600, minimum: 1, maximum: 2592000 },
});

describe("Settings → Scheduler", () => {
  let container: HTMLDivElement;
  let root: Root;
  let puts: unknown[];

  beforeEach(() => {
    puts = [];
    vi.stubGlobal("fetch", vi.fn(async (_url: RequestInfo | URL, init?: RequestInit) => {
      if (init?.method === "PUT") {
        const body = JSON.parse(String(init.body));
        puts.push(body);
        if (body.min_interval_seconds === 0) return Response.json({ error: "bad value" }, { status: 400 });
        return Response.json(view(body.min_interval_seconds ?? 120, "setting"));
      }
      return Response.json(view(120, "env"));
    }));
    container = document.createElement("div");
    document.body.append(container);
    root = createRoot(container);
  });

  afterEach(() => {
    act(() => root.unmount());
    container.remove();
    vi.unstubAllGlobals();
  });

  const flush = () => act(async () => { await new Promise((r) => setTimeout(r, 0)); });
  const typeInto = (input: HTMLInputElement, text: string) =>
    act(async () => {
      Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value")!.set!.call(input, text);
      input.dispatchEvent(new Event("input", { bubbles: true }));
    });

  it("shows each effective value with its source, saves one value and shows a refusal", async () => {
    await act(async () => root.render(<SchedulerSection />));
    await flush();
    const inputs = container.querySelectorAll("input");
    expect((inputs[0] as HTMLInputElement).value).toBe("120");
    expect(container.textContent).toContain("deprecated LOOPER_SCHEDULE_MIN_INTERVAL_SECONDS");
    expect(container.textContent).toContain("Built-in value");
    const save = container.querySelector("button[type=submit]") as HTMLButtonElement;
    expect(save.disabled).toBe(true);

    await typeInto(inputs[0] as HTMLInputElement, "90");
    expect(save.disabled).toBe(false);
    await act(async () => { save.click(); });
    await flush();
    expect(puts).toEqual([{ min_interval_seconds: 90 }]);
    expect(container.textContent).toContain("Saved here");
    expect(container.textContent).toContain("Use the built-in (300 s)");

    await typeInto(container.querySelectorAll("input")[0] as HTMLInputElement, "0");
    await act(async () => { (container.querySelector("button[type=submit]") as HTMLButtonElement).click(); });
    await flush();
    expect(container.querySelector("[role=alert]")?.textContent).toContain("bad value");
  });
});
