import type { GateState, UserGate } from "@/lib/api";

/** How each gate state reads: a word (color is never the only signal), a badge tone and an icon tone. */
export const GATE_STATES: Record<
  GateState,
  { label: string; variant: "default" | "success" | "warning" | "outline"; tone: string }
> = {
  pending: { label: "Not open yet", variant: "outline", tone: "text-muted-foreground" },
  open: { label: "Waiting for you", variant: "warning", tone: "text-warning" },
  discussing: { label: "Discussing", variant: "default", tone: "text-primary" },
  passed: { label: "Passed", variant: "success", tone: "text-success" },
  cancelled: { label: "Cancelled", variant: "outline", tone: "text-muted-foreground" },
  skipped: { label: "Skipped", variant: "outline", tone: "text-muted-foreground" },
};

/** A gate that waits for the user: open, or discussing. */
export const isGateWaiting = (gate: Pick<UserGate, "state">) => gate.state === "open" || gate.state === "discussing";

/** A gate the Overview tab shows: one that waits, or one that has closed (passed or cancelled). */
export const isGateShown = (gate: Pick<UserGate, "state">) =>
  isGateWaiting(gate) || gate.state === "passed" || gate.state === "cancelled";

/** The id of a gate's place on the Overview tab, where the Pipeline's gate node takes the user. */
export const gateAnchor = (step: string) => `gate-${step}`;

/** The id of a step's row in the Overview's Steps list. */
export const stepAnchor = (stepId: string) => `step-${stepId}`;

/** How long since `iso`, for people: 45 s, 12 min, 2 h 5 min, 3 d 4 h. */
export function sinceText(iso: string | null): string {
  if (!iso) return "";
  const seconds = Math.max(0, Math.floor((Date.now() - new Date(iso).getTime()) / 1000));
  if (!Number.isFinite(seconds)) return "";
  if (seconds < 60) return `${seconds} s`;
  const minutes = Math.floor(seconds / 60);
  if (minutes < 60) return `${minutes} min`;
  const hours = Math.floor(minutes / 60);
  if (hours < 24) return `${hours} h${minutes % 60 ? ` ${minutes % 60} min` : ""}`;
  return `${Math.floor(hours / 24)} d${hours % 24 ? ` ${hours % 24} h` : ""}`;
}
