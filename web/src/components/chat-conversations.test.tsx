// @vitest-environment happy-dom
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { ConversationList } from "@/components/chat-conversations";
import type { ChatSession } from "@/lib/api";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

const session = (id: string, channel = "web", client: ChatSession["client"] = null): ChatSession => ({
  session_id: id, channel, client, message_count: 1, first_message_at: "2026-10-24T12:00:00Z",
  last_message_at: "2026-10-24T12:00:00Z", last_role: "user", last_content: id,
});

describe("ConversationList", () => {
  let container: HTMLDivElement;
  let root: Root;
  beforeEach(() => {
    container = document.createElement("div");
    document.body.appendChild(container);
    root = createRoot(container);
  });
  afterEach(async () => {
    await act(async () => root.unmount());
    container.remove();
  });

  it("groups client conversations after Dashboard chat without repeating them under their channel", async () => {
    const terminal = session("cli-one", "web", { id: "one", name: "Terminal client" });
    const other = session("opaque-id", "custom", { id: "two", name: "Second client" });
    const onSelect = vi.fn();
    await act(async () => root.render(<ConversationList
      sessions={[terminal, session("dashboard"), other, session("lark", "lark"),
        session("telegram", "telegram"), session("unknown", "custom")]}
      error="" selected={terminal.session_id} onSelect={onSelect} onRefresh={() => {}} />));

    const groups = Array.from(container.querySelectorAll("section"));
    expect(groups.map((group) => group.querySelector("h3")?.textContent))
      .toEqual(["Dashboard chat", "loopercli", "Lark", "Telegram", "Other"]);
    expect(groups[0].querySelectorAll("li")).toHaveLength(1);
    expect(groups[0].textContent).toContain("dashboard");
    expect(groups[0].textContent).not.toContain("via ");
    expect(groups[1].querySelectorAll("li")).toHaveLength(2);
    expect(groups[1].textContent).toContain("via Terminal client");
    expect(groups[1].textContent).toContain("via Second client");
    expect(groups[4].querySelectorAll("li")).toHaveLength(1);
    const button = groups[1].querySelector("button")!;
    expect(button.getAttribute("aria-current")).toBe("true");
    await act(async () => button.click());
    expect(onSelect).toHaveBeenCalledExactlyOnceWith(terminal);
  });

  it("mentions loopercli when there are no conversations", async () => {
    await act(async () => root.render(<ConversationList sessions={[]} error="" selected=""
      onSelect={() => {}} onRefresh={() => {}} />));
    expect(container.textContent).toContain(
      "No conversations yet. Each chat with the Manager, here, in Lark or Telegram, or through loopercli, is listed once it has a message.",
    );
  });
});
