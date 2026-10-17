/** An element shown full screen: through the browser's Fullscreen API, else (no API, or the browser refused) as a
 * fixed overlay over the whole viewport. */
import { type RefObject, useCallback, useEffect, useRef, useState } from "react";

/** "api": the browser shows the element full screen; "overlay": the page lays it over the viewport. */
export type FullScreenMode = "off" | "api" | "overlay";

/** The part of the document the Fullscreen API is on. */
export interface FullScreenHost {
  readonly fullscreenElement: Element | null;
  readonly fullscreenEnabled: boolean;
  exitFullscreen(): Promise<void>;
  addEventListener(type: "fullscreenchange", listener: () => void): void;
  removeEventListener(type: "fullscreenchange", listener: () => void): void;
}

export interface FullScreen {
  mode(): FullScreenMode;
  enter(): Promise<void>;
  exit(): Promise<void>;
  /** A key pressed on the page, heard last: while full screen, an Esc nothing else took (a node's card takes it
   * first) leaves it and goes no further. */
  key(event: Pick<KeyboardEvent, "key" | "defaultPrevented" | "preventDefault" | "stopImmediatePropagation">): void;
  dispose(): void;
}

/** Full screen for `target`; `onChange` hears each mode, also when the user leaves through the browser. */
export function fullScreen(
  target: Element & { requestFullscreen?: () => Promise<void> },
  host: FullScreenHost,
  onChange: (mode: FullScreenMode) => void,
): FullScreen {
  let mode: FullScreenMode = "off";
  let entering = false;
  const set = (next: FullScreenMode) => {
    if (next === mode) return;
    mode = next;
    onChange(next);
  };
  // The browser's own way out (its Esc, a gesture) ends the API mode; the overlay is the page's alone.
  const followed = () => set(host.fullscreenElement === target ? "api" : mode === "api" ? "off" : mode);
  host.addEventListener("fullscreenchange", followed);
  const exit = async () => {
    if (mode === "api" && host.fullscreenElement === target) {
      try {
        await host.exitFullscreen();
      } catch {
        // Already out.
      }
    }
    set("off");
  };

  return {
    mode: () => mode,
    async enter() {
      if (mode !== "off" || entering) return;
      entering = true;
      try {
        if (host.fullscreenEnabled && typeof target.requestFullscreen === "function") {
          try {
            await target.requestFullscreen();
          } catch {
            // Refused (no user gesture, a policy): the overlay instead.
          }
          if (host.fullscreenElement === target) return set("api");
        }
        set("overlay");
      } finally {
        entering = false;
      }
    },
    exit,
    key(event) {
      if (mode === "off" || event.key !== "Escape" || event.defaultPrevented) return;
      event.preventDefault();
      event.stopImmediatePropagation();
      void exit();
    },
    dispose() {
      host.removeEventListener("fullscreenchange", followed);
      if (host.fullscreenElement === target) host.exitFullscreen().catch(() => {});
    },
  };
}

/** Listens for full screen's Esc on `doc` after the rest of the page (the bubble phase), so a node's card, which
 * listens first, takes an Esc before full screen does. */
export function listenFullScreenKeys(
  doc: Pick<Document, "addEventListener" | "removeEventListener">,
  control: Pick<FullScreen, "key">,
): () => void {
  const listener = (event: KeyboardEvent) => control.key(event);
  doc.addEventListener("keydown", listener);
  return () => doc.removeEventListener("keydown", listener);
}

/** Full screen for the element `ref` holds: its mode, and a toggle. While it is on, Esc leaves it (after a node's
 * card, which takes it first) and reaches nothing else on the page, and the overlay keeps the page under it from
 * scrolling. */
export function useFullScreen(ref: RefObject<HTMLElement | null>): [FullScreenMode, () => void] {
  const [mode, setMode] = useState<FullScreenMode>("off");
  const control = useRef<FullScreen | null>(null);

  useEffect(() => {
    const element = ref.current;
    if (!element) return;
    const current = fullScreen(element, document, setMode);
    control.current = current;
    return () => {
      current.dispose();
      control.current = null;
    };
  }, [ref]);

  useEffect(() => {
    if (mode === "off") return;
    const stop = control.current ? listenFullScreenKeys(document, control.current) : () => {};
    const root = document.documentElement;
    const overflow = root.style.overflow;
    if (mode === "overlay") root.style.overflow = "hidden";
    return () => {
      stop();
      root.style.overflow = overflow;
    };
  }, [mode]);

  const toggle = useCallback(() => {
    const current = control.current;
    if (current) void (current.mode() === "off" ? current.enter() : current.exit());
  }, []);
  return [mode, toggle];
}
