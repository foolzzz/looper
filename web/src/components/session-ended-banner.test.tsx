// @vitest-environment happy-dom
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { SessionEndedBanner } from "@/components/session-ended-banner";
import { SignOut } from "@/components/sign-out";
import { streamSSE, watchChat } from "@/lib/api";
import { installSessionGuard, isSessionEnded, resetSessionGuard, SessionEndedError } from "@/lib/session";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

type NodeProcess = { on(e: string, f: () => void): void; off(e: string, f: () => void): void };

describe("session ended banner and sign out", () => {
  let container: HTMLDivElement;
  let root: Root;
  let status: Response;
  let seen: string[];
  let push: (text: string) => void;

  beforeEach(() => {
    seen = [];
    resetSessionGuard();
    push = () => {};
    status = Response.json({});
    vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      seen.push(url);
      if (url === "/api/stream") {
        const body = new ReadableStream<Uint8Array>({
          start(controller) {
            push = (text) => {
              try { controller.enqueue(new TextEncoder().encode(text)); } catch { /* closed by the abort */ }
            };
            init?.signal?.addEventListener("abort", () => controller.error(new DOMException("x", "AbortError")));
          },
        });
        return new Response(body, { status: 200 });
      }
      if (url === "/api/status") return status.clone();
      if (url === "/api/overview" || url.startsWith("/api/chat/events") || url === "/login") return new Response("{}", { status: 401 });
      return new Response("{}", { status: 200 });
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

  it("shows one banner with a Reload button after an API call gets 401, and retries nothing", async () => {
    installSessionGuard();
    await act(async () => root.render(<SessionEndedBanner />));
    expect(container.textContent).toBe("");
    await act(async () => {
      void fetch("/api/overview");
      await vi.waitFor(() => expect(container.querySelector('[role="alert"]')).not.toBeNull());
      await expect(fetch("/api/overview")).rejects.toBeInstanceOf(SessionEndedError);
    });
    expect(container.querySelectorAll('[role="alert"]')).toHaveLength(1);
    expect(container.textContent).toContain("Your session ended. Reload to sign in again.");
    expect(container.querySelector("button")?.textContent).toBe("Reload");
    expect(seen).toEqual(["/api/overview"]);
  });

  it("settles no call after the session ended, so a retry loop stops and no widget shows an error", async () => {
    vi.useFakeTimers();
    try {
      installSessionGuard();
      const stop = watchChat("s1", () => {});
      await vi.advanceTimersByTimeAsync(0);
      expect(seen.filter((u) => u.startsWith("/api/chat/events"))).toHaveLength(1);
      await vi.advanceTimersByTimeAsync(60_000);
      await expect(fetch("/api/overview")).rejects.toBeInstanceOf(SessionEndedError);
      expect(seen.filter((u) => u.startsWith("/api/chat/events"))).toHaveLength(1);
      expect(seen).not.toContain("/api/status");
      stop();
    } finally {
      vi.useRealTimers();
    }
  });

  it("closes an open stream, clears pollers and rejects later calls once another request gets 401", async () => {
    vi.useFakeTimers();
    const unhandled = vi.fn();
    const proc = (globalThis as unknown as { process: NodeProcess }).process;
    proc.on("unhandledRejection", unhandled);
    try {
      installSessionGuard();
      const events: unknown[] = [];
      streamSSE("/api/stream", (e) => events.push(e));
      await vi.advanceTimersByTimeAsync(0);
      push('data: {"kind":"a","payload":{}}\n\n');
      await vi.advanceTimersByTimeAsync(0);
      expect(events).toHaveLength(1);
      let polls = 0;
      window.setInterval(() => {
        polls++;
        void fetch("/api/status").catch((e) => expect(e).toBeInstanceOf(SessionEndedError));
      }, 1000);
      await vi.advanceTimersByTimeAsync(2000);
      const before = polls;
      void fetch("/api/overview");
      await vi.advanceTimersByTimeAsync(0);
      expect(isSessionEnded()).toBe(true);
      push('data: {"kind":"b","payload":{}}\n\n');
      await vi.advanceTimersByTimeAsync(60_000);
      expect(events).toHaveLength(1);
      expect(polls).toBe(before);
      await expect(fetch("/api/status")).rejects.toBeInstanceOf(SessionEndedError);
      expect(unhandled).not.toHaveBeenCalled();
    } finally {
      proc.off("unhandledRejection", unhandled);
      vi.useRealTimers();
    }
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
    status = new Response("{}", { status: 401 });
    await act(async () => root.render(<SignOut />));
    expect(container.textContent).toBe("");
  });
});
