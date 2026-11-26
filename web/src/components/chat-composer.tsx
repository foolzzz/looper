import { Send, Square, X } from "lucide-react";
import { useEffect, useLayoutEffect, useRef, type KeyboardEvent } from "react";
import { Button } from "@/components/ui/button";
import { composerKeyAction, joinStaged, pullBack, STAGED_MAX, type ComposerState } from "@/lib/composer";

export interface ComposerCopy {
  idle: string; replying: string; stoppable: string; full: string;
  queued: string; edit: string; remove: string; message: string;
  stop: string; stopReason: string; queue: string; queueButton: string; send: string;
}

export interface ChatComposerProps {
  draft: string;
  onDraft: (draft: string) => void;
  staged: string[];
  onStaged: (staged: string[]) => void;
  history: string[];
  replying: boolean;
  canStop: boolean;
  /** Why a reply cannot be stopped though stopping is on (its runtime has no interrupt): Stop shows disabled with it. */
  stopRefusal?: string;
  onSend: (text: string) => void;
  onStop: () => void;
  notice?: string;
  disabled?: boolean;
  /** Send and Queue stay primary unless a page asks for neutral controls. */
  controls?: "primary" | "neutral";
  copy?: ComposerCopy;
}

const IDLE_PLACEHOLDER = "Type a message... (Shift+Enter for a new line)";
const REPLYING_PLACEHOLDER = "Replying… Enter queues";
const REPLYING_STOPPABLE_PLACEHOLDER = "Replying… Enter queues, Esc stops";
const FULL_NOTICE = "Queue is full";
const DEFAULT_COPY: ComposerCopy = {
  idle: IDLE_PLACEHOLDER, replying: REPLYING_PLACEHOLDER, stoppable: REPLYING_STOPPABLE_PLACEHOLDER,
  full: FULL_NOTICE, queued: "Queued · sent when the reply ends", edit: "Edit", remove: "Remove queued message",
  message: "Message", stop: "Stop the reply (Esc)", stopReason: "Stop the reply", queue: "Queue message", queueButton: "Queue", send: "Send",
};

export function ChatComposer(props: ChatComposerProps) {
  const { draft, onDraft, staged, onStaged, history, replying, canStop, stopRefusal, onSend, onStop, notice, disabled, controls } =
    props;
  const copy = props.copy ?? DEFAULT_COPY;
  const submitVariant = controls === "neutral" ? "outline" as const : undefined;
  const inputRef = useRef<HTMLTextAreaElement>(null);
  const browsingRef = useRef<{ index: number; saved: string } | null>(null);
  const shownRef = useRef<string | null>(null);
  const caretAtEnd = useRef(false);

  // A draft changed from outside (a failed send handing its text back) ends history browsing,
  // so a later Esc cannot restore the saved draft over it.
  useEffect(() => {
    if (browsingRef.current && draft !== shownRef.current) browsingRef.current = null;
  }, [draft]);

  // Auto-grow up to the max height set by the class.
  useLayoutEffect(() => {
    const el = inputRef.current;
    if (!el) return;
    el.style.height = "auto";
    el.style.height = `${el.scrollHeight}px`;
    if (caretAtEnd.current) {
      caretAtEnd.current = false;
      el.setSelectionRange(el.value.length, el.value.length);
    }
  }, [draft]);

  const stage = (text: string) => {
    onStaged([...staged, text]);
    onDraft("");
    browsingRef.current = null;
  };

  const pull = (next: string) => {
    onStaged([]);
    browsingRef.current = null;
    onDraft(next);
    caretAtEnd.current = true;
  };

  const onKeyDown = (e: KeyboardEvent<HTMLTextAreaElement>) => {
    const el = e.currentTarget;
    const state: ComposerState = {
      draft,
      caretStart: el.selectionStart ?? draft.length,
      caretEnd: el.selectionEnd ?? draft.length,
      staged,
      history,
      browsing: browsingRef.current,
      replying,
      canStop,
    };
    const action = composerKeyAction(state, {
      key: e.key,
      shiftKey: e.shiftKey,
      ctrlKey: e.ctrlKey,
      altKey: e.altKey,
      metaKey: e.metaKey,
      isComposing: e.nativeEvent.isComposing,
      keyCode: e.keyCode,
    });
    if (action.type === "none") return;
    e.preventDefault();
    switch (action.type) {
      case "send":
        onStaged([]);
        browsingRef.current = null;
        onSend(action.text);
        break;
      case "stage":
        stage(action.text);
        break;
      case "stop":
        if (action.clearDraft) onDraft("");
        onStop();
        break;
      case "pull":
        pull(action.draft);
        break;
      case "show":
        browsingRef.current = action.browsing;
        shownRef.current = action.draft;
        caretAtEnd.current = true;
        onDraft(action.draft);
        break;
      case "restore":
        browsingRef.current = null;
        caretAtEnd.current = true;
        onDraft(action.draft);
        break;
      case "full":
      case "ignore":
        break;
    }
  };

  const hasDraft = draft.trim() !== "";
  const full = replying && hasDraft && staged.length >= STAGED_MAX;
  const shownNotice = notice ?? (full ? copy.full : undefined);

  const submit = () => {
    if (!hasDraft) return;
    if (replying) {
      if (!full) stage(draft.trim());
      return;
    }
    onStaged([]);
    browsingRef.current = null;
    onSend(joinStaged([...staged, draft]));
  };

  return (
    <div className="flex flex-col gap-2">
      {staged.length > 0 && (
        <div className="rounded-lg border border-border bg-muted/40 p-2 transition-opacity duration-150 motion-reduce:transition-none">
          <div className="mb-1 flex items-center justify-between gap-2">
            <span className="text-xs font-medium text-muted-foreground">{copy.queued}</span>
            <Button type="button" variant="ghost" size="sm" className="min-h-11" onClick={() => pull(pullBack(staged, draft))}>
              {copy.edit}
            </Button>
          </div>
          <ul className="flex max-h-[30vh] flex-col gap-1 overflow-y-auto">
            {staged.map((item, i) => (
              <li key={i} className="flex items-start gap-2">
                <span className="line-clamp-3 min-w-0 flex-1 whitespace-pre-wrap break-words text-sm">{item}</span>
                <Button
                  type="button"
                  variant="ghost"
                  size="icon"
                  className="h-11 w-11 shrink-0"
                  aria-label={copy.remove}
                  onClick={() => onStaged(staged.filter((_, j) => j !== i))}
                >
                  <X className="h-4 w-4" aria-hidden="true" />
                </Button>
              </li>
            ))}
          </ul>
        </div>
      )}
      <form
        onSubmit={(e) => {
          e.preventDefault();
          submit();
        }}
        className="flex items-end gap-2"
      >
        <textarea
          ref={inputRef}
          rows={1}
          value={draft}
          disabled={disabled}
          onChange={(e) => {
            browsingRef.current = null;
            onDraft(e.target.value);
          }}
          onKeyDown={onKeyDown}
          placeholder={replying ? (canStop ? copy.stoppable : copy.replying) : copy.idle}
          aria-label={copy.message}
          className="max-h-40 min-h-11 flex-1 resize-none overflow-y-auto rounded-lg border border-input bg-background px-4 py-2.5 text-sm leading-5 text-foreground placeholder:text-muted-foreground outline-none focus:ring-2 focus:ring-ring"
          autoFocus
        />
        {replying ? (
          <>
            {canStop ? (
              <Button type="button" variant="destructive" className="h-11 w-11 shrink-0 p-0" aria-label={copy.stop} onClick={onStop}>
                <Square className="h-4 w-4 fill-current" aria-hidden="true" />
              </Button>
            ) : (
              stopRefusal && (
                // A disabled button gets no pointer events: the wrapper carries the tooltip.
                <span title={stopRefusal} className="shrink-0">
                  <Button type="button" variant="destructive" className="h-11 w-11 p-0" aria-label={`${copy.stopReason}: ${stopRefusal}`} disabled>
                    <Square className="h-4 w-4 fill-current" aria-hidden="true" />
                  </Button>
                </span>
              )
            )}
            <Button type="submit" variant={submitVariant} className="h-11 min-w-11 shrink-0" aria-label={copy.queue} disabled={disabled || !hasDraft || full}>
              {copy.queueButton}
            </Button>
          </>
        ) : (
          <Button type="submit" variant={submitVariant} className="h-11 w-11 p-0" aria-label={copy.send} disabled={disabled || !hasDraft}>
            <Send className="h-4 w-4" aria-hidden="true" />
          </Button>
        )}
      </form>
      {shownNotice && (
        <p role="status" className="text-xs text-muted-foreground">
          {shownNotice}
        </p>
      )}
    </div>
  );
}
