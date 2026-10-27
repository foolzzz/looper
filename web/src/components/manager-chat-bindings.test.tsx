// @vitest-environment happy-dom
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { ManagersSection } from "@/components/managers-settings";
import * as api from "@/lib/api";
import * as bindingsApi from "@/lib/chat-bindings-api";
import { V1Error } from "@/lib/v1";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
vi.mock("@/lib/api", async (original) => ({
  ...(await original<typeof import("@/lib/api")>()),
  fetchManagers: vi.fn(), fetchRegistry: vi.fn(async () => ({ profiles: [] })),
}));
vi.mock("@/lib/chat-bindings-api", () => ({
  fetchChatBindings: vi.fn(), fetchChannelChats: vi.fn(), putChatBinding: vi.fn(), deleteChatBinding: vi.fn(),
}));
const m = vi.mocked(api);
const b = vi.mocked(bindingsApi);
function manager(id: string, name: string, enabled = true): api.ManagerInfo {
  return { id, name, enabled, builtin: id === "manager", description: "", tools: "tasks", task_scope: "own",
    task_policy: "free", idle_timeout_seconds: id === "manager" ? 0 : 1800, system_prompt: null, profile_id: null, profile_name: null, templates: [], revision: 0,
    created_at: "", updated_at: "", state: { sync: "ok", running: false, busy: false, healthy: true },
    spend: { today_usd: 0, last_7d_usd: 0, runs_today: 0 } };
}
const managers = [manager("manager", "Manager"), manager("review", "Review", false), manager("archive", "Archive")];
function binding(patch: Partial<bindingsApi.Binding> = {}): bindingsApi.Binding {
  return { channel_id: "bot-one", chat_id: "42", chat_name: "Design", bot_name: "Example Bot",
    channel_type: "telegram", manager_id: "manager", manager_name: "Manager", manager_enabled: true,
    manager_deleted: false, created_at: "", ...patch };
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
    m.fetchManagers.mockResolvedValue(managers);
    m.fetchRegistry.mockResolvedValue({ profiles: [] } as unknown as api.Registry);
    b.fetchChatBindings.mockImplementation(async () => rows.slice());
    b.fetchChannelChats.mockResolvedValue({ channel_id: "bot-one", bot_manager_id: "manager", bound_chats_only: false,
      chats: [], unbound_chats: [], has_more: false, next_cursor: null });
    b.putChatBinding.mockImplementation(async (_bot, _chat, target) => {
      rows = rows.map((row) => ({ ...row, manager_id: target, manager_name: managers.find((v) => v.id === target)!.name }));
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
