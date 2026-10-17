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

/** Whether a pointer hovers (a mouse) and opens cards by resting; any other (touch, a pen) taps them. */
export const hovers = (pointerType: string) => pointerType === "mouse";

/** The longest press and the farthest move that are still a tap; more is a drag that pans. */
export const TAP_MS = 500;
export const TAP_SLOP_PX = 10;

/** Where a pointer went down or up: on which node (null: elsewhere), and whether on that node's own button. */
export interface PointerAt {
  node: string | null;
  onButton: boolean;
  x: number;
  y: number;
  at: number;
  pointerType: string;
}

/** The node a press and its release tap, or null: a tap is a pointer that does not hover going down and up on one
 * node, soon and without moving. Taken from the pointer, not from click: the browser holds back the click of a
 * quick second tap. A tap on a node's own button (a gate's) is the button's. */
export function tapOn(down: PointerAt | null, up: PointerAt): string | null {
  if (!down?.node || down.onButton || hovers(down.pointerType) || up.pointerType !== down.pointerType) return null;
  const still = Math.hypot(up.x - down.x, up.y - down.y) < TAP_SLOP_PX && up.at - down.at < TAP_MS;
  return up.node === down.node && still ? down.node : null;
}

/** What a key does to the cards: Esc closes the one shown, focus going back to its node when it was in the card;
 * Enter or Space on a focused node takes focus into its card. */
export type CardKey = "close" | "close-to-node" | "into-card" | null;

export function cardKey(
  key: string,
  { shown, focusInCard, focusedNode }: { shown: string | null; focusInCard: boolean; focusedNode: string | null },
): CardKey {
  if (key === "Escape") return shown === null ? null : focusInCard ? "close-to-node" : "close";
  if ((key === "Enter" || key === " ") && focusedNode && !focusInCard) return "into-card";
  return null;
}

/** An ancestor of the graph, as far as a card's room goes: whether it clips what overflows it, whether it is fixed
 * (the ones above a fixed one do not clip it), and its box. */
export interface Clipper {
  clips: boolean;
  fixed: boolean;
  box: Box;
}

/** The room a card has, in the graph's frame; every box is in the window's coordinates. It is the graph in sight,
 * inside the window and each ancestor that clips it (up to a fixed one, such as a full-screen overlay), clear of
 * the graph's own panels that ask for it: above one in the lower half, under one in the upper half. */
export function cardRoom(frame: Box, view: Box, ancestors: Clipper[], avoid: Box[]): Box {
  let sight = view;
  for (const ancestor of ancestors) {
    if (ancestor.clips) sight = intersect(sight, ancestor.box);
    if (ancestor.fixed) break;
  }
  const seen = intersect(frame, sight);
  let room: Box = { left: seen.left - frame.left, top: seen.top - frame.top, width: seen.width, height: seen.height };
  for (const panel of avoid) {
    const top = panel.top - frame.top;
    const bottom = room.top + room.height;
    if (top > room.top + room.height / 2) room = { ...room, height: Math.max(0, Math.min(bottom, top) - room.top) };
    else {
      const under = Math.max(room.top, top + panel.height);
      room = { ...room, top: under, height: Math.max(0, bottom - under) };
    }
  }
  return room;
}

/** A card's whole height, measured without changing its max-height (which would reset its scroll): what it scrolls,
 * with its borders. */
export const fullHeight = (card: { scrollHeight: number; offsetHeight: number; clientHeight: number }) =>
  card.scrollHeight + card.offsetHeight - card.clientHeight;

/** Listens for the cards' keys on `doc` before anything else hears them (the capture phase): a key `route` gives an
 * action is the cards' alone, `act` does it, and it goes no further (not to full screen, which listens after). */
export function listenCardKeys(
  doc: Pick<Document, "addEventListener" | "removeEventListener">,
  route: (event: KeyboardEvent) => CardKey,
  act: (action: NonNullable<CardKey>, event: KeyboardEvent) => void,
): () => void {
  const listener = (event: KeyboardEvent) => {
    const action = route(event);
    if (!action) return;
    event.preventDefault();
    event.stopPropagation();
    act(action, event);
  };
  doc.addEventListener("keydown", listener, true);
  return () => doc.removeEventListener("keydown", listener, true);
}
