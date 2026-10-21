// @vitest-environment happy-dom
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import App from "@/App";
import { SessionEndedBanner } from "@/components/session-ended-banner";
import { SignOut } from "@/components/sign-out";
import { installSessionGuard, resetSessionGuard, SessionEndedError } from "@/lib/session";

const sse = vi.hoisted(() => ({ events: 0, done: 0 }));
vi.mock("@/lib/api", async (importOriginal) => {
  const api = await importOriginal<typeof import("@/lib/api")>();
  return {
    ...api,
    streamSSE: (url: string, onEvent: Parameters<typeof api.streamSSE>[1], onDone?: () => void) =>
      api.streamSSE(url, (e) => { sse.events++; onEvent(e); }, () => { sse.done++; onDone?.(); }),
  };
});

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

const MANAGER_SESSION = {
  session: { active: false, runtime: null, profile_id: null },
  selected: null,
  effective: { profile: null, source: "global" },
  problem: null,
  choices: [],
};

describe("session ended screen and sign out", () => {
  let container: HTMLDivElement;
  let root: Root;
  let status: Response;
  let calls: string[];
  let unauthorized: boolean;
  let push: (text: string) => void;
  let replyAborted: boolean;

  beforeEach(() => {
    calls = [];
    unauthorized = false;
    push = () => {};
    replyAborted = false;
    sse.events = 0;
    sse.done = 0;
    status = Response.json({});
    resetSessionGuard();
    vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      calls.push(url);
      if (url === "/api/chat/send") return Response.json({ events_url: "/api/reply-stream" });
      if (url === "/api/reply-stream") {
        const body = new ReadableStream<Uint8Array>({
          start(controller) {
            push = (text) => {
              try { controller.enqueue(new TextEncoder().encode(text)); } catch { /* closed by the abort */ }
            };
            init?.signal?.addEventListener("abort", () => {
              replyAborted = true;
              controller.error(new DOMException("aborted", "AbortError"));
            });
          },
        });
        return new Response(body, { status: 200 });
      }
      if (unauthorized && url.startsWith("/api/")) return new Response("{}", { status: 401 });
      if (url === "/api/status") return status.clone();
      if (url === "/login") return new Response("{}", { status: 401 });
      if (url === "/api/overview") return Response.json(OVERVIEW);
      if (url === "/api/manager/session") return Response.json(MANAGER_SESSION);
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
    // Count at the wrapper: a poller the guard rejects without a request would still show up here.
    const guarded = window.fetch;
    const wrapperCalls: string[] = [];
    window.fetch = (input, init) => {
      wrapperCalls.push(String(input instanceof Request ? input.url : input));
      return guarded(input, init);
    };
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
    const wrapperBefore = wrapperCalls.length;
    await act(async () => vi.advanceTimersByTimeAsync(60_000));
    expect(calls).toHaveLength(before);
    expect(wrapperCalls).toHaveLength(wrapperBefore);
    expect(container.textContent).not.toContain("Connection failed");
  });

  it("aborts the chat reply stream when the session ends and dispatches nothing after", async () => {
    vi.useFakeTimers();
    window.location.hash = "#chat";
    installSessionGuard();
    await act(async () => root.render(<App />));
    await act(async () => vi.advanceTimersByTimeAsync(1000));
    const input = container.querySelector("textarea") as HTMLTextAreaElement;
    await act(async () => {
      Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype, "value")!.set!.call(input, "hello");
      input.dispatchEvent(new Event("input", { bubbles: true }));
    });
    await act(async () => {
      input.form!.requestSubmit();
      await vi.advanceTimersByTimeAsync(100);
    });
    expect(calls).toContain("/api/reply-stream");
    await act(async () => {
      push('data: {"kind":"agent.phase","payload":{"label":"working"}}\n\n');
      await vi.advanceTimersByTimeAsync(100);
    });
    expect(sse.events).toBe(1);
    expect(replyAborted).toBe(false);

    unauthorized = true; // a different request gets the 401
    await act(async () => vi.advanceTimersByTimeAsync(10_000));
    expect(container.textContent).toContain("Your session ended");
    expect(replyAborted).toBe(true);
    expect(sse.done).toBe(1);
    const before = calls.length;
    await act(async () => {
      push('data: {"kind":"agent.phase","payload":{"label":"late"}}\n\n');
      await vi.advanceTimersByTimeAsync(60_000);
    });
    expect(sse.events).toBe(1);
    expect(sse.done).toBe(1);
    expect(calls).toHaveLength(before);
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
