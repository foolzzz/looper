// @vitest-environment happy-dom
import { act, useState } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { ChatPage } from "@/pages/chat";

let initialQuery = "";
function ChatWithQuery() {
  const [query, setQuery] = useState(initialQuery);
  return <ChatPage query={query} onQuery={setQuery} />;
}

const api = vi.hoisted(() => ({
  sendChat: vi.fn(),
  streamSSE: vi.fn(),
  fetchChatMessages: vi.fn(),
  fetchChatTurn: vi.fn(),
  fetchChatSettings: vi.fn(),
  stopChatReply: vi.fn(),
  fetchChatTargets: vi.fn(),
  fetchChatSessions: vi.fn(),
  fetchChatSession: vi.fn(),
  fetchManagerSession: vi.fn(),
  watchChat: vi.fn((_sid: string, _cb: (m: unknown) => void) => () => {}),
}));
vi.mock("@/lib/api", async (importOriginal) => {
  const real = await importOriginal<typeof import("@/lib/api")>();
  return {
    ...real,
    ...api,
    fetchRoleConfigs: async () => [],
    fetchTaskActivity: async () => ({ activity: [] }),
    fetchTaskDetail: async () => ({ group: "running", gates: [] }),
    fetchLlmRecords: async () => [],
    saveChatMessage: async () => {},
    watchChat: (sid: string, cb: (m: unknown) => void) => api.watchChat(sid, cb),
  };
});

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

const MANAGER = { kind: "manager", id: "manager", can_send: true };
const REVIEW = { kind: "manager", id: "manager-review", name: "Code review", can_send: true };
const SID = "session-default";
const KEY_REVIEW = "looper-chat-session-id:manager-review";

const row = (id: string, text = id) => ({
  session_id: id, channel: "web", message_count: 1, first_message_at: "2026-01-01T00:00:00Z",
  last_message_at: "2026-01-01T00:00:00Z", last_role: "user", last_content: text,
});
const msg = (id: string, content: string) => ({ message_id: id, role: "user", content, created_at: "2026-01-01T00:00:00Z" });
function deferred<T>() {
  let resolve!: (v: T) => void;
  let reject!: (e: unknown) => void;
  const promise = new Promise<T>((a, b) => {
    resolve = a;
    reject = b;
  });
  return { promise, resolve, reject };
}

describe("chat Manager picker", () => {
  let container: HTMLDivElement;
  let root: Root;

  const flush = () => act(async () => { await new Promise((r) => setTimeout(r, 0)); });
  const input = () => container.querySelector("textarea") as HTMLTextAreaElement;
  const say = async (text: string) => {
    act(() => {
      const setter = Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype, "value")!.set!;
      setter.call(input(), text);
      input().dispatchEvent(new Event("input", { bubbles: true }));
    });
    act(() => {
      input().dispatchEvent(new KeyboardEvent("keydown", { key: "Enter", bubbles: true, cancelable: true }));
    });
    await flush();
  };
  const buttons = () => Array.from(container.querySelectorAll("button"));
  const byText = (re: RegExp) => buttons().find((b) => re.test(b.textContent ?? ""));
  const click = async (b: HTMLElement | undefined) => {
    expect(b).toBeTruthy();
    act(() => b!.dispatchEvent(new MouseEvent("click", { bubbles: true })));
    await flush();
  };
  const pick = async (title: string) => {
    await click(buttons().find((b) => b.querySelector("p.text-sm.font-semibold")));
    await click(buttons().find((b) => b.textContent?.startsWith(title) && !b.querySelector("p.font-semibold")));
  };
  async function mount(query = "") {
    initialQuery = query;
    act(() => root.render(<ChatWithQuery />));
    await flush();
  }

  beforeEach(() => {
    localStorage.clear();
    localStorage.setItem("looper-chat-session-id", SID);
    for (const fn of Object.values(api)) fn.mockReset();
    api.sendChat.mockResolvedValue({ events_url: "/events" });
    api.streamSSE.mockImplementation(() => () => {});
    api.fetchChatMessages.mockResolvedValue([]);
    api.fetchChatTurn.mockResolvedValue(null);
    api.fetchChatSettings.mockResolvedValue({ stop_enabled: true });
    api.fetchChatTargets.mockResolvedValue([MANAGER, REVIEW]);
    api.fetchChatSessions.mockResolvedValue([]);
    api.fetchChatSession.mockResolvedValue(null);
    api.fetchManagerSession.mockResolvedValue({
      session: { active: false, runtime: null, profile_id: null }, selected: null,
      effective: { profile: null, source: "session" }, problem: null, choices: [],
    });
    api.watchChat.mockImplementation(() => () => {});
    container = document.createElement("div");
    document.body.append(container);
    root = createRoot(container);
  });

  afterEach(() => {
    act(() => root.unmount());
    container.remove();
  });

  it("lists every Manager before Tasks", async () => {
    await mount();
    await click(buttons().find((b) => b.querySelector("p.text-sm.font-semibold")));
    const text = container.textContent ?? "";
    expect(text.indexOf("Code review")).toBeGreaterThan(-1);
    expect(text.indexOf("Code review")).toBeLessThan(text.indexOf("Tasks"));
    expect(text.indexOf("global")).toBeLessThan(text.indexOf("Tasks"));
  });

  it("shows one Manager row titled Manager with only the default", async () => {
    api.fetchChatTargets.mockResolvedValue([MANAGER]);
    await mount();
    await click(buttons().find((b) => b.querySelector("p.text-sm.font-semibold")));
    expect(container.textContent).toContain("global");
    expect(container.textContent).not.toContain("manager-review");
  });

  it("sends to the selected Manager with its own session, and back to the default", async () => {
    await mount();
    await pick("Code review");
    const reviewSid = localStorage.getItem(KEY_REVIEW)!;
    expect(reviewSid).toBeTruthy();
    expect(reviewSid).not.toBe(SID);
    await say("hi review");
    expect(api.sendChat).toHaveBeenLastCalledWith(
      "hi review", reviewSid, expect.any(String), { kind: "manager", id: "manager-review" }, expect.anything(),
    );
    await pick("Manager");
    await say("hi default");
    expect(api.sendChat).toHaveBeenLastCalledWith("hi default", SID, expect.any(String), { kind: "manager" }, expect.anything());
  });

  it("asks for the selected Manager's session view", async () => {
    await mount();
    await pick("Code review");
    await click(byText(/New session/i));
    expect(api.fetchManagerSession).toHaveBeenCalledWith("manager-review", expect.anything());
  });

  it("keeps a thread pointer per Manager", async () => {
    api.fetchChatSessions.mockImplementation(async (m: string) => (m === "manager-review" ? [row("r1")] : []));
    await mount();
    await pick("Code review");
    const before = localStorage.getItem(KEY_REVIEW);
    await click(byText(/r1/));
    expect(localStorage.getItem(KEY_REVIEW)).toBe("r1");
    expect(before).not.toBe("r1");
    expect(localStorage.getItem("looper-chat-session-id")).toBe(SID);
    await pick("Manager");
    await say("x");
    expect(api.sendChat).toHaveBeenLastCalledWith("x", SID, expect.any(String), { kind: "manager" }, expect.anything());
  });

  it("discards a late history answer after a switch", async () => {
    const a = deferred<unknown[]>();
    const b = deferred<unknown[]>();
    await mount();
    await pick("Code review");
    const reviewSid = localStorage.getItem(KEY_REVIEW)!;
    api.fetchChatMessages.mockImplementation((sid: string) => (sid === SID ? a.promise : b.promise));
    await pick("Manager");
    await pick("Code review");
    await act(async () => b.resolve([msg("b1", "B rows")]));
    await act(async () => a.resolve([msg("a1", "A rows")]));
    await flush();
    expect(container.textContent).toContain("B rows");
    expect(container.textContent).not.toContain("A rows");
    expect(localStorage.getItem(`looper-chat-messages-${reviewSid}`)).toContain("B rows");
    expect(localStorage.getItem(`looper-chat-messages-${SID}`)).toBeNull();
  });

  it("discards a late conversation list after a switch", async () => {
    const a = deferred<unknown[]>();
    const b = deferred<unknown[]>();
    api.fetchChatSessions.mockImplementation((m: string) => (m === "manager" ? a.promise : b.promise));
    await mount();
    await pick("Code review");
    await act(async () => b.resolve([row("b1", "B thread")]));
    await act(async () => a.resolve([row("a1", "A thread")]));
    await flush();
    expect(container.textContent).toContain("B thread");
    expect(container.textContent).not.toContain("A thread");
  });

  it("reads a linked conversation under its Manager", async () => {
    await mount("target=manager-review&conversation=s1");
    expect(api.fetchChatSession).toHaveBeenCalledWith("s1", "manager-review", expect.anything());
  });

  it("shows no stale rows right after a switch", async () => {
    const b = deferred<unknown[]>();
    api.fetchChatSessions.mockImplementation((m: string) => (m === "manager" ? Promise.resolve([row("a1", "A thread")]) : b.promise));
    await mount();
    expect(container.textContent).toContain("A thread");
    const rowButton = byText(/A thread/)!;
    const propsKey = Object.keys(rowButton).find((k) => k.startsWith("__reactProps$"))!;
    const staleClick = (rowButton as unknown as Record<string, { onClick: () => void }>)[propsKey].onClick;
    await pick("Code review");
    const pointer = localStorage.getItem(KEY_REVIEW);
    expect(container.textContent).not.toContain("A thread");
    api.fetchChatMessages.mockClear();
    await act(async () => staleClick());
    await flush();
    expect(localStorage.getItem(KEY_REVIEW)).toBe(pointer);
    expect(api.fetchChatMessages).not.toHaveBeenCalledWith("a1", expect.anything());
    await act(async () => b.resolve([row("b1", "B thread")]));
    expect(container.textContent).toContain("B thread");
    expect(container.textContent).not.toContain("A thread");
  });

  it("stays on the current thread for a link to another Manager's conversation", async () => {
    api.fetchChatSession.mockRejectedValue(new Error("load the conversation 404"));
    await mount("target=manager-review&conversation=a1");
    const pointer = localStorage.getItem(KEY_REVIEW);
    expect(pointer).not.toBe("a1");
    expect(container.textContent).toContain("404");
  });
});
