// @vitest-environment happy-dom
import { act } from "react";
import { createRoot } from "react-dom/client";
import { afterEach, expect, it, vi } from "vitest";
import { AcpReviewerSettingsSection } from "./acp-reviewer-settings";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
afterEach(() => vi.unstubAllGlobals());

async function mount(fail = false) {
  const writes: Record<string, unknown>[] = [];
  let pending: Record<string, unknown> = {};
  let session: string | undefined;
  let confirmed = false;
  let saved = { enabled: false, command: [], model: "", account: "", config_dir: "", inspect_roots: [],
    policy_check: { reason: "unknown_cli_config", detail: "Unknown command; no process will start." },
    latest_launch_check: { reason: "config_dir_overlaps", detail: "Config directory overlaps the worktree" } };
  vi.stubGlobal("confirm", vi.fn(() => true));
  vi.stubGlobal("fetch", vi.fn(async (path: string, init: RequestInit) => {
    if (fail) return Response.json({ status: "error", error: { code: "unavailable", message: "Settings unavailable" } }, { status: 503 });
    if (path.endsWith("/update")) {
      const body = JSON.parse(String(init.body)); writes.push(body); pending = body;
      session = (init.headers as Record<string, string>)["X-Looper-Session"];
      return Response.json({ status: "needs_confirmation", confirmation_id: "acp-confirmation", expires_in_seconds: 300,
        impact: "启用后，Reviewer 可以在写保护策略下运行这条命令。未知 CLI 和外部 MCP 服务拒绝启动。" }, { status: 202 });
    }
    if (path.endsWith("/confirm")) {
      expect(JSON.parse(String(init.body))).toEqual({ confirmation_id: "acp-confirmation" });
      expect((init.headers as Record<string, string>)["X-Looper-Session"]).toBe(session);
      saved = { ...saved, ...pending }; confirmed = true;
    }
    return Response.json({ status: "ok", result: saved });
  }));
  const container = document.createElement("div"); document.body.append(container);
  const root = createRoot(container);
  await act(async () => root.render(<AcpReviewerSettingsSection />));
  return { container, writes, confirmed: () => confirmed, close: () => { act(() => root.unmount()); container.remove(); } };
}
async function enter(input: HTMLInputElement | HTMLTextAreaElement, value: string) {
  await act(async () => {
    const proto = input instanceof HTMLTextAreaElement ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;
    Object.getOwnPropertyDescriptor(proto, "value")!.set!.call(input, value);
    input.dispatchEvent(new Event("input", { bubbles: true }));
  });
}

it("saves the six settings and explains the conservative process-tree check", async () => {
  const page = await mount();
  try {
    expect(page.container.textContent).toContain("ACP Reviewer");
    expect(page.container.textContent).toContain("Agent Profiles");
    expect(page.container.textContent).toContain("unknown_cli_config");
    expect(page.container.textContent).toContain("config_dir_overlaps");
    expect(page.container.querySelectorAll('input[type="checkbox"]')).toHaveLength(0);
    await enter(page.container.querySelector('[id$="-command"]')!, '["grok","agent","stdio"]');
    await enter(page.container.querySelector('[id$="-model"]')!, "review-model");
    await enter(page.container.querySelector('[id$="-account"]')!, "Review account");
    await enter(page.container.querySelector('[id$="-roots"]')!, '["/review/material"]');
    await act(async () => page.container.querySelector<HTMLButtonElement>('[role="switch"]')!.click());
    await act(async () => page.container.querySelector('form')!.dispatchEvent(new Event("submit", { bubbles: true, cancelable: true })));
    expect(page.writes).toEqual([{ enabled: true, command: ["grok", "agent", "stdio"], model: "review-model",
      account: "Review account", config_dir: "", inspect_roots: ["/review/material"] }]);
    expect(vi.mocked(confirm)).toHaveBeenCalled();
    expect(page.confirmed()).toBe(true);
  } finally { page.close(); }
});

it("rejects malformed argv locally without writing", async () => {
  const page = await mount();
  try {
    await enter(page.container.querySelector('[id$="-command"]')!, "grok agent stdio");
    await act(async () => page.container.querySelector('form')!.dispatchEvent(new Event("submit", { bubbles: true, cancelable: true })));
    expect(page.writes).toEqual([]);
    expect(page.container.querySelector('[role="alert"]')?.textContent).toContain("JSON");
  } finally { page.close(); }
});

it("shows unavailable settings with a retry", async () => {
  const page = await mount(true);
  try {
    expect(page.container.querySelector('[role="alert"]')?.textContent).toContain("Settings unavailable");
    expect(page.container.querySelector('button')?.textContent).toBe("重试");
  } finally { page.close(); }
});


it("does not apply a server confirmation that the operator cancels", async () => {
  const page = await mount();
  vi.mocked(confirm).mockReturnValue(false);
  try {
    await act(async () => page.container.querySelector('form')!.dispatchEvent(new Event("submit", { bubbles: true, cancelable: true })));
    expect(page.writes).toHaveLength(1);
    expect(page.confirmed()).toBe(false);
    expect(page.container.querySelector('[role="alert"]')).toBeNull();
  } finally { page.close(); }
});
