// @vitest-environment happy-dom
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { SchedulerSection } from "@/components/scheduler-settings";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

const view = (min: number, source: string, timeout: number | null = null) => ({
  min_interval_seconds: { value: min, source, built_in: 300, minimum: 1, gated_value: Math.min(60, min) },
  run_timeout_seconds: { value: timeout, source: "built-in", built_in: null, minimum: 1, maximum: 2592000 },
});
const waits = (max = 604800, min = 300, source = "built-in") => ({
  max_wait_seconds: { value: max, default: 604800, min: 60, max: 2592000, source },
  min_check_seconds: { value: min, default: 300, min: 10, max: 86400, source },
});

describe("Settings → Scheduler", () => {
  let container: HTMLDivElement;
  let root: Root;
  let puts: unknown[];

  beforeEach(() => {
    puts = [];
    vi.stubGlobal("fetch", vi.fn(async (_url: RequestInfo | URL, init?: RequestInit) => {
      if (String(_url).endsWith("/wait-settings")) {
        if (init?.method !== "PUT") return Response.json(waits());
        const body = JSON.parse(String(init.body));
        puts.push(body);
        if (body.min_check_seconds === 0) return Response.json({ error: "min_check_seconds must be at least 10" }, { status: 400 });
        return Response.json(waits(body.max_wait_seconds ?? 604800, body.min_check_seconds ?? 300, "setting"));
      }
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

  it("keeps what is typed in the other field when one is saved", async () => {
    await act(async () => root.render(<SchedulerSection />));
    await flush();
    await typeInto(container.querySelectorAll("input")[1] as HTMLInputElement, "45");
    await typeInto(container.querySelectorAll("input")[0] as HTMLInputElement, "90");
    await act(async () => { (container.querySelector("button[type=submit]:not(:disabled)") as HTMLButtonElement).click(); });
    await flush();
    expect(puts).toEqual([{ min_interval_seconds: 90 }]);
    const inputs = container.querySelectorAll("input");
    expect((inputs[0] as HTMLInputElement).value).toBe("90");
    expect((inputs[1] as HTMLInputElement).value).toBe("45");
  });

  it("shows wait defaults and sources, saves seconds, resets to default and shows a refusal", async () => {
    await act(async () => root.render(<SchedulerSection />));
    await flush();
    const max = container.querySelector('input[id$="-max_wait_seconds"]') as HTMLInputElement;
    const min = container.querySelector('input[id$="-min_check_seconds"]') as HTMLInputElement;
    expect(max.value).toBe("604800");
    expect(min.value).toBe("300");
    expect(max.closest("form")?.textContent).toContain("Built-in value. Built-in default: 7 d");
    await typeInto(min, "600");
    await typeInto(max, "86400");
    await act(async () => { (max.closest("form")!.querySelector("button[type=submit]") as HTMLButtonElement).click(); });
    await flush();
    expect(puts).toEqual([{ max_wait_seconds: 86400 }]);
    expect(min.value).toBe("600");
    expect(max.closest("form")?.textContent).toContain("Saved here");
    await act(async () => { (max.closest("form")!.querySelector("button[type=button]") as HTMLButtonElement).click(); });
    await flush();
    expect(puts[1]).toEqual({ max_wait_seconds: null });
    await typeInto(min, "0");
    await act(async () => { (min.closest("form")!.querySelector("button[type=submit]") as HTMLButtonElement).click(); });
    await flush();
    expect(puts[2]).toEqual({ min_check_seconds: 0 });
    expect(container.querySelector("[role=alert]")?.textContent).toContain("min_check_seconds must be at least 10");
  });
});
