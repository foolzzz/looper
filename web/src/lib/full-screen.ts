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
  /** A key pressed on the page, heard first: while full screen, Esc leaves it and goes no further. */
  key(event: Pick<KeyboardEvent, "key" | "preventDefault" | "stopImmediatePropagation">): void;
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
      if (mode === "off" || event.key !== "Escape") return;
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

/** Full screen for the element `ref` holds: its mode, and a toggle. While it is on, Esc leaves it and reaches
 * nothing else on the page, and the overlay keeps the page under it from scrolling. */
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
    const key = (event: KeyboardEvent) => control.current?.key(event);
    window.addEventListener("keydown", key, true);
    const root = document.documentElement;
    const overflow = root.style.overflow;
    if (mode === "overlay") root.style.overflow = "hidden";
    return () => {
      window.removeEventListener("keydown", key, true);
      root.style.overflow = overflow;
    };
  }, [mode]);

  const toggle = useCallback(() => {
    const current = control.current;
    if (current) void (current.mode() === "off" ? current.enter() : current.exit());
  }, []);
  return [mode, toggle];
}
