// @vitest-environment happy-dom
import { act, useState } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { ChatComposer } from "@/components/chat-composer";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

describe("ChatComposer", () => {
  let container: HTMLDivElement;
  let root: Root;
  let onSend: ReturnType<typeof vi.fn>;
  let onStop: ReturnType<typeof vi.fn>;
  let seen: { draft: string; staged: string[] };
  let setOuterDraft: (d: string) => void;

  function mount(init: { draft?: string; staged?: string[]; history?: string[]; replying?: boolean; canStop?: boolean }) {
    function Harness() {
      const [draft, setDraft] = useState(init.draft ?? "");
      const [staged, setStaged] = useState(init.staged ?? []);
      seen = { draft, staged };
      setOuterDraft = setDraft;
      return (
        <ChatComposer
          draft={draft}
          onDraft={setDraft}
          staged={staged}
          onStaged={setStaged}
          history={init.history ?? []}
          replying={init.replying ?? false}
          canStop={init.canStop ?? false}
          onSend={onSend as (t: string) => void}
          onStop={onStop as () => void}
        />
      );
    }
    act(() => root.render(<Harness />));
  }

  const input = () => container.querySelector("textarea") as HTMLTextAreaElement;
  const key = (k: string, extra: Record<string, unknown> = {}) =>
    act(() => {
      input().dispatchEvent(new KeyboardEvent("keydown", { key: k, bubbles: true, cancelable: true, ...extra }));
    });
  const button = (label: string) =>
    [...container.querySelectorAll("button")].find((b) => b.textContent === label || b.getAttribute("aria-label") === label) as HTMLButtonElement;

  beforeEach(() => {
    container = document.createElement("div");
    document.body.appendChild(container);
    root = createRoot(container);
    onSend = vi.fn();
    onStop = vi.fn();
  });
  afterEach(() => {
    act(() => root.unmount());
    container.remove();
  });

  it("Enter while replying stages, clears the draft, shows the item; remove and Edit work", () => {
    mount({ draft: "hello", replying: true });
    key("Enter");
    expect(seen).toEqual({ draft: "", staged: ["hello"] });
    expect(container.textContent).toContain("Queued · sent when the reply ends");
    expect(container.textContent).toContain("hello");
    act(() => button("Remove queued message").click());
    expect(seen.staged).toEqual([]);
    expect(container.textContent).not.toContain("Queued ·");

    mount({ staged: ["a", "b"], draft: "c", replying: true });
    act(() => button("Edit").click());
    expect(seen).toEqual({ draft: "a\n\nb\n\nc", staged: [] });
  });

  it("ArrowUp pulls back staged, else walks history; Escape restores the draft", () => {
    mount({ staged: ["a"], history: ["old"], replying: true });
    key("ArrowUp");
    expect(seen).toEqual({ draft: "a", staged: [] });

    mount({ draft: "mine", history: ["one", "two"] });
    key("ArrowUp");
    expect(seen.draft).toBe("two");
    key("ArrowUp");
    expect(seen.draft).toBe("one");
    key("Escape");
    expect(seen.draft).toBe("mine");
  });

  it("a draft handed back from outside ends history browsing, so Escape keeps it", () => {
    mount({ draft: "mine", history: ["one", "two"] });
    key("ArrowUp");
    expect(seen.draft).toBe("two");
    act(() => setOuterDraft("failed text"));
    key("Escape");
    expect(seen.draft).toBe("failed text");
  });

  it("Escape and the Stop button stop; the Queue button stages", () => {
    mount({ replying: true, canStop: true });
    key("Escape");
    expect(onStop).toHaveBeenCalledTimes(1);
    act(() => button("Stop the reply (Esc)").click());
    expect(onStop).toHaveBeenCalledTimes(2);

    mount({ draft: "later", replying: true, canStop: true });
    act(() => button("Queue message").click());
    expect(seen).toEqual({ draft: "", staged: ["later"] });
  });

  it("a touch user can stop the reply with text in the box: Stop sits beside Queue", () => {
    mount({ draft: "later", replying: true, canStop: true });
    expect(button("Queue message")).toBeTruthy();
    act(() => button("Stop the reply (Esc)").click());
    expect(onStop).toHaveBeenCalledTimes(1);
    expect(seen).toEqual({ draft: "later", staged: [] });
  });

  it("a long queue scrolls inside the staging area instead of pushing the thread off the screen", () => {
    mount({ staged: Array.from({ length: 20 }, (_, i) => `queued ${i + 1}`), replying: true, canStop: true });
    const list = container.querySelector("ul") as HTMLElement;
    expect(list.className).toMatch(/max-h-/);
    expect(list.className).toContain("overflow-y-auto");
  });

  it("does nothing while an input method is composing", () => {
    mount({ draft: "x", history: ["h"], staged: [], replying: true, canStop: true });
    for (const k of ["Enter", "ArrowUp", "Escape"]) key(k, { isComposing: true });
    expect(seen).toEqual({ draft: "x", staged: [] });
    expect(onSend).not.toHaveBeenCalled();
    expect(onStop).not.toHaveBeenCalled();
  });

  it("idle Enter sends the trimmed draft", () => {
    mount({ draft: "  hi  " });
    key("Enter");
    expect(onSend).toHaveBeenCalledWith("hi");
  });
});
