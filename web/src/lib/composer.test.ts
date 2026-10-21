import { describe, expect, it } from "vitest";
import {
  composerKeyAction,
  historyFrom,
  isStopCommand,
  joinStaged,
  pullBack,
  type ComposerKey,
  type ComposerState,
} from "./composer";

const key = (k: string, o: Partial<ComposerKey> = {}): ComposerKey => ({
  key: k, shiftKey: false, ctrlKey: false, altKey: false, metaKey: false, isComposing: false, keyCode: 0, ...o,
});
const st = (o: Partial<ComposerState> = {}): ComposerState => {
  const draft = o.draft ?? "";
  return {
    draft, caretStart: draft.length, caretEnd: draft.length, staged: [], history: [],
    browsing: null, replying: false, canStop: false, ...o,
  };
};

describe("guards", () => {
  for (const k of ["Enter", "ArrowUp", "Escape"]) {
    it(`${k} during IME composition does nothing`, () => {
      const s = st({ draft: "你好", replying: true, canStop: true, history: ["a"], browsing: { index: 0, saved: "" } });
      expect(composerKeyAction(s, key(k, { isComposing: true }))).toEqual({ type: "none" });
      expect(composerKeyAction(s, key(k, { keyCode: 229 }))).toEqual({ type: "none" });
    });
  }
  it("modifiers are ignored", () => {
    for (const m of ["ctrlKey", "altKey", "metaKey"] as const) {
      expect(composerKeyAction(st({ draft: "x" }), key("Enter", { [m]: true }))).toEqual({ type: "none" });
    }
  });
});

describe("Enter", () => {
  it("Shift+Enter is a newline", () => {
    expect(composerKeyAction(st({ draft: "x" }), key("Enter", { shiftKey: true }))).toEqual({ type: "none" });
  });
  it("replying + /stop stops and clears the draft", () => {
    expect(composerKeyAction(st({ draft: " /STOP ", replying: true }), key("Enter"))).toEqual({ type: "stop", clearDraft: true });
  });
  it("replying + draft stages", () => {
    expect(composerKeyAction(st({ draft: "hi ", replying: true }), key("Enter"))).toEqual({ type: "stage", text: "hi" });
  });
  it("replying + full queue", () => {
    const staged = Array.from({ length: 20 }, (_, i) => `m${i}`);
    expect(composerKeyAction(st({ draft: "hi", replying: true, staged }), key("Enter"))).toEqual({ type: "full" });
    expect(composerKeyAction(st({ draft: "hi", replying: true, staged: staged.slice(1) }), key("Enter")).type).toBe("stage");
  });
  it("replying + empty is ignored", () => {
    expect(composerKeyAction(st({ draft: "  ", replying: true }), key("Enter"))).toEqual({ type: "ignore" });
  });
  it("idle + draft sends, merging staged", () => {
    expect(composerKeyAction(st({ draft: "c" }), key("Enter"))).toEqual({ type: "send", text: "c" });
    expect(composerKeyAction(st({ draft: "c", staged: ["a", "b"] }), key("Enter"))).toEqual({ type: "send", text: "a\n\nb\n\nc" });
  });
  it("idle + empty is ignored", () => {
    expect(composerKeyAction(st(), key("Enter"))).toEqual({ type: "ignore" });
  });
});

describe("ArrowUp", () => {
  it("non-collapsed selection acts natively", () => {
    expect(composerKeyAction(st({ draft: "abc", caretStart: 0, caretEnd: 2, history: ["h"] }), key("ArrowUp"))).toEqual({ type: "none" });
  });
  it("caret on line 2 is none, on line 1 acts", () => {
    const base = { draft: "one\ntwo", history: ["h"] };
    expect(composerKeyAction(st({ ...base, caretStart: 6, caretEnd: 6 }), key("ArrowUp"))).toEqual({ type: "none" });
    expect(composerKeyAction(st({ ...base, caretStart: 2, caretEnd: 2 }), key("ArrowUp")).type).toBe("show");
  });
  it("browsing moves older and stays at the oldest", () => {
    const history = ["a", "b", "c"];
    expect(composerKeyAction(st({ draft: "c", history, browsing: { index: 2, saved: "s" } }), key("ArrowUp"))).toEqual({
      type: "show", draft: "b", browsing: { index: 1, saved: "s" },
    });
    expect(composerKeyAction(st({ draft: "a", history, browsing: { index: 0, saved: "s" } }), key("ArrowUp"))).toEqual({
      type: "show", draft: "a", browsing: { index: 0, saved: "s" },
    });
  });
  it("staged non-empty pulls back before history", () => {
    expect(composerKeyAction(st({ draft: "d", staged: ["a", "b"], history: ["h"] }), key("ArrowUp"))).toEqual({
      type: "pull", draft: "a\n\nb\n\nd",
    });
  });
  it("history enters browsing at the newest and saves the draft", () => {
    expect(composerKeyAction(st({ draft: "wip", history: ["a", "b"] }), key("ArrowUp"))).toEqual({
      type: "show", draft: "b", browsing: { index: 1, saved: "wip" },
    });
  });
  it("nothing to recall is none", () => {
    expect(composerKeyAction(st(), key("ArrowUp"))).toEqual({ type: "none" });
  });
  it("a recalled multi-line entry: caret at end is on the last line, so ArrowUp moves natively", () => {
    const draft = "l1\nl2";
    const s = st({ draft, history: ["x", draft], browsing: { index: 1, saved: "" } });
    expect(composerKeyAction(s, key("ArrowUp"))).toEqual({ type: "none" });
    expect(composerKeyAction({ ...s, caretStart: 1, caretEnd: 1 }, key("ArrowUp")).type).toBe("show");
  });
});

describe("ArrowDown", () => {
  it("not browsing is none", () => {
    expect(composerKeyAction(st({ draft: "x", history: ["a"] }), key("ArrowDown"))).toEqual({ type: "none" });
  });
  it("newline after the caret is none", () => {
    const s = st({ draft: "a\nb", caretStart: 0, caretEnd: 0, history: ["a", "a\nb"], browsing: { index: 0, saved: "" } });
    expect(composerKeyAction(s, key("ArrowDown"))).toEqual({ type: "none" });
  });
  it("at the newest restores the saved draft", () => {
    expect(composerKeyAction(st({ draft: "b", history: ["a", "b"], browsing: { index: 1, saved: "s" } }), key("ArrowDown"))).toEqual({
      type: "restore", draft: "s",
    });
  });
  it("otherwise shows the newer entry", () => {
    expect(composerKeyAction(st({ draft: "a", history: ["a", "b"], browsing: { index: 0, saved: "s" } }), key("ArrowDown"))).toEqual({
      type: "show", draft: "b", browsing: { index: 1, saved: "s" },
    });
  });
});

describe("Escape", () => {
  it("replying + canStop stops, keeping the draft", () => {
    expect(composerKeyAction(st({ replying: true, canStop: true }), key("Escape"))).toEqual({ type: "stop", clearDraft: false });
  });
  it("replying without canStop falls through to browsing", () => {
    const s = st({ replying: true, canStop: false, browsing: { index: 0, saved: "s" }, history: ["a"] });
    expect(composerKeyAction(s, key("Escape"))).toEqual({ type: "restore", draft: "s" });
  });
  it("browsing restores", () => {
    expect(composerKeyAction(st({ browsing: { index: 0, saved: "s" }, history: ["a"] }), key("Escape"))).toEqual({ type: "restore", draft: "s" });
  });
  it("otherwise none", () => {
    expect(composerKeyAction(st({ draft: "keep" }), key("Escape"))).toEqual({ type: "none" });
  });
});

describe("helpers", () => {
  it("joinStaged drops blanks and trims", () => {
    expect(joinStaged([" a ", "", "  ", "b"])).toBe("a\n\nb");
  });
  it("pullBack appends a non-empty draft", () => {
    expect(pullBack(["a"], "")).toBe("a");
    expect(pullBack(["a"], "d")).toBe("a\n\nd");
  });
  it("historyFrom collapses only adjacent duplicates", () => {
    const msgs = [
      { role: "user", content: "a" }, { role: "agent", content: "r" }, { role: "user", content: " a " },
      { role: "user", content: "b" }, { role: "user", content: "  " }, { role: "user", content: "a" },
    ];
    expect(historyFrom(msgs)).toEqual(["a", "b", "a"]);
  });
  it("historyFrom keeps the newest 100", () => {
    const msgs = Array.from({ length: 130 }, (_, i) => ({ role: "user", content: `m${i}` }));
    const h = historyFrom(msgs);
    expect(h).toHaveLength(100);
    expect(h[0]).toBe("m30");
    expect(h[99]).toBe("m129");
  });
  it("isStopCommand", () => {
    expect(isStopCommand("/STOP ")).toBe(true);
    expect(isStopCommand("/stop now")).toBe(false);
  });
});
