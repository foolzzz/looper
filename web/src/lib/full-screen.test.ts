import { describe, expect, it, vi } from "vitest";
import { type FullScreenHost, type FullScreenMode, fullScreen } from "@/lib/full-screen";

/** A document whose Fullscreen API answers as `api` says: present or not, granting the request or refusing it. */
function setup(api: "grants" | "refuses" | "missing" | "disabled" = "grants") {
  const listeners = new Set<() => void>();
  const host = {
    fullscreenElement: null as Element | null,
    fullscreenEnabled: api !== "disabled",
    exitFullscreen: vi.fn(async () => {
      host.fullscreenElement = null;
      listeners.forEach((listener) => listener());
    }),
    addEventListener: (_: string, listener: () => void) => listeners.add(listener),
    removeEventListener: (_: string, listener: () => void) => listeners.delete(listener),
  } satisfies FullScreenHost;
  const target = {
    requestFullscreen: api === "missing" ? undefined : vi.fn(async () => {
      if (api === "refuses") throw new TypeError("Permissions check failed");
      host.fullscreenElement = target as unknown as Element;
      listeners.forEach((listener) => listener());
    }),
  };
  const modes: FullScreenMode[] = [];
  const control = fullScreen(target as unknown as Element, host, (mode) => modes.push(mode));
  // The user leaving through the browser: its own Esc, or a gesture.
  const leaveInBrowser = () => {
    host.fullscreenElement = null;
    listeners.forEach((listener) => listener());
  };
  return { host, target, modes, control, listeners, leaveInBrowser };
}

const escape = (defaultPrevented = false) =>
  ({ key: "Escape", defaultPrevented, preventDefault: vi.fn(), stopImmediatePropagation: vi.fn() });

describe("fullScreen", () => {
  it("enters through the browser's Fullscreen API, and leaves through it", async () => {
    const { host, target, modes, control } = setup();
    await control.enter();
    expect(target.requestFullscreen).toHaveBeenCalledOnce();
    expect(control.mode()).toBe("api");
    await control.exit();
    expect(host.exitFullscreen).toHaveBeenCalledOnce();
    expect(modes).toEqual(["api", "off"]);
  });

  it.each(["refuses", "missing", "disabled"] as const)("lays an overlay over the page when the API %s", async (api) => {
    const { host, modes, control } = setup(api);
    await control.enter();
    expect(control.mode()).toBe("overlay");
    await control.exit();
    expect(host.exitFullscreen).not.toHaveBeenCalled();
    expect(modes).toEqual(["overlay", "off"]);
  });

  it("follows the user leaving through the browser", async () => {
    const { control, modes, leaveInBrowser } = setup();
    await control.enter();
    leaveInBrowser();
    expect(control.mode()).toBe("off");
    expect(modes).toEqual(["api", "off"]);
  });

  it("leaves on Esc, which goes no further, in either mode", async () => {
    for (const api of ["grants", "missing"] as const) {
      const { control } = setup(api);
      await control.enter();
      const key = escape();
      control.key(key);
      await Promise.resolve();
      expect(control.mode()).toBe("off");
      expect(key.preventDefault).toHaveBeenCalled();
      expect(key.stopImmediatePropagation).toHaveBeenCalled();
    }
  });

  it("leaves Esc and other keys alone while it is off, and other keys while it is on", async () => {
    const { control } = setup();
    const off = escape();
    control.key(off);
    expect(off.stopImmediatePropagation).not.toHaveBeenCalled();
    await control.enter();
    const other = { ...escape(), key: "a" };
    control.key(other);
    expect(other.stopImmediatePropagation).not.toHaveBeenCalled();
    expect(control.mode()).toBe("api");
  });

  it("leaves an Esc a node's card took (it closed the card) to the card, and stays", async () => {
    const { control } = setup("missing");
    await control.enter();
    const taken = escape(true);
    control.key(taken);
    await Promise.resolve();
    expect(control.mode()).toBe("overlay");
    expect(taken.stopImmediatePropagation).not.toHaveBeenCalled();
  });

  it("stops listening, and leaves the browser's full screen, when disposed", async () => {
    const { host, control, listeners } = setup();
    await control.enter();
    control.dispose();
    expect(listeners.size).toBe(0);
    expect(host.exitFullscreen).toHaveBeenCalledOnce();
  });
});
