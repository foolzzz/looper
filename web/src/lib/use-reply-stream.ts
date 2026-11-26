import { useEffect, useRef } from "react";
import { sendChat, streamChatReply, type ChatTarget, type SSEEvent } from "@/lib/api";

/** The writable chat owner a send was started for. */
export interface ReplyStreamOwner {
  id: string;
  threadId: string;
}

export interface ReplyStreamStart {
  /** Captured `sendChat` target. */
  target: ChatTarget;
  /** Captured thread id. */
  threadId: string;
  /** Captured client message id. */
  messageId: string;
  text: string;
  options?: { newSessionProfileId?: string };
  /** Captured writable owner id (`target.id` on the Chat page). */
  ownerId: string;
  onEvent: (event: SSEEvent) => void;
  onDone: () => void;
  onError: (error: unknown) => void;
  /**
   * Runs after the send is accepted and before the owner/generation gate.
   * The Chat page uses it to drop the captured thread's local mark, which the
   * old send did even when a later navigation had already made the result stale.
   */
  onAccepted?: () => void;
  /** Set synchronously to this send's owner and generation check, for updates that run later. */
  isCurrent?: { current: () => boolean };
}

interface ActiveSend {
  generation: number;
  cancel: () => void;
}

/**
 * Sends one chat message and follows its reply stream.
 * `cancel` aborts the browser request and the reply subscription. It does not
 * stop the Manager's turn. A resolution, rejection, event or done is delivered
 * only while this send's owner and generation are still current, so a final
 * that starts the next send cannot let the previous close clear that turn.
 */
export function useReplyStream(getOwner: () => ReplyStreamOwner): {
  start: (input: ReplyStreamStart) => () => void;
  cancel: () => void;
} {
  const getOwnerRef = useRef(getOwner);
  getOwnerRef.current = getOwner;
  const generation = useRef(0);
  const active = useRef<ActiveSend | null>(null);

  const cancel = () => {
    active.current?.cancel();
  };

  // Unmount is the only implicit cancel. The page cancels on a new writable owner itself.
  useEffect(() => () => {
    active.current?.cancel();
  }, []);

  const start = (input: ReplyStreamStart) => {
    active.current?.cancel();
    const gen = ++generation.current;
    const controller = new AbortController();
    const captured = { id: input.ownerId, threadId: input.threadId };
    let close: (() => void) | undefined;
    let stopped = false;
    const current = () => {
      if (stopped || gen !== generation.current || controller.signal.aborted) return false;
      const live = getOwnerRef.current();
      return live.id === captured.id && live.threadId === captured.threadId;
    };
    const stop = () => {
      if (stopped) return;
      stopped = true;
      controller.abort();
      const unsubscribe = close;
      if (active.current?.generation === gen) active.current = null;
      unsubscribe?.();
    };
    // Installed before awaiting sendChat: navigation owns cancellation throughout the send's lifetime.
    active.current = { generation: gen, cancel: stop };
    if (input.isCurrent) input.isCurrent.current = current;

    void (async () => {
      try {
        const { correlation_id } = await sendChat(input.text, input.threadId, input.messageId, input.target, {
          newSessionProfileId: input.options?.newSessionProfileId,
          signal: controller.signal,
        });
        input.onAccepted?.();
        if (!current()) return;
        close = streamChatReply(
          correlation_id,
          (event) => {
            if (!current()) return;
            input.onEvent(event);
          },
          () => {
            if (!current()) return;
            input.onDone();
          },
        );
        // A synchronous final may already have started the next send, before `close` was assigned.
        if (!current()) close();
      } catch (error) {
        if (!current()) return;
        input.onError(error);
      }
    })();
    return stop;
  };

  return { start, cancel };
}
