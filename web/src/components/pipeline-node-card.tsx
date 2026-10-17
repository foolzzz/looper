import { useCallback, useEffect, useLayoutEffect, useRef, useState } from "react";
import { useStore } from "@xyflow/react";
import { type Box, CARD_GAP, type HoverCard, hoverCard, intersect, placeCard } from "@/lib/hover-card";
import { Markdown } from "@/components/markdown";
import type { CardLook, CardSection, NodeCard } from "@/lib/node-cards";
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
          className="mt-1 text-xs font-medium text-muted-foreground underline-offset-2 hover:text-foreground hover:underline"
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
        <dl className="grid grid-cols-[auto_minmax(0,1fr)] gap-x-3 gap-y-0.5 text-xs">
          {section.facts.map((fact) => (
            <div key={fact.label} className="contents">
              <dt className="text-muted-foreground">{fact.label}</dt>
              <dd className="min-w-0 break-words">
                {fact.href ? (
                  <a
                    href={fact.href}
                    className="font-mono underline decoration-dotted underline-offset-2 hover:text-primary"
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

export function NodeCardView({ card }: { card: NodeCard }) {
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
      {card.sections.map((section) => (
        <section key={section.title}>
          <h4 className={cn("mb-1 text-[11px] font-medium text-muted-foreground",
                            section.kind === "text" && section.tone === "destructive" && "text-destructive")}>
            {section.title}
          </h4>
          <CardBody section={section} />
        </section>
      ))}
    </div>
  );
}

// The longest press and the farthest move that are still a tap.
const TAP_MS = 500;
const TAP_SLOP_PX = 10;

/** The cards of a React Flow graph's nodes (a child of `ReactFlow`), by node id. A card shows beside its node when
 * the mouse rests on the node or the node has keyboard focus, and a tap toggles it; it stays inside the graph's
 * visible part and follows the node as the graph pans and zooms. */
export function NodeCards({ cards }: { cards: Record<string, NodeCard> }) {
  const root = useStore((s) => s.domNode);
  const transform = useStore((s) => s.transform);
  const [shown, setShown] = useState<string | null>(null);
  const [control] = useState<HoverCard>(() => hoverCard(setShown));
  const cardRef = useRef<HTMLDivElement>(null);
  const card = shown ? cards[shown] : undefined;

  useEffect(() => () => control.dispose(), [control]);

  useEffect(() => {
    if (!root) return;
    // A touch that went down on a node; it is a tap when it comes up there, soon and without moving (a drag pans).
    // Taken from the pointer, not from click: the browser holds back the click of a quick second tap.
    let touch: { id: string; x: number; y: number; at: number } | null = null;
    const inCard = (target: EventTarget | null) => target instanceof Node && !!cardRef.current?.contains(target);
    const nodeOf = (target: EventTarget | null) =>
      target instanceof Element && root.contains(target)
        ? (target.closest<HTMLElement>(".react-flow__node")?.dataset.id ?? null)
        : null;
    const over = (event: PointerEvent) => {
      if (event.pointerType === "touch") return;
      if (inCard(event.target)) return control.enterCard();
      const id = nodeOf(event.target);
      if (id) control.enter(id);
      else control.leave();
    };
    const out = (event: PointerEvent) => {
      if (event.pointerType !== "touch" && !root.contains(event.relatedTarget as Node | null)) control.leave();
    };
    const down = (event: PointerEvent) => {
      const id = nodeOf(event.target);
      touch = event.pointerType === "touch" && id ? { id, x: event.clientX, y: event.clientY, at: event.timeStamp } : null;
      if (!inCard(event.target) && !id) control.dismiss();
    };
    const up = (event: PointerEvent) => {
      const start = touch;
      touch = null;
      if (start && event.pointerType === "touch" && nodeOf(event.target) === start.id && event.timeStamp - start.at < TAP_MS &&
          Math.hypot(event.clientX - start.x, event.clientY - start.y) < TAP_SLOP_PX) {
        control.tap(start.id);
      }
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
    const key = (event: KeyboardEvent) => {
      if (event.key === "Escape" && control.shown()) control.dismiss();
    };
    root.addEventListener("pointerover", over);
    root.addEventListener("pointerout", out);
    root.addEventListener("pointerup", up);
    root.addEventListener("focusin", focusIn);
    root.addEventListener("focusout", focusOut);
    root.addEventListener("keydown", key);
    document.addEventListener("pointerdown", down, true);
    return () => {
      root.removeEventListener("pointerover", over);
      root.removeEventListener("pointerout", out);
      root.removeEventListener("pointerup", up);
      root.removeEventListener("focusin", focusIn);
      root.removeEventListener("focusout", focusOut);
      root.removeEventListener("keydown", key);
      document.removeEventListener("pointerdown", down, true);
    };
  }, [root, control]);

  // Beside its node, inside the graph's part in the window; hidden while the node is out of sight. Set on the
  // element, so following a pan or a scroll does not render the card again.
  const place = useCallback(() => {
    const element = cardRef.current;
    const node = shown && root?.querySelector(`.react-flow__node[data-id="${CSS.escape(shown)}"]`);
    if (!root || !element || !node) return;
    const frame = root.getBoundingClientRect();
    const local = (r: Box): Box => ({ left: r.left - frame.left, top: r.top - frame.top, width: r.width, height: r.height });
    const area = local(intersect(frame, { left: 0, top: 0, width: window.innerWidth, height: window.innerHeight }));
    const at = local(node.getBoundingClientRect());
    const seen = intersect(at, area);
    if (!seen.width || !seen.height) {
      element.style.visibility = "hidden";
      return;
    }
    element.style.maxHeight = `${Math.max(0, area.height - 2 * CARD_GAP)}px`;
    const { left, top, height } = placeCard(at, { width: element.offsetWidth, height: element.offsetHeight }, area);
    Object.assign(element.style, { left: `${left}px`, top: `${top}px`, maxHeight: `${height}px`, visibility: "visible",
                                   opacity: "1" });
  }, [root, shown]);

  useLayoutEffect(place, [place, card, transform]);

  // Placed again when the page scrolls or resizes, or the card's content changes its size.
  useEffect(() => {
    const element = cardRef.current;
    if (!card || !element) return;
    const observer = new ResizeObserver(place);
    observer.observe(element);
    window.addEventListener("scroll", place, { capture: true, passive: true });
    window.addEventListener("resize", place);
    return () => {
      observer.disconnect();
      window.removeEventListener("scroll", place, { capture: true });
      window.removeEventListener("resize", place);
    };
  }, [card, place]);

  if (!card) return null;
  return (
    <div
      key={shown}
      ref={cardRef}
      role="dialog"
      aria-label={`${card.name}: details`}
      className={cn(
        "nowheel nopan nodrag absolute z-10 w-80 max-w-[calc(100%-16px)] overflow-y-auto rounded-lg border border-border bg-popover p-3 text-popover-foreground shadow-md",
        // Shown by `place` once it has a place.
        "invisible left-0 top-0 opacity-0 motion-safe:transition-opacity motion-safe:duration-150",
      )}
    >
      <NodeCardView card={card} />
    </div>
  );
}
