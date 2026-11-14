// @vitest-environment happy-dom
import { act } from "react";
import { createRoot } from "react-dom/client";
import { renderToStaticMarkup } from "react-dom/server";
import { afterEach, describe, expect, it, vi } from "vitest";
import {
  ManagerSessionProfile,
  ManagerSessionProfileView,
  confirmText,
  sourceText,
} from "@/components/manager-session-profile";
import { setManagerSessionProfile, type ManagerProfileChoice, type ManagerSessionView } from "@/lib/api";

afterEach(() => vi.unstubAllGlobals());

const choice = (over: Partial<ManagerProfileChoice> = {}): ManagerProfileChoice => ({
  number: 1, profile_id: "p1", name: "Alpha", runtime: "claude-code", provider: "anthropic", model: "m",
  effect: "current", problem: null, ...over,
});

const view = (over: Partial<ManagerSessionView> = {}): ManagerSessionView => ({
  session: { active: true, runtime: "claude-code", profile_id: "p1" },
  selected: null,
  effective: {
    profile: {
      profile_id: "p1", name: "Effective Agent", effort: null, runtime: "claude-code", provider: "anthropic", model: "opus",
    },
    source: "session",
  },
  problem: null,
  follow_settings: { name: "Settings Agent", effect: "new_session" },
  choices: [choice(), choice({ number: 2, profile_id: "p2", name: "Beta", problem: "runtime missing" })],
  ...over,
});

describe("manager session profile", () => {
  it.each([["session", "this session"], ["role", "Settings"], ["system", "Settings"], ["builtin", "Settings"]] as const)(
    "sourceText %s", (source, text) => {
      expect(sourceText({ effective: { profile: null, source } })).toBe(text);
    });

  it("confirms only for new_session", () => {
    expect(confirmText(choice({ effect: "new_session" }))).toContain("starts a new session on Alpha");
    for (const effect of ["current", "resumes", "starts"] as const) expect(confirmText(choice({ effect }))).toBeNull();
  });

  it("confirms following Settings when it starts a new session", () => {
    expect(confirmText(view().follow_settings!)).toContain("new session on Settings Agent");
  });

  it("renders the profile, options and problems", () => {
    const html = renderToStaticMarkup(
      <ManagerSessionProfileView view={view({ problem: "settings broken" })} busy={false} error={null} onSelect={() => {}} />,
    );
    expect(html).toContain("Effective Agent — claude-code · anthropic / opus");
    expect(html).not.toContain("undefined");
    expect(html).toContain("this session");
    expect(html).toContain("Follow Settings");
    expect(html).toContain("1. Alpha");
    expect(html).toMatch(/<option[^>]*disabled[^>]*>2\. Beta \(runtime missing\)/);
    expect(html).toContain("settings broken");
  });

  it("sends a null profile_id to follow Settings", async () => {
    const fetch = vi.fn().mockResolvedValue(Response.json({ status: "ok", result: { ...view(), effect: "current", text: "ok" } }));
    vi.stubGlobal("fetch", fetch);
    await setManagerSessionProfile(null);
    expect(fetch.mock.calls[0][0]).toBe("/api/v1/manager_session/select_profile");
    expect(fetch.mock.calls[0][1].method).toBe("POST");
    expect(JSON.parse(fetch.mock.calls[0][1].body)).toEqual({ profile_id: null });
  });
});

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

describe("manager session focus refresh", () => {
  it("coalesces repeated focus while a read is pending", async () => {
    vi.useFakeTimers();
    Object.defineProperty(document, "visibilityState", { configurable: true, value: "visible" });
    const fetcher = vi.fn(() => new Promise<Response>(() => {}));
    vi.stubGlobal("fetch", fetcher);
    const container = document.createElement("div");
    const root = createRoot(container);
    try {
      await act(async () => root.render(<ManagerSessionProfile />));
      await act(async () => vi.advanceTimersByTimeAsync(30_000));
      for (let i = 0; i < 5; i++) {
        await act(async () => {
          window.dispatchEvent(new Event("focus"));
          await vi.advanceTimersByTimeAsync(2000);
        });
      }
      expect(fetcher).toHaveBeenCalledTimes(1);
    } finally {
      act(() => root.unmount());
      vi.useRealTimers();
    }
  });

  it.each([false, true])("throttles focus reads after a failed read=%s and ignores inactive or hidden chat", async (failFirst) => {
    vi.useFakeTimers();
    Object.defineProperty(document, "visibilityState", { configurable: true, value: "visible" });
    const fetcher = vi.fn(async () => Response.json({ status: "ok", result: view() }));
    vi.stubGlobal("fetch", fetcher);
    if (failFirst) fetcher.mockRejectedValueOnce(new Error("Offline"));
    const root = createRoot(document.createElement("div"));
    try {
      await act(async () => root.render(<ManagerSessionProfile active={false} />));
      await act(async () => window.dispatchEvent(new Event("focus")));
      expect(fetcher).not.toHaveBeenCalled();
      await act(async () => root.render(<ManagerSessionProfile active />));
      expect(fetcher).toHaveBeenCalledTimes(1);
      await act(async () => {
        await vi.advanceTimersByTimeAsync(29_999);
        window.dispatchEvent(new Event("focus"));
      });
      expect(fetcher).toHaveBeenCalledTimes(1);
      await act(async () => {
        await vi.advanceTimersByTimeAsync(1);
        window.dispatchEvent(new Event("focus"));
      });
      expect(fetcher).toHaveBeenCalledTimes(2);
      Object.defineProperty(document, "visibilityState", { configurable: true, value: "hidden" });
      await act(async () => {
        await vi.advanceTimersByTimeAsync(30_000);
        window.dispatchEvent(new Event("focus"));
      });
      expect(fetcher).toHaveBeenCalledTimes(2);
      await act(async () => root.render(<ManagerSessionProfile active={false} />));
      Object.defineProperty(document, "visibilityState", { configurable: true, value: "visible" });
      await act(async () => window.dispatchEvent(new Event("focus")));
      expect(fetcher).toHaveBeenCalledTimes(2);
    } finally {
      act(() => root.unmount());
      vi.useRealTimers();
    }
  });
});
