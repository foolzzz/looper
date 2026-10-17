/** When a node's details card shows and hides, apart from the DOM: the graph's events call it, it calls `onChange`
 * with the node whose card shows (null: none). */

/** How long the pointer rests on a node before its card shows. */
export const OPEN_DELAY_MS = 200;
/** How long a card stays after the pointer left its node or itself, so the pointer can move between the two. */
export const CLOSE_DELAY_MS = 150;

/** What opened the card: a resting mouse (it closes when the pointer leaves), keyboard focus (when focus leaves) or
 * a tap (on the next tap). */
export type CardTrigger = "hover" | "focus" | "tap";

export interface HoverCard {
  /** The mouse rests on node `id`. */
  enter(id: string): void;
  /** The mouse is on the card. */
  enterCard(): void;
  /** The mouse left the node or the card, for neither. */
  leave(): void;
  /** Node `id` has keyboard focus. */
  focus(id: string): void;
  /** Focus left the node, for neither it nor the card. */
  blur(): void;
  /** A tap (no hover) on node `id`: its card shows, or hides when it shows. */
  tap(id: string): void;
  /** A tap or click elsewhere, or Escape. */
  dismiss(): void;
  shown(): string | null;
  dispose(): void;
}

export function hoverCard(onChange: (id: string | null) => void): HoverCard {
  let shown: string | null = null;
  let trigger: CardTrigger | null = null;
  // The node whose card shows when `opening` fires; the pointer crosses a node's children with one timer.
  let pending: string | null = null;
  let opening: ReturnType<typeof setTimeout> | undefined;
  let closing: ReturnType<typeof setTimeout> | undefined;

  const cancelOpen = () => {
    clearTimeout(opening);
    opening = undefined;
    pending = null;
  };
  const cancelClose = () => {
    clearTimeout(closing);
    closing = undefined;
  };
  const stop = () => {
    cancelOpen();
    cancelClose();
  };
  const show = (id: string | null, by: CardTrigger | null) => {
    stop();
    trigger = by;
    if (id === shown) return;
    shown = id;
    onChange(id);
  };

  return {
    enter(id) {
      cancelClose();
      if (id === shown) {
        cancelOpen();
        return;
      }
      if (id === pending) return;
      cancelOpen();
      pending = id;
      opening = setTimeout(() => show(id, "hover"), OPEN_DELAY_MS);
    },
    enterCard() {
      stop();
    },
    leave() {
      cancelOpen();
      if (shown !== null && trigger === "hover" && closing === undefined) {
        closing = setTimeout(() => show(null, null), CLOSE_DELAY_MS);
      }
    },
    focus(id) {
      show(id, "focus");
    },
    blur() {
      if (trigger === "focus") show(null, null);
    },
    tap(id) {
      show(id === shown ? null : id, id === shown ? null : "tap");
    },
    dismiss() {
      show(null, null);
    },
    shown: () => shown,
    dispose: stop,
  };
}

export interface Box {
  left: number;
  top: number;
  width: number;
  height: number;
}

export type CardSide = "right" | "left" | "below" | "above";

/** The gap between a node and its card, and the card's least distance from the area's edges. */
export const CARD_GAP = 8;

const clamp = (value: number, low: number, high: number) => Math.max(low, Math.min(value, Math.max(low, high)));

/** The least height a card keeps when it is shortened to fit over or under its node; with less room it covers it. */
export const MIN_CARD_HEIGHT = 120;

/** Where a card of `card`'s size goes beside `node`, inside `area` (all in one frame): on the node's right, else
 * its left, moved along that side to stay inside the area; else under or over the node, on the side with the room,
 * else the roomier side with the card shortened to fit (it scrolls), so the node stays in sight to be tapped again.
 * `height` is the card's height there. */
export function placeCard(node: Box, card: { width: number; height: number }, area: Box): Box & { side: CardSide } {
  const right = area.left + area.width - CARD_GAP;
  const bottom = area.top + area.height - CARD_GAP;
  const alongY = clamp(node.top, area.top + CARD_GAP, bottom - card.height);
  const alongX = clamp(node.left + node.width / 2 - card.width / 2, area.left + CARD_GAP, right - card.width);
  const place = (side: CardSide, left: number, top: number, height = card.height) =>
    ({ side, left, top, width: card.width, height });
  if (node.left + node.width + CARD_GAP + card.width <= right) {
    return place("right", node.left + node.width + CARD_GAP, alongY);
  }
  if (node.left - CARD_GAP - card.width >= area.left + CARD_GAP) {
    return place("left", node.left - CARD_GAP - card.width, alongY);
  }
  const below = node.top + node.height + CARD_GAP;
  const roomBelow = bottom - below;
  const roomAbove = node.top - CARD_GAP - (area.top + CARD_GAP);
  if (card.height <= roomBelow || (card.height > roomAbove && roomBelow >= roomAbove && roomBelow >= MIN_CARD_HEIGHT)) {
    return place("below", alongX, below, Math.min(card.height, roomBelow));
  }
  if (card.height <= roomAbove || roomAbove >= MIN_CARD_HEIGHT) {
    const height = Math.min(card.height, roomAbove);
    return place("above", alongX, node.top - CARD_GAP - height, height);
  }
  // No side has the room: the card covers the node, inside the area.
  return place("below", alongX, clamp(below, area.top + CARD_GAP, bottom - card.height));
}

/** The part of `box` inside `clip`; empty (zero width or height) when they do not meet. */
export function intersect(box: Box, clip: Box): Box {
  const left = Math.max(box.left, clip.left);
  const top = Math.max(box.top, clip.top);
  const width = Math.max(0, Math.min(box.left + box.width, clip.left + clip.width) - left);
  const height = Math.max(0, Math.min(box.top + box.height, clip.top + clip.height) - top);
  return { left, top, width, height };
}
