// @vitest-environment happy-dom
import { afterEach, describe, expect, it, vi } from "vitest";
import { cardKey, listenCardKeys } from "@/lib/hover-card";
import { type FullScreenHost, fullScreen, listenFullScreenKeys } from "@/lib/full-screen";

/** The page: the graph with a focusable node, a button elsewhere, and a listener the rest of the page keeps on the
 * window (as the sidebar drawer's Esc is). A card is shown for s1 until the cards' Esc closes it; the graph is full
 * screen in the overlay (a browser without the Fullscreen API). */
async function page({ fullScreenFirst = false } = {}) {
  document.body.innerHTML = '<div id="graph"><div id="node" tabindex="0"></div></div><button id="elsewhere">x</button>';
  const outside = vi.fn();
  window.addEventListener("keydown", outside);
  const state = { shown: "s1" as string | null };
  const host: FullScreenHost = {
    fullscreenElement: null, fullscreenEnabled: false, exitFullscreen: async () => {},
    addEventListener: () => {}, removeEventListener: () => {},
  };
  const control = fullScreen(document.getElementById("graph")!, host, () => {});
  await control.enter();
  const cards = () => listenCardKeys(document, (event) => cardKey(event.key, { shown: state.shown, focusInCard: false,
                                                                            focusedNode: null }),
                                     () => { state.shown = null; });
  const full = () => listenFullScreenKeys(document, control);
  const stops = fullScreenFirst ? [full(), cards()] : [cards(), full()];
  const press = (on = "node") =>
    document.getElementById(on)!.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape", bubbles: true, cancelable: true }));
  const stop = () => {
    stops.forEach((s) => s());
    window.removeEventListener("keydown", outside);
  };
  return { state, control, outside, press, stop };
}

describe("Esc, on a real document", () => {
  let stop = () => {};
  afterEach(() => stop());

  it.each([false, true])("closes the open card first, then leaves full screen, and reaches nothing else (full screen listening first: %s)",
    async (fullScreenFirst) => {
      const p = await page({ fullScreenFirst });
      stop = p.stop;
      p.press();
      expect(p.state.shown).toBeNull();
      expect(p.control.mode()).toBe("overlay");
      expect(p.outside).not.toHaveBeenCalled();
      p.press();
      expect(p.control.mode()).toBe("off");
      expect(p.outside).not.toHaveBeenCalled();
      p.press(); // nothing open: the key goes on to the page
      expect(p.outside).toHaveBeenCalledOnce();
    });

  it("closes a card wherever focus is", async () => {
    const p = await page();
    stop = p.stop;
    p.press("elsewhere");
    expect(p.state.shown).toBeNull();
    expect(p.outside).not.toHaveBeenCalled();
  });

  it("hears nothing once the listeners are removed (the graph went away)", async () => {
    const p = await page();
    p.stop();
    window.addEventListener("keydown", p.outside);
    stop = () => window.removeEventListener("keydown", p.outside);
    p.press();
    expect([p.state.shown, p.control.mode()]).toEqual(["s1", "overlay"]);
    expect(p.outside).toHaveBeenCalledOnce();
  });
});
