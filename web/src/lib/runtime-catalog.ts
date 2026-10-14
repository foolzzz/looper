/** What the dashboard says about each runtime type beyond what the server reports: its kind and how to install
 * it. The server's runtime types (`Registry.runtime_types`) are the ones that can run; the planned ones cannot
 * be selected yet. */

export type RuntimeKind = "CLI" | "SDK";

export interface RuntimeTypeInfo {
  kind: RuntimeKind;
  /** The product name, as a card titles it. */
  title: string;
  /** The command that installs it, when there is one to give. */
  install?: string;
  /** Its own login, while Looper cannot sign in with it yet: why, for a disabled entry. A type whose server
   * `auth_kinds` include "oauth" has a login Looper uses; one with neither has none. */
  loginComing?: string;
}

export const RUNTIME_TYPES: Record<string, RuntimeTypeInfo> = {
  "claude-code": { kind: "CLI", title: "Claude Code CLI", install: "npm install -g @anthropic-ai/claude-code" },
  codex: {
    kind: "CLI",
    title: "Codex CLI",
    install: "npm install -g @openai/codex",
    loginComing: "coming soon; use an API-key provider such as CPA for now",
  },
  omp: { kind: "CLI", title: "OMP CLI" },
};

/** Runtime types Looper does not run yet. */
export const PLANNED_RUNTIMES: { title: string; kind: RuntimeKind }[] = [
  { title: "Claude Code SDK", kind: "SDK" },
  { title: "OpenAI Agents SDK", kind: "SDK" },
  { title: "norma", kind: "SDK" },
];

const ROLE_LABELS: Record<string, string> = {
  manager: "Manager",
  planner: "Planner",
  worker: "Worker",
  reviewer: "Reviewer",
};

export function roleLabel(role: string): string {
  return ROLE_LABELS[role] ?? role;
}
