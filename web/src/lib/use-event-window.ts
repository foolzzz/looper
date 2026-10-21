import { useCallback, useEffect, useLayoutEffect, useRef, useState } from "react";
import type { ActivityEvent, EventWindow } from "@/lib/api";

/** How near the top (px) a scroll asks for the earlier page. */
const TOP_EDGE = 120;
/** How near the bottom (px) still counts as being at the bottom. */
const BOTTOM_EDGE = 40;

const bySeq = (a: ActivityEvent, b: ActivityEvent) => a.seq - b.seq;

/** A task's events as a window on their tail: the latest page is read at once, the page before the oldest loaded
 * one when the scroll nears the top (its place anchored, so the reading does not jump). The view follows new events
 * only while it is at the bottom. `latestSeq` is the newest loaded seq once the first page is read (0 when it could
 * not be), null before: where a stream should start. */
export function useEventWindow(
  key: string,
  fetchPage: (before: number | undefined, signal: AbortSignal) => Promise<EventWindow>,
) {
  const [events, setEvents] = useState<ActivityEvent[]>([]);
  const [hasMore, setHasMore] = useState(false);
  const [loadingEarlier, setLoadingEarlier] = useState(false);
  const [latestSeq, setLatestSeq] = useState<number | null>(null);
  const scrollRef = useRef<HTMLDivElement>(null);
  const anchor = useRef<{ height: number; top: number } | null>(null);
  const atBottom = useRef(true);
  const busy = useRef(false);
  const state = useRef({ events, hasMore });
  state.current = { events, hasMore };
  const fetchRef = useRef(fetchPage);
  fetchRef.current = fetchPage;
  const controller = useRef<AbortController | null>(null);

  useEffect(() => {
    const abort = new AbortController();
    controller.current = abort;
    setEvents([]);
    setHasMore(false);
    setLoadingEarlier(false);
    setLatestSeq(null);
    atBottom.current = true;
    anchor.current = null;
    busy.current = false;
    fetchRef.current(undefined, abort.signal).then(
      (page) => {
        if (abort.signal.aborted) return;
        const rows = [...page.events].sort(bySeq);
        setEvents(rows);
        setHasMore(page.hasMore);
        setLatestSeq(rows.length ? rows[rows.length - 1].seq : 0);
      },
      () => {
        if (!abort.signal.aborted) setLatestSeq(0);
      },
    );
    return () => abort.abort();
  }, [key]);

  const loadEarlier = useCallback(() => {
    const { events: loaded, hasMore: more } = state.current;
    const abort = controller.current;
    if (busy.current || !more || !loaded.length || !abort) return;
    busy.current = true;
    setLoadingEarlier(true);
    fetchRef.current(loaded[0].seq, abort.signal).then(
      (page) => {
        if (abort.signal.aborted) return;
        busy.current = false;
        const el = scrollRef.current;
        anchor.current = el ? { height: el.scrollHeight, top: el.scrollTop } : null;
        setEvents((prev) => {
          const have = new Set(prev.map((e) => e.seq));
          return [...page.events.filter((e) => !have.has(e.seq)), ...prev].sort(bySeq);
        });
        setHasMore(page.hasMore);
        setLoadingEarlier(false);
      },
      () => {
        if (abort.signal.aborted) return;
        busy.current = false;
        setLoadingEarlier(false);
      },
    );
  }, []);

  const append = useCallback((event: ActivityEvent) => {
    setEvents((prev) => (prev.some((e) => e.seq === event.seq) ? prev : [...prev, event]));
  }, []);

  // After events change: keep the reading place when earlier ones were put above it; else follow the bottom.
  useLayoutEffect(() => {
    const el = scrollRef.current;
    if (!el) return;
    const kept = anchor.current;
    if (kept) {
      anchor.current = null;
      el.scrollTop = kept.top + (el.scrollHeight - kept.height);
    } else if (atBottom.current) {
      el.scrollTop = el.scrollHeight;
    }
  }, [events]);

  const onScroll = useCallback(() => {
    const el = scrollRef.current;
    if (!el) return;
    atBottom.current = el.scrollHeight - el.scrollTop - el.clientHeight <= BOTTOM_EDGE;
    if (el.scrollTop <= TOP_EDGE) loadEarlier();
  }, [loadEarlier]);

  return { events, hasMore, loadingEarlier, latestSeq, scrollRef, onScroll, append };
}
