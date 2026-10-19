// @vitest-environment happy-dom
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { SessionEndedBanner } from "@/components/session-ended-banner";
import { SignOut } from "@/components/sign-out";
import { installSessionGuard, resetSessionGuard } from "@/lib/session";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

describe("session ended banner and sign out", () => {
  let container: HTMLDivElement;
  let root: Root;
  let status: Response;
  let seen: string[];

  beforeEach(() => {
    seen = [];
    resetSessionGuard();
    vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input);
      seen.push(url);
      if (url === "/api/status") return status.clone();
      if (url === "/api/overview" || url === "/login") return new Response("{}", { status: 401 });
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
      await fetch("/api/overview");
      await fetch("/api/overview");
    });
    expect(container.querySelectorAll('[role="alert"]')).toHaveLength(1);
    expect(container.textContent).toContain("Your session ended. Reload to sign in again.");
    expect(container.querySelector("button")?.textContent).toBe("Reload");
    expect(seen).toEqual(["/api/overview", "/api/overview"]);
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
