import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import {
  CARD_GAP,
  CLOSE_DELAY_MS,
  OPEN_DELAY_MS,
  type PointerAt,
  TAP_MS,
  TAP_SLOP_PX,
  cardKey,
  cardRoom,
  fullHeight,
  hoverCard,
  hovers,
  intersect,
  placeCard,
  tapOn,
} from "@/lib/hover-card";

describe("hoverCard", () => {
  let changes: (string | null)[];
  let card: ReturnType<typeof hoverCard>;

  beforeEach(() => {
    vi.useFakeTimers();
    changes = [];
    card = hoverCard((id) => changes.push(id));
  });
  afterEach(() => {
    card.dispose();
    vi.useRealTimers();
  });

  it("shows a node's card once the mouse rests on it, not before", () => {
    card.enter("s1");
    vi.advanceTimersByTime(OPEN_DELAY_MS - 1);
    expect(card.shown()).toBeNull();
    vi.advanceTimersByTime(1);
    expect(card.shown()).toBe("s1");
    expect(changes).toEqual(["s1"]);
  });

  it("keeps one delay while the mouse crosses the node's children", () => {
    card.enter("s1");
    vi.advanceTimersByTime(OPEN_DELAY_MS - 50);
    card.enter("s1");
    vi.advanceTimersByTime(50);
    expect(card.shown()).toBe("s1");
  });

  it("shows nothing for a node the mouse only passed over", () => {
    card.enter("s1");
    vi.advanceTimersByTime(OPEN_DELAY_MS / 2);
    card.leave();
    vi.advanceTimersByTime(OPEN_DELAY_MS * 2);
    expect(changes).toEqual([]);
  });

  it("hides the card shortly after the mouse leaves its node", () => {
    card.enter("s1");
    vi.advanceTimersByTime(OPEN_DELAY_MS);
    card.leave();
    vi.advanceTimersByTime(CLOSE_DELAY_MS - 1);
    expect(card.shown()).toBe("s1");
    card.leave(); // moving over the pane does not put the close off
    vi.advanceTimersByTime(1);
    expect(card.shown()).toBeNull();
    expect(changes).toEqual(["s1", null]);
  });

  it("stays while the mouse moves from the node into the card, and hides when it leaves the card", () => {
    card.enter("s1");
    vi.advanceTimersByTime(OPEN_DELAY_MS);
    card.leave();
    vi.advanceTimersByTime(CLOSE_DELAY_MS - 10);
    card.enterCard();
    vi.advanceTimersByTime(CLOSE_DELAY_MS * 5);
    expect(card.shown()).toBe("s1");
    card.leave();
    vi.advanceTimersByTime(CLOSE_DELAY_MS);
    expect(card.shown()).toBeNull();
  });

  it("keeps the card when the mouse comes back to its node", () => {
    card.enter("s1");
    vi.advanceTimersByTime(OPEN_DELAY_MS);
    card.leave();
    card.enter("s1");
    vi.advanceTimersByTime(CLOSE_DELAY_MS * 5);
    expect(card.shown()).toBe("s1");
  });

  it("moves to the next node after its delay, keeping the first card until then", () => {
    card.enter("s1");
    vi.advanceTimersByTime(OPEN_DELAY_MS);
    card.leave();
    card.enter("s2");
    vi.advanceTimersByTime(OPEN_DELAY_MS - 1);
    expect(card.shown()).toBe("s1");
    vi.advanceTimersByTime(1);
    expect(card.shown()).toBe("s2");
    expect(changes).toEqual(["s1", "s2"]);
  });

  it("toggles a node's card on a tap, and moves it to another tapped node", () => {
    card.tap("s1");
    expect(card.shown()).toBe("s1");
    card.tap("s1");
    expect(card.shown()).toBeNull();
    card.tap("s1");
    card.tap("s2");
    expect(card.shown()).toBe("s2");
    expect(changes).toEqual(["s1", null, "s1", "s2"]);
  });

  it("keeps a tapped card until the next tap or a tap elsewhere", () => {
    card.tap("s1");
    card.leave();
    vi.advanceTimersByTime(CLOSE_DELAY_MS * 5);
    expect(card.shown()).toBe("s1");
    card.dismiss();
    expect(card.shown()).toBeNull();
  });

  it("shows a focused node's card at once and hides it when focus leaves", () => {
    card.focus("s1");
    expect(card.shown()).toBe("s1");
    card.leave(); // the mouse leaving does not hide a focused node's card
    vi.advanceTimersByTime(CLOSE_DELAY_MS * 5);
    expect(card.shown()).toBe("s1");
    card.focus("s2");
    expect(card.shown()).toBe("s2");
    card.blur();
    expect(card.shown()).toBeNull();
    expect(changes).toEqual(["s1", "s2", null]);
  });

  it("closes on Escape (dismiss) and cancels a pending open", () => {
    card.focus("s1");
    card.dismiss();
    expect(card.shown()).toBeNull();
    card.enter("s2");
    card.dismiss();
    vi.advanceTimersByTime(OPEN_DELAY_MS * 2);
    expect(card.shown()).toBeNull();
  });

  it("opens nothing once disposed (the graph went away)", () => {
    card.enter("s1");
    card.dispose();
    vi.advanceTimersByTime(OPEN_DELAY_MS * 2);
    expect(changes).toEqual([]);
  });

  it("does not let a blur hide a card the mouse opened", () => {
    card.enter("s1");
    vi.advanceTimersByTime(OPEN_DELAY_MS);
    card.blur();
    expect(card.shown()).toBe("s1");
  });
});

describe("placeCard", () => {
  const area = { left: 0, top: 0, width: 1000, height: 600 };
  const size = { width: 320, height: 200 };
  const node = (left: number, top: number) => ({ left, top, width: 180, height: 60 });

  it("puts the card on the node's right when it fits", () => {
    expect(placeCard(node(100, 100), size, area)).toMatchObject({ side: "right", left: 100 + 180 + CARD_GAP, top: 100 });
  });

  it("flips it to the left near the right edge", () => {
    expect(placeCard(node(700, 100), size, area)).toMatchObject({ side: "left", left: 700 - CARD_GAP - 320, top: 100 });
  });

  it("keeps it inside the area at the bottom", () => {
    expect(placeCard(node(100, 560), size, area).top).toBe(600 - CARD_GAP - 200);
  });

  it("puts it under the node, inside the area, when neither side has the room", () => {
    const narrow = { left: 0, top: 0, width: 390, height: 600 };
    expect(placeCard(node(200, 100), size, narrow)).toMatchObject({ side: "below", left: 390 - CARD_GAP - 320, top: 168 });
  });

  it("puts it over the node when there is more room there", () => {
    const narrow = { left: 0, top: 0, width: 390, height: 600 };
    expect(placeCard(node(100, 450), size, narrow)).toMatchObject({ side: "above", top: 450 - CARD_GAP - 200 });
  });

  it("shortens it to the roomier side over or under the node, so the node stays in sight", () => {
    const phone = { left: 0, top: 0, width: 390, height: 450 };
    const tall = { width: 320, height: 380 };
    expect(placeCard({ left: 130, top: 200, width: 125, height: 42 }, tall, phone))
      .toMatchObject({ side: "below", top: 250, height: 450 - CARD_GAP - 250 });
    expect(placeCard({ left: 130, top: 260, width: 125, height: 42 }, tall, phone))
      .toMatchObject({ side: "above", top: CARD_GAP, height: 260 - 2 * CARD_GAP });
  });

  it("covers the node only when neither side has the least room", () => {
    const short = { left: 0, top: 0, width: 390, height: 200 };
    expect(placeCard({ left: 130, top: 80, width: 125, height: 42 }, { width: 320, height: 180 }, short))
      .toMatchObject({ side: "below", top: 200 - CARD_GAP - 180, height: 180 });
  });

  it("respects an area that does not start at the frame's corner", () => {
    const clipped = { left: 0, top: 300, width: 1000, height: 300 };
    expect(placeCard(node(100, 250), size, clipped).top).toBe(300 + CARD_GAP);
  });
});

describe("intersect", () => {
  it("is the overlap, or empty", () => {
    expect(intersect({ left: 0, top: 0, width: 100, height: 100 }, { left: 50, top: 20, width: 100, height: 50 }))
      .toEqual({ left: 50, top: 20, width: 50, height: 50 });
    expect(intersect({ left: 0, top: 0, width: 10, height: 10 }, { left: 50, top: 50, width: 10, height: 10 }).width).toBe(0);
  });
});

describe("tapOn", () => {
  const press = (extra: Partial<PointerAt> = {}): PointerAt =>
    ({ node: "s1", onButton: false, x: 100, y: 100, at: 1000, pointerType: "touch", ...extra });

  it("is a tap on the node a touch or a pen went down and up on, soon and without moving", () => {
    expect(tapOn(press(), press({ x: 100 + TAP_SLOP_PX - 1, at: 1000 + TAP_MS - 1 }))).toBe("s1");
    expect(tapOn(press({ pointerType: "pen" }), press({ pointerType: "pen" }))).toBe("s1");
  });

  it("is no tap when the pointer moved, stayed down, left the node, or hovers", () => {
    expect(tapOn(press(), press({ x: 100 + TAP_SLOP_PX }))).toBeNull(); // a drag: it pans
    expect(tapOn(press(), press({ y: 100 - TAP_SLOP_PX }))).toBeNull();
    expect(tapOn(press(), press({ at: 1000 + TAP_MS }))).toBeNull(); // a long press
    expect(tapOn(press(), press({ node: "s2" }))).toBeNull();
    expect(tapOn(press({ node: null }), press({ node: null }))).toBeNull(); // on the pane
    expect(tapOn(press({ pointerType: "mouse" }), press({ pointerType: "mouse" }))).toBeNull(); // the mouse hovers
    expect(tapOn(null, press())).toBeNull();
  });

  it("leaves a tap on a node's own button (a gate's) to the button", () => {
    expect(tapOn(press({ onButton: true }), press({ onButton: true }))).toBeNull();
  });

  it("knows which pointers hover", () => {
    expect([hovers("mouse"), hovers("touch"), hovers("pen")]).toEqual([true, false, false]);
  });
});

describe("cardKey", () => {
  const state = (extra: Partial<Parameters<typeof cardKey>[1]> = {}) =>
    ({ shown: "s1", focusInCard: false, focusedNode: null, ...extra });

  it("closes the shown card on Esc wherever focus is, focus going back to the node from the card", () => {
    expect(cardKey("Escape", state())).toBe("close");
    expect(cardKey("Escape", state({ focusInCard: true }))).toBe("close-to-node");
    expect(cardKey("Escape", state({ shown: null }))).toBeNull(); // nothing to close: the key goes on
  });

  it("takes focus into the card on Enter or Space on a focused node", () => {
    expect(cardKey("Enter", state({ focusedNode: "s1" }))).toBe("into-card");
    expect(cardKey(" ", state({ shown: null, focusedNode: "s2" }))).toBe("into-card");
    expect(cardKey("Enter", state({ focusedNode: null }))).toBeNull(); // on a node's button, or elsewhere
    expect(cardKey("Enter", state({ focusedNode: "s1", focusInCard: true }))).toBeNull(); // the card's own controls
    expect(cardKey("a", state({ focusedNode: "s1" }))).toBeNull();
  });
});

describe("cardRoom", () => {
  const box = (left: number, top: number, width: number, height: number) => ({ left, top, width, height });
  const VIEW = box(0, 0, 390, 844);
  const frame = box(0, 300, 390, 450); // the graph, under the task header

  it("is the graph in the window, in the graph's frame", () => {
    expect(cardRoom(frame, VIEW, [], [])).toEqual(box(0, 0, 390, 450));
    expect(cardRoom(box(0, 600, 390, 450), VIEW, [], [])).toEqual(box(0, 0, 390, 244)); // below the fold
  });

  it("is cut by an ancestor that clips: the page scrolled the graph under its header", () => {
    const scroller = { clips: true, fixed: false, box: box(0, 257, 390, 343) };
    const scrolled = box(0, 77, 390, 450); // 180 px up
    expect(cardRoom(scrolled, VIEW, [scroller], [])).toEqual(box(0, 180, 390, 270));
    expect(cardRoom(scrolled, VIEW, [{ ...scroller, clips: false }], [])).toEqual(box(0, 0, 390, 450));
  });

  it("stops at a fixed ancestor: the ones above a full-screen overlay do not clip it", () => {
    const overlay = { clips: false, fixed: true, box: VIEW };
    const scroller = { clips: true, fixed: false, box: box(0, 257, 390, 343) };
    expect(cardRoom(VIEW, VIEW, [overlay, scroller], [])).toEqual(box(0, 0, 390, 844));
  });

  it("keeps clear of the graph's panels: above the toolbar, under full screen's way out", () => {
    const toolbar = box(67, 371, 308, 64); // its bottom right, in the lower half
    const exit = box(244, 15, 131, 52); // its top right
    expect(cardRoom(frame, VIEW, [], [box(67, 671, 308, 64)])).toEqual(box(0, 0, 390, 371));
    expect(cardRoom(box(0, 0, 390, 844), VIEW, [], [exit, { ...toolbar, top: 765 }])).toEqual(box(0, 67, 390, 698));
  });
});

describe("fullHeight", () => {
  it("is what the card scrolls, with its borders, whatever its max-height cuts", () => {
    expect(fullHeight({ scrollHeight: 660, offsetHeight: 204, clientHeight: 202 })).toBe(662);
    expect(fullHeight({ scrollHeight: 300, offsetHeight: 302, clientHeight: 300 })).toBe(302);
  });
});
