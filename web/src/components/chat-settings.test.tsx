// @vitest-environment happy-dom
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { ChatSettingsSection } from "@/components/chat-settings";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

describe("Chat settings", () => {
  let container: HTMLDivElement;
  let root: Root;
  let stored: { stop_enabled: boolean };
  let puts: unknown[];

  beforeEach(() => {
    stored = { stop_enabled: false };
    puts = [];
    vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      if (!["/api/v1/chat_settings/get", "/api/v1/chat_settings/update"].includes(String(input))) throw new Error(`Unexpected request: ${input}`);
      if (String(input) === "/api/v1/chat_settings/update") {
        const body = JSON.parse(String(init?.body));
        puts.push(body);
        stored = { stop_enabled: body.stop_enabled };
      }
      return Response.json({ status: "ok", result: stored });
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

  it("shows Stop off with an experimental note, and turns it on", async () => {
    await act(async () => root.render(<ChatSettingsSection />));
    const box = container.querySelector("input[type=checkbox]") as HTMLInputElement;
    expect(box.checked).toBe(false);
    expect(container.textContent).toContain("Experimental");
    await act(async () => box.click());
    expect(puts).toEqual([{ stop_enabled: true }]);
    expect((container.querySelector("input[type=checkbox]") as HTMLInputElement).checked).toBe(true);
  });
});
