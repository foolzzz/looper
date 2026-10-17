import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { CARD_GAP, CLOSE_DELAY_MS, OPEN_DELAY_MS, hoverCard, intersect, placeCard } from "@/lib/hover-card";

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
