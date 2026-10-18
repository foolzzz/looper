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
}

export const RUNTIME_TYPES: Record<string, RuntimeTypeInfo> = {
  "claude-code": { kind: "CLI", title: "Claude Code CLI", install: "npm install -g @anthropic-ai/claude-code" },
  codex: { kind: "CLI", title: "Codex CLI", install: "npm install -g @openai/codex" },
  omp: { kind: "CLI", title: "OMP CLI" },
  grok: { kind: "CLI", title: "Grok CLI" },
};

/** How the operator checks a runtime's own login on the server's machine, and signs in again (`how`: with what). */
export interface RuntimeLoginHelp {
  /** The command that reports the login; none when the runtime has no such command. */
  check?: string;
  signIn: string;
  how?: string;
}

export const LOGIN_HELP: Record<string, RuntimeLoginHelp> = {
  codex: { check: "codex login status", signIn: "codex login", how: "with ChatGPT" },
  grok: { signIn: "grok login" },
};

/** Whether a runtime login the server reports can run: Codex's ChatGPT login, or Grok's signed-in login. */
export function loginUsable(state: string): boolean {
  return state === "chatgpt" || state === "logged_in";
}

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
