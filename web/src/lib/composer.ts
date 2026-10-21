import type { ChatTurn } from "./api";

export const STAGED_MAX = 20;
export const HISTORY_MAX = 100;
export const STAGED_SEPARATOR = "\n\n";

export function joinStaged(items: string[]): string {
  return items
    .map((item) => item.trim())
    .filter((item) => item !== "")
    .join(STAGED_SEPARATOR);
}

export function pullBack(staged: string[], draft: string): string {
  const joined = joinStaged(staged);
  return draft.trim() === "" ? joined : `${joined}${STAGED_SEPARATOR}${draft}`;
}

export function historyFrom(messages: { role: string; content: string }[]): string[] {
  const out: string[] = [];
  for (const m of messages) {
    if (m.role !== "user") continue;
    const text = m.content.trim();
    if (text === "" || out[out.length - 1] === text) continue;
    out.push(text);
  }
  return out.slice(-HISTORY_MAX);
}

export function isStopCommand(text: string): boolean {
  return text.trim().toLowerCase() === "/stop";
}

export interface ComposerState {
  draft: string;
  caretStart: number;
  caretEnd: number;
  staged: string[];
  history: string[];
  browsing: { index: number; saved: string } | null;
  replying: boolean;
  canStop: boolean;
}

export interface ComposerKey {
  key: string;
  shiftKey: boolean;
  ctrlKey: boolean;
  altKey: boolean;
  metaKey: boolean;
  isComposing: boolean;
  keyCode: number;
}

export type Action =
  | { type: "none" }
  | { type: "send"; text: string }
  | { type: "stage"; text: string }
  | { type: "stop"; clearDraft: boolean }
  | { type: "pull"; draft: string }
  | { type: "show"; draft: string; browsing: { index: number; saved: string } }
  | { type: "restore"; draft: string }
  | { type: "full" }
  | { type: "ignore" };

const NONE: Action = { type: "none" };

export function composerKeyAction(state: ComposerState, k: ComposerKey): Action {
  if (k.isComposing || k.keyCode === 229) return NONE;
  if (k.ctrlKey || k.altKey || k.metaKey) return NONE;
  // Shift+arrow extends the native selection; never treat it as history navigation.
  if (k.shiftKey && (k.key === "ArrowUp" || k.key === "ArrowDown")) return NONE;
  const { draft, staged, history, browsing } = state;
  const hasDraft = draft.trim() !== "";

  switch (k.key) {
    case "Enter":
      if (k.shiftKey) return NONE;
      if (state.replying) {
        if (isStopCommand(draft)) return { type: "stop", clearDraft: true };
        if (!hasDraft) return { type: "ignore" };
        if (staged.length >= STAGED_MAX) return { type: "full" };
        return { type: "stage", text: draft.trim() };
      }
      if (!hasDraft) return { type: "ignore" };
      return { type: "send", text: joinStaged([...staged, draft]) };
    case "ArrowUp": {
      if (state.caretStart !== state.caretEnd) return NONE;
      if (draft.slice(0, state.caretStart).includes("\n")) return NONE;
      if (browsing) {
        const index = Math.max(0, browsing.index - 1);
        return { type: "show", draft: history[index] ?? draft, browsing: { index, saved: browsing.saved } };
      }
      if (staged.length > 0) return { type: "pull", draft: pullBack(staged, draft) };
      if (history.length > 0) {
        const index = history.length - 1;
        return { type: "show", draft: history[index], browsing: { index, saved: draft } };
      }
      return NONE;
    }
    case "ArrowDown": {
      if (!browsing) return NONE;
      if (draft.slice(state.caretEnd).includes("\n")) return NONE;
      if (browsing.index >= history.length - 1) return { type: "restore", draft: browsing.saved };
      const index = browsing.index + 1;
      return { type: "show", draft: history[index], browsing: { index, saved: browsing.saved } };
    }
    case "Escape":
      if (state.replying && state.canStop) return { type: "stop", clearDraft: false };
      if (browsing) return { type: "restore", draft: browsing.saved };
      return NONE;
    default:
      return NONE;
  }
}

/** Does the running turn answer one of these message ids? The page knows its own id, the server's envelope has another. */
export function turnAnswers(turn: ChatTurn | null, ids: readonly string[]): boolean {
  if (!turn) return false;
  return ids.includes(turn.message_id) || (turn.client_message_id !== null && ids.includes(turn.client_message_id));
}
