import { memo, useCallback, useEffect, useLayoutEffect, useRef, useState } from "react";
import { useStore } from "@xyflow/react";
import {
  type Box,
  CARD_GAP,
  type HoverCard,
  type PointerAt,
  cardKey,
  hoverCard,
  hovers,
  intersect,
  placeCard,
  tapOn,
} from "@/lib/hover-card";
import { Markdown } from "@/components/markdown";
import { type CardLook, type CardSection, type NodeCard, nodeCardId } from "@/lib/node-cards";
import { cn } from "@/lib/utils";

function StatusPill({ look }: { look: CardLook }) {
  return (
    <span
      className="inline-flex items-center rounded-md px-1.5 py-0.5 text-[11px] font-medium"
      style={{ background: look.background, color: look.color, border: `1px ${look.borderStyle} ${look.border}` }}
    >
      {look.label}
    </span>
  );
}

function CardText({ section }: { section: Extract<CardSection, { kind: "text" }> }) {
  const [expanded, setExpanded] = useState(false);
  const shown = section.preview && !expanded ? section.preview : section.text;
  return (
    <>
      {section.markdown ? (
        // The card's size for the text and its headings, under the typography styles.
        <div className="[&_.prose]:text-xs [&_h1]:text-xs [&_h2]:mb-0.5 [&_h2]:mt-2 [&_h2]:text-xs [&_h3]:text-xs">
          <Markdown>{shown}</Markdown>
        </div>
      ) : (
        <p className="whitespace-pre-wrap break-words text-xs">{shown}</p>
      )}
      {section.preview && (
        <button
          type="button"
          onClick={() => setExpanded(!expanded)}
          aria-expanded={expanded}
          className="mt-1 text-xs font-medium text-muted-foreground underline-offset-2 hover:text-foreground hover:underline pointer-coarse:min-h-11"
        >
          {expanded ? "Show less" : "Show more"}
        </button>
      )}
    </>
  );
}

function CardBody({ section }: { section: CardSection }) {
  switch (section.kind) {
    case "text":
      return <CardText section={section} />;
    case "list":
      return (
        <ul className={cn("list-disc space-y-0.5 pl-4 text-xs", section.mono && "font-mono")}>
          {section.items.map((item, i) => (
            <li key={i} className="break-words">{item}</li>
          ))}
        </ul>
      );
    case "steps":
      return (
        <ul className="flex flex-wrap gap-1.5">
          {section.steps.map((step) => (
            <li key={step.name} className="flex items-center gap-1 text-xs">
              <span className="font-mono">{step.name}</span>
              <StatusPill look={step.look} />
            </li>
          ))}
        </ul>
      );
    case "facts":
      return (
        <dl className="grid grid-cols-[auto_minmax(0,1fr)] gap-x-3 gap-y-0.5 text-xs pointer-coarse:items-center">
          {section.facts.map((fact) => (
            <div key={fact.label} className="contents">
              <dt className="text-muted-foreground">{fact.label}</dt>
              <dd className="min-w-0 break-words">
                {fact.href ? (
                  <a
                    href={fact.href}
                    className="font-mono underline decoration-dotted underline-offset-2 hover:text-primary pointer-coarse:inline-flex pointer-coarse:min-h-11 pointer-coarse:items-center"
                  >
                    {fact.value}
                  </a>
                ) : (
                  fact.value
                )}
              </dd>
            </div>
          ))}
        </dl>
      );
  }
}

/** A card's content; it renders again only for a new card, not as the graph pans and zooms. */
export const NodeCardView = memo(function NodeCardView({ card }: { card: NodeCard }) {
  return (
    <div className="space-y-3">
      <header>
        <div className="flex flex-wrap items-center gap-2">
          <span className="font-mono text-sm font-semibold">{card.name}</span>
          <StatusPill look={card.look} />
        </div>
        {card.roles.map((role) => (
          <p key={role} className="mt-1 font-mono text-xs text-muted-foreground">{role}</p>
        ))}
      </header>
      {/* By place: a title can change (a note's time) and a section keeps its Show more. */}
      {card.sections.map((section, i) => (
        <section key={`${i}:${section.kind}`}>
          <h4 className={cn("mb-1 text-[11px] font-medium text-muted-foreground",
                            section.kind === "text" && section.tone === "destructive" && "text-destructive")}>
            {section.title}
          </h4>
          <CardBody section={section} />
        </section>
      ))}
    </div>
  );
});

/** The cards of a React Flow graph's nodes (a child of `ReactFlow`), by node id. A card shows beside its node when
 * the mouse rests on the node or the node has keyboard focus, and a tap toggles it; Enter takes focus into it and
 * Esc closes it. It stays inside the graph's visible part and follows the node as the graph pans and zooms. */
export function NodeCards({ cards }: { cards: Record<string, NodeCard> }) {
  const root = useStore((s) => s.domNode);
  const transform = useStore((s) => s.transform);
  const [shown, setShown] = useState<string | null>(null);
  const [control] = useState<HoverCard>(() => hoverCard(setShown));
  const cardRef = useRef<HTMLDivElement>(null);
  // Enter on a node opened its card: focus goes into the card once it has a place.
  const focusCard = useRef(false);
  const card = shown ? cards[shown] : undefined;

  useEffect(() => () => control.dispose(), [control]);

  useEffect(() => {
    if (!root) return;
    let press: PointerAt | null = null;
    const inCard = (target: EventTarget | null) => target instanceof Node && !!cardRef.current?.contains(target);
    const nodeElement = (target: EventTarget | null) =>
      target instanceof Element && root.contains(target) ? target.closest<HTMLElement>(".react-flow__node") : null;
    const nodeOf = (target: EventTarget | null) => nodeElement(target)?.dataset.id ?? null;
    const at = (event: PointerEvent): PointerAt => ({
      node: nodeOf(event.target), x: event.clientX, y: event.clientY, at: event.timeStamp, pointerType: event.pointerType,
      onButton: event.target instanceof Element && !!event.target.closest(".react-flow__node button"),
    });
    const over = (event: PointerEvent) => {
      if (!hovers(event.pointerType)) return;
      if (inCard(event.target)) return control.enterCard();
      const id = nodeOf(event.target);
      if (id) control.enter(id);
      else control.leave();
    };
    const out = (event: PointerEvent) => {
      if (hovers(event.pointerType) && !root.contains(event.relatedTarget as Node | null)) control.leave();
    };
    const down = (event: PointerEvent) => {
      press = at(event);
      if (!inCard(event.target) && !press.node) control.dismiss();
    };
    const up = (event: PointerEvent) => {
      const id = tapOn(press, at(event));
      press = null;
      if (id) control.tap(id);
    };
    const focusIn = (event: FocusEvent) => {
      const id = nodeOf(event.target);
      if (id && !inCard(event.target) && (event.target as Element).matches(":focus-visible")) control.focus(id);
    };
    const focusOut = (event: FocusEvent) => {
      const next = nodeOf(event.relatedTarget);
      if (inCard(event.relatedTarget) || (next && next === nodeOf(event.target))) return;
      control.blur();
    };
    // On the document, first: Esc closes a card wherever focus is, and goes no further (not to full screen).
    const key = (event: KeyboardEvent) => {
      const focused = nodeElement(event.target);
      const action = cardKey(event.key, {
        shown: control.shown(),
        focusInCard: inCard(document.activeElement),
        focusedNode: focused && focused === event.target ? (focused.dataset.id ?? null) : null,
      });
      if (!action) return;
      event.preventDefault();
      event.stopPropagation();
      if (action === "into-card") {
        if (cardRef.current && control.shown() === focused?.dataset.id) cardRef.current.focus();
        else {
          focusCard.current = true;
          control.focus(focused!.dataset.id!);
        }
        return;
      }
      const id = control.shown();
      if (action === "close-to-node" && id) root.querySelector<HTMLElement>(`.react-flow__node[data-id="${CSS.escape(id)}"]`)?.focus();
      control.dismiss();
    };
    root.addEventListener("pointerover", over);
    root.addEventListener("pointerout", out);
    root.addEventListener("pointerup", up);
    root.addEventListener("focusin", focusIn);
    root.addEventListener("focusout", focusOut);
    document.addEventListener("keydown", key, true);
    document.addEventListener("pointerdown", down, true);
    return () => {
      root.removeEventListener("pointerover", over);
      root.removeEventListener("pointerout", out);
      root.removeEventListener("pointerup", up);
      root.removeEventListener("focusin", focusIn);
      root.removeEventListener("focusout", focusOut);
      document.removeEventListener("keydown", key, true);
      document.removeEventListener("pointerdown", down, true);
    };
  }, [root, control]);

  // Beside its node, inside the part of the graph in sight: the window, and every ancestor that clips (up to a
  // fixed one, such as the full-screen overlay); hidden while the node is out of sight. Set on the element, so
  // following a pan or a scroll does not render the card again, and measured without changing its height, so a
  // card scrolled inside keeps its place.
  const place = useCallback(() => {
    const element = cardRef.current;
    const node = shown && root?.querySelector(`.react-flow__node[data-id="${CSS.escape(shown)}"]`);
    if (!root || !element || !node) return;
    let sight: Box = { left: 0, top: 0, width: window.innerWidth, height: window.innerHeight };
    for (let ancestor = root.parentElement; ancestor; ancestor = ancestor.parentElement) {
      const style = getComputedStyle(ancestor);
      if (style.overflowX !== "visible" || style.overflowY !== "visible") sight = intersect(sight, ancestor.getBoundingClientRect());
      if (style.position === "fixed") break;
    }
    const frame = root.getBoundingClientRect();
    const local = (r: Box): Box => ({ left: r.left - frame.left, top: r.top - frame.top, width: r.width, height: r.height });
    let area = local(intersect(frame, sight));
    // Clear of the graph's own controls that ask for it (data-card-avoid): above one in its lower half, under one
    // in its upper half.
    for (const control of root.querySelectorAll("[data-card-avoid]")) {
      const r = local(control.getBoundingClientRect());
      const bottom = area.top + area.height;
      if (r.top > area.top + area.height / 2) area = { ...area, height: Math.max(0, Math.min(bottom, r.top) - area.top) };
      else area = { ...area, top: Math.max(area.top, r.top + r.height), height: Math.max(0, bottom - Math.max(area.top, r.top + r.height)) };
    }
    const at = local(node.getBoundingClientRect());
    const seen = intersect(at, area);
    if (!seen.width || !seen.height) {
      element.style.visibility = "hidden";
      return;
    }
    // Its whole height: what it scrolls, with its borders.
    const natural = element.scrollHeight + element.offsetHeight - element.clientHeight;
    const room = Math.max(0, area.height - 2 * CARD_GAP);
    const { left, top, height } = placeCard(at, { width: element.offsetWidth, height: Math.min(natural, room) }, area);
    Object.assign(element.style, { left: `${left}px`, top: `${top}px`, visibility: "visible", opacity: "1" });
    if (element.style.maxHeight !== `${height}px`) element.style.maxHeight = `${height}px`;
    if (focusCard.current) {
      focusCard.current = false;
      element.focus();
    }
  }, [root, shown]);

  useLayoutEffect(place, [place, card, transform]);

  // Placed again when the page scrolls (not the card itself) or resizes, or the card's content changes its size.
  useEffect(() => {
    const element = cardRef.current;
    if (!card || !element) return;
    const observer = new ResizeObserver(place);
    observer.observe(element);
    const scrolled = (event: Event) => {
      if (!(event.target instanceof Node && element.contains(event.target))) place();
    };
    window.addEventListener("scroll", scrolled, { capture: true, passive: true });
    window.addEventListener("resize", place);
    return () => {
      observer.disconnect();
      window.removeEventListener("scroll", scrolled, { capture: true });
      window.removeEventListener("resize", place);
    };
  }, [card, place]);

  if (!card) return null;
  return (
    <div
      key={shown}
      ref={cardRef}
      id={nodeCardId(shown!)}
      role="dialog"
      aria-label={`${card.name}: details`}
      tabIndex={-1}
      className={cn(
        "nowheel nopan nodrag absolute z-10 w-80 max-w-[calc(100%-16px)] overflow-y-auto rounded-lg border border-border bg-popover p-3 text-popover-foreground shadow-md focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring",
        // Shown by `place` once it has a place.
        "invisible left-0 top-0 opacity-0 motion-safe:transition-opacity motion-safe:duration-150",
      )}
    >
      <NodeCardView card={card} />
    </div>
  );
}
