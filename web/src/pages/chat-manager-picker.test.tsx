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
  watchChat: vi.fn((_sid: string, _cb: (m: unknown) => void, _managerId?: string) => () => {}),
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
    watchChat: (sid: string, cb: (m: unknown) => void, managerId?: string) => api.watchChat(sid, cb, managerId),
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

  it("closes the New session panel when the Manager changes", async () => {
    await mount();
    await pick("Code review");
    await click(byText(/New session/i));
    expect(byText(/Start new session/i)).toBeTruthy();
    await pick("Manager");
    expect(byText(/Start new session/i)).toBeUndefined();
  });

  it("watches the open thread under its Manager", async () => {
    api.fetchChatSessions.mockResolvedValue([row("r1")]);
    api.fetchChatMessages.mockResolvedValue([msg("m1", "hello")]);
    await mount();
    await pick("Code review");
    await click(byText(/r1/));
    expect(api.watchChat).toHaveBeenCalledWith("r1", expect.any(Function), "manager-review");
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
    expect(api.fetchChatMessages.mock.calls.some(([sid]) => sid === "a1")).toBe(false);
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

  describe("with the server's answers", () => {
    const OWNED = "this conversation belongs to Manager Manager: open it there or start a new one";
    // The server: thread "a-sid" is the default Manager's; any other id is new (no row, no messages).
    const server = vi.fn(async (url: string) => {
      const params = new URL(url, "http://x").searchParams;
      const manager = params.get("manager") ?? "manager";
      if (params.get("session_id") === "a-sid" && manager !== "manager") {
        return new Response(JSON.stringify({ error: OWNED, manager_id: "manager" }), { status: 409 });
      }
      if (url.includes("/chat/messages")) {
        const rows = params.get("session_id") === "a-sid" ? [msg("a1", "A's secret history")] : [];
        return new Response(JSON.stringify({ messages: rows }), { status: 200 });
      }
      const rows = params.get("session_id") === "a-sid" ? [row("a-sid", "A's secret history")] : [];
      return new Response(JSON.stringify({ sessions: rows }), { status: 200 });
    });

    beforeEach(async () => {
      const real = await vi.importActual<typeof import("@/lib/api")>("@/lib/api");
      api.fetchChatSession.mockImplementation(real.fetchChatSession);
      api.fetchChatMessages.mockImplementation(real.fetchChatMessages);
      server.mockClear();
      vi.stubGlobal("fetch", server);
    });
    afterEach(() => vi.unstubAllGlobals());

    const asked = (path: string, sid: string) =>
      server.mock.calls.filter(([url]) => url.includes(path) && url.includes(`session_id=${sid}`)).map(([url]) => url);

    it.each(["planner-t1", "telegram:9"])("does not adopt the missing conversation %s", async (id) => {
      await mount(`conversation=${encodeURIComponent(id)}`);
      expect(localStorage.getItem("looper-chat-session-id")).toBe(SID);
      expect(container.querySelector('[role="alert"]')?.textContent).toMatch(/conversation not found/i);
      expect(input()).toBeNull();
      expect(api.sendChat).not.toHaveBeenCalled();
    });

    it("opens an id the server answers with an empty list as a new thread of the selected Manager", async () => {
      await mount("target=manager-review&conversation=new-sid");
      expect(asked("/chat/sessions", "new-sid")).toEqual([expect.stringContaining("manager=manager-review")]);
      expect(localStorage.getItem(KEY_REVIEW)).toBe("new-sid");
      expect(container.textContent).not.toContain("another Manager");
      expect(container.textContent).toContain("Send a message to start");
      await say("hello");
      expect(api.sendChat).toHaveBeenLastCalledWith(
        "hello", "new-sid", expect.any(String), { kind: "manager", id: "manager-review" }, expect.anything(),
      );
    });

    it("never attaches another Manager's conversation from the address, and says whose it is", async () => {
      await mount("target=manager-review&conversation=a-sid");
      expect(localStorage.getItem(KEY_REVIEW)).not.toBe("a-sid");
      expect(container.textContent).toContain("This is another Manager's conversation");
      expect(container.textContent).toContain(OWNED);
      expect(container.textContent).not.toContain("A's secret history");
      expect(container.textContent).not.toContain("Send a message to start");
      expect(input()).toBeNull();
      expect(asked("/chat/messages", "a-sid")).toEqual([]);
      // It opens under the Manager it belongs to.
      await click(byText(/Open it there/));
      expect(localStorage.getItem("looper-chat-session-id")).toBe("a-sid");
      expect(container.textContent).toContain("A's secret history");
      expect(localStorage.getItem(KEY_REVIEW)).not.toBe("a-sid");
    });

    it("starts a new thread instead of a stored pointer to another Manager's conversation", async () => {
      localStorage.setItem(KEY_REVIEW, "a-sid");
      await mount("target=manager-review");
      expect(asked("/chat/messages", "a-sid")).toEqual([expect.stringContaining("manager=manager-review")]);
      const pointer = localStorage.getItem(KEY_REVIEW);
      expect(pointer).toBeTruthy();
      expect(pointer).not.toBe("a-sid");
      expect(container.textContent).not.toContain("A's secret history");
      expect(container.textContent).toContain("belongs to another Manager");
      await say("hello");
      expect(api.sendChat).toHaveBeenLastCalledWith(
        "hello", pointer, expect.any(String), { kind: "manager", id: "manager-review" }, expect.anything(),
      );
    });

    it("switching to a Manager whose pointer names the other's thread shows and watches none of it", async () => {
      localStorage.setItem("looper-chat-session-id", "a-sid");
      localStorage.setItem(KEY_REVIEW, "a-sid");
      const watched: { sid: string; cb: (m: unknown) => void; closed: boolean }[] = [];
      api.watchChat.mockImplementation((sid: string, cb: (m: unknown) => void) => {
        const w = { sid, cb, closed: false };
        watched.push(w);
        return () => { w.closed = true; };
      });
      await mount();
      expect(container.textContent).toContain("A's secret history");
      expect(watched.map((w) => w.sid)).toEqual(["a-sid"]);
      const before = server.mock.calls.length;
      await pick("Code review");
      await flush();
      const afterSwitch = server.mock.calls.slice(before).map(([url]) => url as string)
        .filter((url) => url.includes("/chat/messages") && url.includes("session_id=a-sid"));
      // The only read of A's thread under B is the scoped one, which the server refuses.
      expect(afterSwitch).toEqual([expect.stringContaining("manager=manager-review")]);
      expect(watched[0].closed).toBe(true);
      expect(watched.filter((w) => w.sid === "a-sid")).toHaveLength(1);
      expect(container.textContent).not.toContain("A's secret history");
      const pointer = localStorage.getItem(KEY_REVIEW);
      expect(pointer).not.toBe("a-sid");
      expect(localStorage.getItem("looper-chat-session-id")).toBe("a-sid");
      // B watches its own new thread only.
      expect(watched.slice(1).map((w) => w.sid)).toEqual([pointer]);
      // A late event on A's subscription is dropped.
      await act(async () => watched[0].cb({
        message_id: "late", role: "assistant", content: "A's late reply", created_at: "2026-01-01T00:00:01Z",
      }));
      expect(container.textContent).not.toContain("A's late reply");
    });

    it("watches nothing until the Manager-scoped read confirms the thread", async () => {
      localStorage.setItem(KEY_REVIEW, "a-sid");
      const pending = deferred<unknown[]>();
      api.fetchChatMessages.mockImplementation((sid: string) => (sid === "a-sid" ? pending.promise : Promise.resolve([])));
      await mount("target=manager-review");
      expect(api.fetchChatMessages).toHaveBeenCalledWith("a-sid", expect.anything(), "manager-review");
      expect(api.watchChat.mock.calls.some(([sid]) => sid === "a-sid")).toBe(false);
      expect(api.fetchChatTurn.mock.calls.some(([sid]) => sid === "a-sid")).toBe(false);
      const real = await vi.importActual<typeof import("@/lib/api")>("@/lib/api");
      await act(async () => pending.reject(new real.ForeignConversationError("A's", "manager")));
      await flush();
      expect(api.watchChat.mock.calls.some(([sid]) => sid === "a-sid")).toBe(false);
      expect(api.fetchChatTurn.mock.calls.some(([sid]) => sid === "a-sid")).toBe(false);
      expect(localStorage.getItem(KEY_REVIEW)).not.toBe("a-sid");
    });
  });
});
