// @vitest-environment happy-dom
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { ManagersSection } from "@/components/managers-settings";
import * as api from "@/lib/api";
import * as bindingsApi from "@/lib/chat-bindings-api";
import { callV1, V1Error } from "@/lib/v1";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
vi.mock("@/lib/api", async (original) => ({
  ...(await original<typeof import("@/lib/api")>()),
  fetchManagers: vi.fn(), fetchRegistry: vi.fn(async () => ({ profiles: [] })),
}));
vi.mock("@/lib/v1", async (original) => ({
  ...(await original<typeof import("@/lib/v1")>()),
  callV1: vi.fn(),
}));
vi.mock("@/lib/chat-bindings-api", async (original) => ({
  ...(await original<typeof import("@/lib/chat-bindings-api")>()),
  confirmChatBinding: vi.fn(), fetchChatBindings: vi.fn(), fetchChannelChats: vi.fn(), putChatBinding: vi.fn(), deleteChatBinding: vi.fn(),
  listCapabilitySets: vi.fn(), listOverrides: vi.fn(), putDefaultScope: vi.fn(), putOverride: vi.fn(), deleteOverride: vi.fn(),
}));
vi.mock("@/lib/manager-installs-api", () => ({
  fetchManagerInstalls: vi.fn(async (manager_id: string) => ({ manager_id, installs: [] })),
}));
const m = vi.mocked(api);
const b = vi.mocked(bindingsApi);
function manager(id: string, name: string, enabled = true): api.ManagerInfo {
  return { id, name, enabled, builtin: id === "manager", description: "", tools: "tasks", task_scope: "own", scope_project_ids: [],
    task_policy: "free", idle_timeout_seconds: id === "manager" ? 0 : 1800, system_prompt: null, profile_id: null, profile_name: null, templates: [], revision: 0,
    created_at: "", updated_at: "", state: { sync: "ok", running: false, busy: false, healthy: true },
    spend: { today_usd: 0, last_7d_usd: 0, runs_today: 0 } };
}
const managers = [manager("manager", "Manager"), manager("review", "Review", false), manager("archive", "Archive")];
function binding(patch: Partial<bindingsApi.Binding> = {}): bindingsApi.Binding {
  return { channel_id: "bot-one", chat_id: "42", chat_name: "Design", bot_name: "Example Bot",
    channel_type: "telegram", manager_id: "manager", manager_name: "Manager", manager_enabled: true,
    manager_deleted: false, created_at: "", allowed_senders: [], sender_policy: "listed",
    default_scope: null, effective_default_scope: "chat_only", legacy_allowed: false, suggested_scope: null, ...patch };
}
function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (error: Error) => void;
  const promise = new Promise<T>((yes, no) => { resolve = yes; reject = no; });
  return { promise, resolve, reject };
}

describe("Manager explicit chats", () => {
  let container: HTMLDivElement;
  let root: Root;
  let rows: bindingsApi.Binding[];
  const render = () => act(async () => root.render(<ManagersSection />));
  const region = (name = "Manager") => container.querySelector(`[aria-label="Chats for ${name}"]`)!;
  const button = (text: string, scope: Element = container) => Array.from(scope.querySelectorAll("button"))
    .find((el) => el.textContent === text)!;
  const click = (el: Element) => act(async () => (el as HTMLElement).click());
  const select = async (target: string, scope = region()) => {
    const el = scope.querySelector("select")!;
    await act(async () => {
      Object.getOwnPropertyDescriptor(HTMLSelectElement.prototype, "value")!.set!.call(el, target);
      el.dispatchEvent(new Event("change", { bubbles: true }));
    });
  };
  beforeEach(() => {
    vi.resetAllMocks();
    rows = [binding()];
    vi.mocked(callV1).mockImplementation(async (path: string) => {
      if (path === "/api/v1/manager_sessions/list") return { routing_enabled: false, sessions: [] };
      throw new Error("unavailable");
    });
    b.listCapabilitySets.mockResolvedValue([
      { id: "chat_only", name: "Chat only", builtin: true, current_version: 1, items: [] },
      { id: "full", name: "Full", builtin: true, current_version: 1, items: [] },
      { id: "legacy", name: "Legacy", builtin: true, current_version: 1, items: [] },
    ]);
    b.listOverrides.mockResolvedValue([]);
    m.fetchManagers.mockResolvedValue(managers);
    m.fetchRegistry.mockResolvedValue({ profiles: [] } as unknown as api.Registry);
    b.fetchChatBindings.mockImplementation(async () => rows.slice());
    b.fetchChannelChats.mockResolvedValue({ channel_id: "bot-one", bot_manager_id: "manager", bound_chats_only: false,
      chats: [], unbound_chats: [], has_more: false, next_cursor: null });
    b.putChatBinding.mockImplementation(async (_bot, _chat, target, senders, senderPolicy) => {
      rows = rows.map((row) => ({ ...row, sender_policy: senderPolicy ?? row.sender_policy, allowed_senders: senders ?? (row.manager_id === target ? row.allowed_senders : []), manager_id: target, manager_name: managers.find((v) => v.id === target)!.name }));
      return { binding: rows[0] };
    });
    b.deleteChatBinding.mockImplementation(async (channel_id, chat_id) => {
      rows = []; return { channel_id, chat_id, deleted: true };
    });
    container = document.createElement("div"); document.body.append(container); root = createRoot(container);
  });
  afterEach(() => { act(() => root.unmount()); container.remove(); });

  it("shows a default Manager's explicit own binding", async () => {
    await render();
    expect(region().textContent).toContain("Design");
    expect(region().textContent).toContain("Bound to Manager");
    expect(region().textContent).toContain("Scope: chat_only");
    expect(region().textContent).toContain("0 overrides");
    expect(region().textContent).not.toContain("Legacy: everyone admitted");
  });
  const scopeValues = () => [...(region().querySelector('[aria-label="Scope for Design"]') as HTMLSelectElement).options].map((option) => option.value);
  const setControl = async (selector: string, value: string) => {
    const el = region().querySelector(selector) as HTMLSelectElement | HTMLInputElement;
    const proto = el instanceof HTMLSelectElement ? HTMLSelectElement.prototype : HTMLInputElement.prototype;
    await act(async () => {
      Object.getOwnPropertyDescriptor(proto, "value")!.set!.call(el, value);
      el.dispatchEvent(new Event(el instanceof HTMLSelectElement ? "change" : "input", { bubbles: true }));
    });
  };
  it("never offers full, and offers legacy only when the binding allows it", async () => {
    await render();
    expect(scopeValues()).toEqual(["", "chat_only"]);
    expect(scopeValues()).not.toContain("full");
    const overrideScopes = [...(region().querySelector('[aria-label="Override scope for Design"]') as HTMLSelectElement).options].map((option) => option.value);
    expect(overrideScopes).toEqual(["chat_only"]);
    rows = [binding({ legacy_allowed: true })];
    await click(button("Refresh chats"));
    expect(scopeValues()).toContain("legacy");
    expect(scopeValues()).not.toContain("full");
  });
  it("saves the group default with stop_now through the confirmation", async () => {
    await render();
    await setControl('[aria-label="Scope for Design"]', "chat_only");
    await click(button("Save scope", region()));
    await act(async () => (region().querySelector('[aria-label="Apply now, interrupting a running reply"]') as HTMLInputElement).click());
    await click(button("Confirm scope", region()));
    expect(b.putDefaultScope).toHaveBeenCalledWith(expect.objectContaining({
      channel_id: "bot-one", chat_id: "42", manager_id: "manager" }), "chat_only", true);
  });
  it("shows the legacy notice only for legacy and adopts the suggested scope", async () => {
    rows = [binding({ default_scope: "legacy", effective_default_scope: "legacy", legacy_allowed: true, suggested_scope: "chat_only" })];
    await render();
    expect(region().textContent).toContain("Legacy: everyone admitted here shares a session that can run commands with this machine's account.");
    await click(button("Adopt chat_only", region()));
    await click(button("Confirm scope", region()));
    expect(b.putDefaultScope).toHaveBeenCalledWith(expect.anything(), "chat_only", false);
    rows = [binding()];
    await click(button("Refresh chats"));
    expect(region().textContent).not.toContain("Legacy: everyone admitted");
  });
  it("adds a bot override with both ids and shows a conflict inline", async () => {
    b.putOverride.mockRejectedValueOnce(new V1Error(409, "conflict", "sender id already belongs to principal ou_other"));
    await render();
    await setControl('[aria-label="Override kind for Design"]', "bot");
    await setControl('[aria-label="Override id for Design"]', "ou_one");
    await setControl('[aria-label="Override second id for Design"]', "cli_two");
    await click(button("Add override", region()));
    expect(b.putOverride).toHaveBeenCalledWith(expect.objectContaining({ channel_id: "bot-one", chat_id: "42" }), {
      principal_kind: "bot", sender_ids: ["ou_one", "cli_two"], scope_id: "chat_only", stop_now: false,
    });
    expect([...region().querySelectorAll('[role="alert"]')].some((el) => el.textContent?.includes("conflict"))).toBe(true);
  });
  it("removes an override by principal id", async () => {
    b.listOverrides.mockResolvedValue([{ principal_id: "ou_one", principal_kind: "person", sender_ids: ["ou_one"],
      scope_id: "chat_only", created_at: "" }]);
    await render();
    expect(region().textContent).toContain("person");
    expect(region().textContent).toContain("1 override");
    await click(button("Remove", region()));
    expect(b.deleteOverride).toHaveBeenCalledWith(expect.objectContaining({ channel_id: "bot-one", chat_id: "42" }), "ou_one", false);
  });
  it("lists sessions on a scenario Manager and not on the default Manager", async () => {
    await render();
    const bodies = vi.mocked(callV1).mock.calls.filter((call) => call[0] === "/api/v1/manager_sessions/list").map((call) => call[1]);
    expect(bodies).toEqual([{ manager_id: "review" }, { manager_id: "archive" }]);
    expect(container.querySelectorAll('[aria-label="Manager sessions"]')).toHaveLength(2);
    expect(region().querySelector('[aria-label="Manager sessions"]')).toBeNull();
  });
  it.each(["lark", "lark_bot", "telegram"] as const)("edits %s extra senders and confirms them after readback", async (channel_type) => {
    rows = [binding({ channel_type, allowed_senders: ["a", "b"] })];
    await render();
    const input = region().querySelector('input[aria-label="Extra senders for Design"]') as HTMLInputElement;
    expect(input.value).toBe("a, b");
    expect(input.placeholder).toBe(channel_type === "telegram" ? "123456789, …" : "ou_…, cli_…");
    expect(region().textContent).toContain("2 extra senders");
    expect(button("Save", region()).disabled).toBe(true);
    await act(async () => {
      Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value")!.set!.call(input, " a, b ");
      input.dispatchEvent(new Event("input", { bubbles: true }));
    });
    expect(button("Save", region()).disabled).toBe(true);
    await act(async () => {
      Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value")!.set!.call(input, " a, b c , ");
      input.dispatchEvent(new Event("input", { bubbles: true }));
    });
    expect(button("Save", region()).disabled).toBe(false);
    await click(button("Save", region()));
    expect(b.putChatBinding).toHaveBeenCalledWith("bot-one", "42", "manager", ["a", "b", "c"],
      ...(channel_type === "telegram" ? [] : ["listed"]));
    expect(region().textContent).toContain("3 extra senders");
    expect(button("Save", region()).disabled).toBe(true);
  });
  it.each(["lark", "lark_bot"] as const)("round-trips %s sender policy with chat-only admission help", async (channel_type) => {
    rows = [binding({ channel_type })];
    await render();
    const policy = region().querySelector('[aria-label="Sender policy for Design"]') as HTMLSelectElement;
    expect(policy.value).toBe("listed");
    expect(region().textContent).toContain("Only the bot's allowed senders");
    await act(async () => {
      Object.getOwnPropertyDescriptor(HTMLSelectElement.prototype, "value")!.set!.call(policy, "chat");
      policy.dispatchEvent(new Event("change", { bubbles: true }));
    });
    expect(region().textContent).toContain("Anyone who can post in this chat, including other bots");
    await click(button("Save", region()));
    expect(b.putChatBinding).toHaveBeenLastCalledWith("bot-one", "42", "manager", [], "chat");
    expect(policy.value).toBe("chat");
    expect([...region().querySelectorAll("p")].find((p) => p.textContent?.startsWith("Chat ID:"))?.textContent).toContain(" · Everyone in this chat");
    expect(button("Save", region()).disabled).toBe(true);
    await act(async () => {
      Object.getOwnPropertyDescriptor(HTMLSelectElement.prototype, "value")!.set!.call(policy, "listed");
      policy.dispatchEvent(new Event("change", { bubbles: true }));
    });
    await click(button("Save", region()));
    expect(b.putChatBinding).toHaveBeenLastCalledWith("bot-one", "42", "manager", [], "listed");
    expect(policy.value).toBe("listed");
    expect([...region().querySelectorAll("p")].find((p) => p.textContent?.startsWith("Chat ID:"))?.textContent).not.toContain("Everyone in this chat");
  });
  it("waits for explicit sender policy confirmation and reads back only after confirmation", async () => {
    rows = [binding({ channel_type: "lark" })];
    const pending = { status: "needs_confirmation" as const, confirmation_id: "pending", expires_in_seconds: 60,
      session: "session", impact: "Anyone in this chat can reach a Manager with all tools." };
    b.putChatBinding.mockRejectedValueOnce(new bindingsApi.ChatBindingConfirmation(pending));
    b.confirmChatBinding.mockImplementation(async () => {
      rows = rows.map((row) => ({ ...row, sender_policy: "chat" as const }));
      return { binding: rows[0] };
    });
    await render();
    const policy = region().querySelector('[aria-label="Sender policy for Design"]') as HTMLSelectElement;
    await act(async () => {
      Object.getOwnPropertyDescriptor(HTMLSelectElement.prototype, "value")!.set!.call(policy, "chat");
      policy.dispatchEvent(new Event("change", { bubbles: true }));
    });
    await click(button("Save", region()));
    expect(region().textContent).toContain(pending.impact);
    expect([...region().querySelectorAll("p")].find((p) => p.textContent === pending.impact)?.classList.contains("text-warning")).toBe(true);
    expect(b.confirmChatBinding).not.toHaveBeenCalled();
    expect(b.fetchChatBindings).toHaveBeenCalledTimes(1);
    await click(button("Open to everyone", region()));
    expect(b.confirmChatBinding).toHaveBeenCalledWith(pending);
    expect(b.fetchChatBindings).toHaveBeenCalledTimes(2);
    expect(button("Save", region()).disabled).toBe(true);
  });
  it("does not expose the Lark policy control for Telegram", async () => {
    await render();
    expect(region().querySelector('[aria-label="Sender policy for Design"]')).toBeNull();
  });
  it.each([{ allowed_senders: [] }, { allowed_senders: ["8"] }])("warns that moving resets sender policy with or without extra senders: %j", async ({ allowed_senders }) => {
    rows = [binding({ allowed_senders })];
    await render();
    expect(region().textContent).not.toContain("Moving clears");
    await select("review");
    expect(region().textContent).toContain("Moving clears this chat's extra senders and resets its policy to listed senders.");
  });
  it("shows sender save errors in the row and keeps edits available", async () => {
    await render();
    const input = region().querySelector('input[aria-label="Extra senders for Design"]') as HTMLInputElement;
    await act(async () => {
      Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value")!.set!.call(input, "bad");
      input.dispatchEvent(new Event("input", { bubbles: true }));
    });
    b.putChatBinding.mockRejectedValueOnce(new V1Error(400, "invalid", "allowed_senders: bad"));
    await click(button("Save", region()));
    expect(region().querySelector('[role="alert"]')?.textContent).toContain("allowed_senders: bad");
    expect(input.value).toBe("bad");
    expect(button("Save", region()).disabled).toBe(false);
  });
  it("omits implicit default conversations", async () => {
    rows = [];
    await render();
    expect(region().textContent).toContain("No explicit chat bindings");
    expect(region().querySelector("li")).toBeNull();
  });
  it("separates identical chat ids from different bots by Manager", async () => {
    rows.push(binding({ channel_id: "bot-two", bot_name: "Other Bot", manager_id: "review", manager_name: "Review", manager_enabled: false }));
    await render();
    expect(region().textContent).not.toContain("Other Bot");
    expect(region("Review").textContent).toContain("Other Bot");
    expect(region("Review").textContent).not.toContain("Example Bot");
    expect(b.fetchChatBindings).toHaveBeenCalledTimes(1);
  });
  it("moves to an off Manager only after readback and focuses the source Chats", async () => {
    await render();
    const readback = deferred<bindingsApi.Binding[]>();
    b.fetchChatBindings.mockReturnValueOnce(readback.promise);
    await select("review"); await click(button("Move", region()));
    expect(region().textContent).toContain("Design");
    expect((region().querySelector("select") as HTMLSelectElement).disabled).toBe(true);
    await act(async () => readback.resolve(rows));
    expect(region().textContent).not.toContain("Design");
    expect(region("Review").textContent).toContain("Design");
    expect(document.activeElement).toBe(region().querySelector("h4"));
  });
  it.each([false, true])("shows the server unbind consequence for bound-only=%s", async (boundOnly) => {
    b.fetchChannelChats.mockResolvedValueOnce({ channel_id: "bot-one", bot_manager_id: "review", bound_chats_only: boundOnly,
      chats: [], unbound_chats: [], has_more: false, next_cursor: null });
    await render(); await click(button("Unbind", region()));
    expect(region().textContent).toContain(boundOnly ? "stay silent" : "Review · Off");
    expect(b.deleteChatBinding).not.toHaveBeenCalled();
    await click(button("Confirm unbind", region()));
    expect(region().textContent).toContain("No explicit chat bindings");
  });
  it("preserves the confirmed row on a failed pending write and lets the operator retry", async () => {
    const write = deferred<{ binding: bindingsApi.Binding }>();
    b.putChatBinding.mockReturnValueOnce(write.promise);
    await render(); await select("archive"); await click(button("Move", region()));
    expect(button("Unbind", region()).disabled).toBe(true);
    expect(button("Save", region()).disabled).toBe(true);
    await act(async () => write.reject(new V1Error(503, "unavailable", "Try again")));
    expect(region().textContent).toContain("Design");
    expect(region().querySelector('[role="alert"]')?.textContent).toContain("unavailable");
    await click(button("Move", region()));
    expect(region("Archive").textContent).toContain("Design");
  });
  it("keeps confirmed state and exposes retry when write readback fails", async () => {
    await render();
    b.fetchChatBindings.mockRejectedValueOnce(new V1Error(503, "unavailable", "Read failed"));
    await select("archive"); await click(button("Move", region()));
    expect(region().textContent).toContain("Design");
    expect(region().textContent).toContain("Change saved; readback failed");
    await click(button("Retry chats"));
    expect(region().textContent).not.toContain("Design");
    expect(region("Archive").textContent).toContain("Design");
  });
  it("loads an empty list after a typed list failure and retry", async () => {
    rows = [];
    b.fetchChatBindings.mockRejectedValueOnce(new V1Error(503, "unavailable", "Read failed"));
    await render();
    expect(region().textContent).not.toContain("No explicit chat bindings");
    expect(container.textContent).toContain("unavailable");
    await click(button("Retry chats"));
    expect(region().textContent).toContain("No explicit chat bindings");
  });
  it("allows disabled targets but excludes explicitly deleted DTO targets", async () => {
    rows.push(binding({ channel_id: "bot-two", manager_id: "archive", manager_name: "Archive", manager_deleted: true }));
    await render();
    const options = Array.from(region().querySelectorAll("option"));
    expect(options.find((v) => v.value === "review")?.textContent).toContain("Off");
    expect(options.some((v) => v.value === "archive")).toBe(false);
    expect(region("Archive").textContent).toContain("Deleted");
  });
  it("shows not_found and refreshes a target deleted during the write", async () => {
    await render();
    m.fetchManagers.mockResolvedValue(managers.filter((v) => v.id !== "archive"));
    b.putChatBinding.mockRejectedValueOnce(new V1Error(404, "not_found", "Manager is unavailable"));
    await select("archive"); await click(button("Move", region()));
    expect(region().textContent).toContain("not_found");
    expect(region().textContent).toContain("Design");
    expect(Array.from(region().querySelectorAll("option")).find((v) => v.value === "archive")?.disabled).toBe(true);
  });
  it.each(["move", "unbind"])("does not let a stale list restore a row after %s", async (action) => {
    await render();
    const stale = deferred<bindingsApi.Binding[]>();
    const old = rows.slice();
    b.fetchChatBindings.mockReturnValueOnce(stale.promise);
    await click(button("Refresh chats"));
    const signal = b.fetchChatBindings.mock.calls.at(-1)![1]!;
    if (action === "move") {
      await select("archive"); await click(button("Move", region()));
    } else {
      await click(button("Unbind", region())); await click(button("Confirm unbind", region()));
    }
    expect(signal.aborted).toBe(true);
    await act(async () => stale.resolve(old));
    expect(region().textContent).not.toContain("Design");
    expect(region("Archive").textContent?.includes("Design")).toBe(action === "move");
  });
  it("discards an older section refresh even when it completes last", async () => {
    await render();
    const old = deferred<bindingsApi.Binding[]>();
    b.fetchChatBindings.mockReturnValueOnce(old.promise);
    await click(button("Refresh chats"));
    rows = [];
    await click(button("Refresh chats"));
    await act(async () => old.resolve([binding()]));
    expect(region().textContent).toContain("No explicit chat bindings");
  });
  it("keeps binding state on a policy failure and retries policy on demand", async () => {
    b.fetchChannelChats.mockRejectedValueOnce(new V1Error(503, "unavailable", "Policy failed"));
    await render(); await click(button("Unbind", region()));
    expect(region().textContent).toContain("unavailable");
    expect(button("Confirm unbind", region())).toBeUndefined();
    await click(button("Unbind", region()));
    expect(button("Confirm unbind", region())).toBeDefined();
  });
});
