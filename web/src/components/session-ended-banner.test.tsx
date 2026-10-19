// @vitest-environment happy-dom
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import App from "@/App";
import { SessionEndedBanner } from "@/components/session-ended-banner";
import { SignOut } from "@/components/sign-out";
import { installSessionGuard, resetSessionGuard, SessionEndedError } from "@/lib/session";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

const OVERVIEW = {
  tasks: { total: 0, other_statuses: {} },
  agents: { manager: null, planners: [], workers: [], reviewers: [] },
  projects: [],
  idle_projects: 0,
  spend_today: { since: "", provider_priced_usd: 0, equivalent_usd: 0, runs: 0, running: 0, unpriced_runs: 0 },
  health: { unhealthy: [] },
  schedules: { total: 0, enabled: 0, next: [], failing: [], skipped_today: {} },
};

describe("session ended screen and sign out", () => {
  let container: HTMLDivElement;
  let root: Root;
  let status: Response;
  let calls: string[];
  let unauthorized: boolean;

  beforeEach(() => {
    calls = [];
    unauthorized = false;
    status = Response.json({});
    resetSessionGuard();
    vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input);
      calls.push(url);
      if (unauthorized && url.startsWith("/api/")) return new Response("{}", { status: 401 });
      if (url === "/api/status") return status.clone();
      if (url === "/login") return new Response("{}", { status: 401 });
      if (url === "/api/overview") return Response.json(OVERVIEW);
      return Response.json({});
    }));
    container = document.createElement("div");
    document.body.append(container);
    root = createRoot(container);
  });

  afterEach(() => {
    act(() => root.unmount());
    container.remove();
    vi.useRealTimers();
    vi.unstubAllGlobals();
  });

  it("shows one banner with a Reload button after an API call gets 401", async () => {
    installSessionGuard();
    await act(async () => root.render(<SessionEndedBanner />));
    expect(container.textContent).toBe("");
    unauthorized = true;
    await act(async () => {
      await expect(fetch("/api/overview")).rejects.toBeInstanceOf(SessionEndedError);
    });
    expect(container.querySelectorAll('[role="alert"]')).toHaveLength(1);
    expect(container.textContent).toContain("Your session ended. Reload to sign in again.");
    expect(container.querySelector("button")?.textContent).toBe("Reload");
    await expect(fetch("/api/overview")).rejects.toBeInstanceOf(SessionEndedError);
    expect(calls).toEqual(["/api/overview"]); // the call after the end sent no request
  });

  it("settles every concurrent call that gets 401", async () => {
    installSessionGuard();
    unauthorized = true;
    const results = await Promise.allSettled(Array.from({ length: 20 }, () => fetch("/api/overview")));
    expect(results.every((r) => r.status === "rejected" && r.reason instanceof SessionEndedError)).toBe(true);
  });

  it("unmounts the routed app when the session ends: no more calls, no widget error", async () => {
    vi.useFakeTimers();
    window.location.hash = "#dashboard";
    installSessionGuard();
    await act(async () => root.render(<App />));
    await act(async () => vi.advanceTimersByTimeAsync(10_000));
    expect(calls.length).toBeGreaterThan(0);
    expect(container.textContent).toContain("Dashboard");

    unauthorized = true;
    await act(async () => vi.advanceTimersByTimeAsync(10_000));
    expect(container.textContent).toContain("Your session ended. Reload to sign in again.");
    expect(container.textContent).not.toContain("Connection failed");
    expect(container.querySelector("aside")).toBeNull();

    const before = calls.length;
    await act(async () => vi.advanceTimersByTimeAsync(60_000));
    expect(calls).toHaveLength(before);
    expect(container.textContent).not.toContain("Connection failed");
  });

  it("ignores a 401 that is not an API call", async () => {
    installSessionGuard();
    await act(async () => root.render(<SessionEndedBanner />));
    await act(async () => {
      await fetch("/login");
    });
    expect(container.textContent).toBe("");
  });

  it.each([[true, 1], [false, 0]])("sign out with can_sign_out=%s shows %i control", async (can, count) => {
    status = Response.json({ can_sign_out: can });
    await act(async () => root.render(<SignOut />));
    expect(container.querySelectorAll('form[method="post"][action="/logout"]')).toHaveLength(count);
    if (count) expect(container.querySelector("button")?.textContent).toBe("Sign out");
  });

  it("shows no control when the status call fails", async () => {
    installSessionGuard();
    unauthorized = true;
    await act(async () => root.render(<SignOut />));
    expect(container.textContent).toBe("");
  });
});
