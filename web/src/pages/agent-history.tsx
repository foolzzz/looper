import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  AlertTriangle,
  ArrowLeft,
  Bot,
  Brain,
  ChevronDown,
  ChevronRight,
  Copy,
  Check,
  Library,
  Loader2,
  MessageSquare,
  Plug,
  ScrollText,
  Shrink,
  Terminal,
  User,
  Wrench,
} from "lucide-react";
import ReactMarkdown from "react-markdown";
import { Badge } from "@/components/ui/badge";
import { ContextUsagePanel } from "@/components/context-usage";
import { cn } from "@/lib/utils";
import { compactionChange, formatTokens, formatUsd } from "@/lib/usage";
import {
  fetchChatMessages,
  fetchLlmRecord,
  fetchLlmRecordEvents,
  fetchLlmRecords,
  fetchTaskActivity,
  type ActivityEvent,
  type Compaction,
  type DbChatMessage,
  type LlmRecord,
  type LlmRecordEvent,
  type PromptLayers,
} from "@/lib/api";

// A run with no output for this long is flagged as possibly stuck.
const STUCK_AFTER_MS = 2 * 60 * 1000;
const POLL_MS = 2000;
const LONG_ARG = 200;

type Block = Record<string, unknown>;

function formatClock(iso: string | null) {
  if (!iso) return "--:--:--";
  const date = new Date(iso);
  return Number.isNaN(date.getTime()) ? iso : date.toLocaleTimeString([], { hour12: false });
}

function formatDateTime(iso: string | null) {
  return iso ? new Date(iso).toLocaleString() : "-";
}

function formatMs(ms: number) {
  const sec = Math.max(0, Math.round(ms / 1000));
  if (sec < 60) return `${sec}s`;
  const min = Math.floor(sec / 60);
  return min < 60 ? `${min}m ${sec % 60}s` : `${Math.floor(min / 60)}h ${min % 60}m`;
}

function agentRole(agentId: string) {
  if (agentId === "manager") return "Manager";
  if (agentId.startsWith("planner")) return "Planner";
  if (agentId.startsWith("reviewer")) return "Reviewer";
  if (agentId.startsWith("worker")) return "Worker";
  return "Agent";
}

function shellQuote(arg: string) {
  return /^[A-Za-z0-9_\-./=:@%+,]+$/.test(arg) ? arg : `'${arg.replace(/'/g, `'\\''`)}'`;
}

/** Parse "<agentId>?task=<taskId>" from the #history/ route. */
export function parseHistoryRoute(sub: string): { agentId: string; taskId: string | null } {
  const [agentPart, query = ""] = sub.split("?");
  return {
    agentId: decodeURIComponent(agentPart || ""),
    taskId: new URLSearchParams(query).get("task"),
  };
}

/** The task router logs the Planner's progress reports as "planner". */
export function activityBelongsTo(ev: ActivityEvent, agentId: string) {
  const worker = ev.worker || "system";
  return worker === agentId || (agentId.startsWith("planner") && worker === "planner");
}

export function historyHref(agentId: string, taskId?: string | null) {
  return `#history/${encodeURIComponent(agentId)}${taskId ? `?task=${encodeURIComponent(taskId)}` : ""}`;
}

function CopyButton({ text, label }: { text: string; label: string }) {
  const [copied, setCopied] = useState(false);
  return (
    <button
      type="button"
      onClick={() => {
        navigator.clipboard?.writeText(text).then(() => {
          setCopied(true);
          setTimeout(() => setCopied(false), 1500);
        });
      }}
      className="flex items-center gap-1 rounded border border-border px-2 py-0.5 text-[11px] text-muted-foreground hover:text-foreground hover:bg-accent"
    >
      {copied ? <Check className="h-3 w-3" /> : <Copy className="h-3 w-3" />}
      {copied ? "Copied" : label}
    </button>
  );
}

function Collapsible({
  title,
  icon,
  children,
  defaultOpen = false,
  tone,
}: {
  title: React.ReactNode;
  icon?: React.ReactNode;
  children: React.ReactNode;
  defaultOpen?: boolean;
  tone?: "error";
}) {
  const [open, setOpen] = useState(defaultOpen);
  return (
    <div className={cn("rounded-md border", tone === "error" ? "border-destructive/40" : "border-border")}>
      <button
        type="button"
        onClick={() => setOpen(!open)}
        className="flex w-full items-center gap-2 px-3 py-1.5 text-left text-xs hover:bg-accent/40"
      >
        {open ? <ChevronDown className="h-3 w-3 shrink-0" /> : <ChevronRight className="h-3 w-3 shrink-0" />}
        {icon}
        <span className={cn("min-w-0 truncate", tone === "error" && "text-destructive")}>{title}</span>
      </button>
      {open && <div className="border-t border-border px-3 py-2">{children}</div>}
    </div>
  );
}

function Pre({ text }: { text: string }) {
  return (
    <pre className="max-h-96 overflow-auto whitespace-pre-wrap break-all font-mono text-[11px] leading-relaxed text-muted-foreground">
      {text}
    </pre>
  );
}

function LaunchCommand({ record }: { record: LlmRecord }) {
  const command = record.metadata?.command ?? [];
  const cwd = record.metadata?.cwd ?? "";
  const [expanded, setExpanded] = useState<Set<number>>(new Set());
  if (!command.length) {
    return <p className="text-xs text-muted-foreground">Launch command was not recorded for this run.</p>;
  }
  const full = `${cwd ? `cd ${shellQuote(cwd)} && ` : ""}${command.map(shellQuote).join(" ")}`;
  return (
    <div className="rounded-md border border-border bg-muted/30 p-3">
      <div className="mb-2 flex items-center justify-between gap-2">
        <span className="flex items-center gap-1.5 text-xs font-medium">
          <Terminal className="h-3.5 w-3.5" /> Launch command
        </span>
        <CopyButton text={full} label="Copy" />
      </div>
      {cwd && <p className="mb-1 font-mono text-[11px] text-muted-foreground">cwd: {cwd}</p>}
      <p className="font-mono text-[11px] leading-relaxed break-all">
        {command.map((arg, i) => {
          const long = arg.length > LONG_ARG;
          const shown = long && !expanded.has(i);
          // The appended system prompt (inline in older runs) also has its own section below.
          const note =
            command[i - 1] === "--append-system-prompt"
              ? ", see System prompt"
              : command[i - 1] === "--settings"
                ? ", isolation settings"
                : "";
          return (
            <span key={i}>
              {i > 0 && " "}
              {shown ? (
                <button
                  type="button"
                  onClick={() => setExpanded(new Set(expanded).add(i))}
                  className="rounded bg-primary/10 px-1 text-primary hover:bg-primary/20"
                  title="Show the full argument"
                >
                  {"<"}{arg.length} chars{note}{">"}
                </button>
              ) : long ? (
                <span className="whitespace-pre-wrap">{shellQuote(arg)}</span>
              ) : (
                shellQuote(arg)
              )}
            </span>
          );
        })}
      </p>
    </div>
  );
}

/** The value after ``flag`` in an argv, if any. */
function argAfter(command: string[], flag: string) {
  const at = command.indexOf(flag);
  return at >= 0 && at + 1 < command.length ? command[at + 1] : undefined;
}

function shortSha(sha: string | null | undefined) {
  return sha ? sha.slice(0, 12) : "";
}

/** Where a run's settings came from: the settings layers, then the names runs recorded before the layers used. */
const SETTING_SOURCES: Record<string, string> = {
  builtin: "built-in default",
  system: "Settings",
  role: "role (Agents page)",
  project: "project",
  role_project: "role in project",
  builtin_default: "built-in default",
  system_default: "Settings default",
  manager_override: "Manager override",
  agent_override: "agent override",
  project_override: "project override",
};
const sourceName = (source: string) => SETTING_SOURCES[source] ?? source;

/**
 * One line per layer, in prompt order: where the harness, role, project, task and contract came from;
 * then the model the run asked for and the Settings environment variables it got.
 */
function PromptLayerSummary({ layers, metadata }: { layers: PromptLayers; metadata: LlmRecord["metadata"] }) {
  const { harness, role, project, task, contract } = layers;
  const rows: [string, string][] = [];
  if (harness) {
    rows.push([
      "Harness",
      [
        sourceName(harness.source) + (harness.from_role ? ` (the ${harness.from_role}'s)` : ""),
        harness.project_id,
        shortSha(harness.sha256),
      ]
        .filter(Boolean)
        .join(" · "),
    ]);
  }
  if (role) rows.push(["Role", `${role.config_id} · ${role.recommended ? "recommended" : "edited"} · ${shortSha(role.sha256)}`]);
  rows.push([
    "Project",
    project
      ? [
          project.id,
          project.background_sha256 ? `background ${shortSha(project.background_sha256)}` : "no background",
          project.updated_at ? `updated ${formatDateTime(project.updated_at)}` : "",
        ]
          .filter(Boolean)
          .join(" · ")
      : "none",
  ]);
  rows.push([
    "Task",
    task
      ? [task.kind, [task.task_id, task.step_id].filter(Boolean).join(" / "), shortSha(task.sha256)].filter(Boolean).join(" · ")
      : "none",
  ]);
  if (contract) rows.push(["Contract", `${contract.name} · ${shortSha(contract.sha256)}`]);
  const choice = metadata?.model_choice;
  if (choice) {
    rows.push([
      "Model",
      [choice.model, sourceName(choice.source), choice.effort && `effort ${choice.effort}`]
        .filter(Boolean)
        .join(" · "),
    ]);
  }
  const sources = metadata?.settings_sources;
  if (metadata?.env) {
    const keys = Object.keys(metadata.env.keys).map((key) =>
      sources?.env?.[key] ? `${key} (${sourceName(sources.env[key])})` : key,
    );
    rows.push(["Env", `${keys.join(", ")} · ${shortSha(metadata.env.sha256)}`]);
  }
  if (sources) {
    rows.push([
      "Sources",
      `effort ${sourceName(sources.effort)} · run cap ${sourceName(sources.run_budget_usd)}`,
    ]);
  }
  return (
    <dl className="mb-2 grid grid-cols-[auto_1fr] gap-x-3 gap-y-0.5 font-mono text-[11px]">
      {rows.map(([name, value]) => (
        <div key={name} className="contents">
          <dt className="text-muted-foreground">{name}</dt>
          <dd className="min-w-0 break-all">{value}</dd>
        </div>
      ))}
    </dl>
  );
}

/** What LoopX appended to Claude Code's system prompt for this run. */
function SystemPrompt({ record }: { record: LlmRecord }) {
  const metadata = record.metadata;
  // Runs recorded before metadata.system_prompt existed may still carry it in their command.
  const text = metadata?.system_prompt ?? argAfter(metadata?.command ?? [], "--append-system-prompt");
  if (text == null) return null;
  const sha = metadata?.system_prompt_sha256;
  const layers = metadata?.prompt_layers;
  return (
    <Collapsible
      icon={<ScrollText className="h-3 w-3" />}
      title={
        <span className="flex items-center gap-1.5">
          <span className="truncate">
            System prompt · {text.length} chars{sha ? ` · ${shortSha(sha)}` : " · appended by LoopX"}
          </span>
          {layers?.isolation && (
            <Badge
              variant="outline"
              className="shrink-0 py-0 text-[10px]"
              title={`Ran without the operator's personal Claude Code settings, memory, hooks, plugins or MCP servers (${layers.isolation})`}
            >
              isolated
            </Badge>
          )}
        </span>
      }
    >
      {layers && <PromptLayerSummary layers={layers} metadata={metadata} />}
      {sha && (
        <div className="mb-2 flex items-center gap-2">
          <span className="min-w-0 break-all font-mono text-[11px] text-muted-foreground">sha256 {sha}</span>
          <CopyButton text={sha} label="Copy" />
        </div>
      )}
      <p className="mb-2 text-[11px] text-muted-foreground">
        The part LoopX appends to Claude Code&apos;s system prompt; its built-in system prompt is not included.
      </p>
      <Pre text={text} />
    </Collapsible>
  );
}

/**
 * The plugins and skills this run loaded: the catalog items installed for its role, globally or in its project, with
 * their versions. Runs recorded before the catalog list their project's skills apart (`project_skills`).
 */
function LoadedLibrary({ record }: { record: LlmRecord }) {
  const library = record.metadata?.library;
  const project = record.metadata?.project_skills;
  if (!library && !project) return null;
  const items = library?.items ?? [];
  // A skill is named <plugin>:<name>; runs recorded before the catalog give only its name.
  const named = (item: (typeof items)[number]) => ({
    name: item.kind === "skill" && item.plugin ? `${item.plugin}:${item.name}` : item.name,
    sha256: item.sha256,
  });
  const plugins = items.filter((item) => item.kind === "plugin").map(named);
  const skills = items.filter((item) => item.kind === "skill");
  const servers = items.filter((item) => item.kind === "mcp").map(named);
  const globalSkills = skills.filter((item) => item.scope !== "project").map(named);
  const projectSkills = [
    ...skills.filter((item) => item.scope === "project").map(named),
    ...(project?.skills ?? []).map((skill) => ({ name: `${project?.plugin ?? "project"}:${skill.name}`, sha256: skill.sha256 })),
  ];
  const plural = (n: number, word: string) => `${n} ${word}${n === 1 ? "" : "s"}`;
  const parts = [
    library &&
      (library.error
        ? `not loaded: ${library.error}`
        : `${plural(plugins.length, "plugin")}, ${plural(skills.length, "skill")}${
            servers.length ? `, ${plural(servers.length, "MCP server")}` : ""
          } · ${shortSha(library.sha256)}`),
    project &&
      (project.error ? `project skills not loaded: ${project.error}` : plural(project.skills.length, "project skill")),
  ].filter(Boolean);
  const groups = [
    ["Plugins", plugins],
    ["Skills (global)", globalSkills],
    ["Skills (project)", projectSkills],
    ["MCP servers", servers],
  ] as const;
  return (
    <Collapsible
      icon={<Library className="h-3 w-3" />}
      title={`Plugins & skills · ${parts.join(" · ")}`}
      tone={library?.error || project?.error ? "error" : undefined}
    >
      {library?.sha256 && (
        <div className="mb-2 flex items-center gap-2">
          <span className="min-w-0 break-all font-mono text-[11px] text-muted-foreground">library sha256 {library.sha256}</span>
          <CopyButton text={library.sha256} label="Copy" />
        </div>
      )}
      {groups.map(([name, group]) =>
        group.length ? (
          <div key={name} className="mb-2">
            <p className="mb-0.5 text-[11px] font-medium text-muted-foreground">{name}</p>
            <ul className="grid gap-x-4 gap-y-0.5 font-mono text-[11px] sm:grid-cols-2">
              {group.map((item) => (
                <li key={item.name} className="flex min-w-0 justify-between gap-2">
                  <span className="truncate" title={item.name}>{item.name}</span>
                  <span className="shrink-0 text-muted-foreground">{shortSha(item.sha256)}</span>
                </li>
              ))}
            </ul>
          </div>
        ) : null,
      )}
    </Collapsible>
  );
}

function toolSummary(input: Block) {
  for (const key of ["command", "file_path", "pattern", "url", "description", "prompt"]) {
    const value = input[key];
    if (typeof value === "string" && value) return value.replace(/\s+/g, " ").slice(0, 160);
  }
  return "";
}

function resultText(content: unknown): string {
  if (typeof content === "string") return content;
  if (Array.isArray(content)) {
    return content
      .map((part) => (part && typeof part === "object" && "text" in part ? String((part as Block).text) : JSON.stringify(part)))
      .join("\n");
  }
  return JSON.stringify(content, null, 2);
}

function isThinkingProgress(event: Block) {
  return event.type === "system" && event.subtype === "thinking_tokens";
}

/** Events to show: consecutive thinking-progress rows collapse into one, keeping the latest count. */
function displayEvents(events: LlmRecordEvent[]): { item: LlmRecordEvent; startedAt: string }[] {
  const out: { item: LlmRecordEvent; startedAt: string }[] = [];
  for (const item of events) {
    const last = out[out.length - 1];
    if (last && isThinkingProgress(item.event) && isThinkingProgress(last.item.event)) {
      last.item = item;
    } else {
      out.push({ item, startedAt: item.created_at });
    }
  }
  return out;
}

function thinkingTokens(event: Block) {
  const n = event.estimated_tokens;
  return typeof n === "number" ? n.toLocaleString() : "?";
}

/** One muted line, optionally expandable to show detail. */
function EventLine({
  icon,
  text,
  tone,
  detail,
}: {
  icon: React.ReactNode;
  text: React.ReactNode;
  tone?: "error";
  detail?: string;
}) {
  const body = (
    <span className={cn("flex min-w-0 items-center gap-1.5 text-xs", tone === "error" ? "text-destructive" : "text-muted-foreground")}>
      {icon}
      <span className="min-w-0 truncate">{text}</span>
    </span>
  );
  if (!detail) return <div className="min-w-0 flex-1 py-0.5">{body}</div>;
  return (
    <div className="min-w-0 flex-1">
      <Collapsible title={body} tone={tone}>
        <Pre text={detail} />
      </Collapsible>
    </div>
  );
}

function EventView({ item, startedAt, live }: { item: LlmRecordEvent; startedAt?: string; live?: boolean }) {
  const event = item.event;
  const type = event.type;
  const time = (
    <span className="shrink-0 font-mono text-[11px] tabular-nums text-muted-foreground">
      {formatClock(startedAt ?? item.created_at)}
    </span>
  );

  if (type === "system" && event.subtype === "init") {
    return (
      <div className="flex items-center gap-2 text-[11px] text-muted-foreground">
        {time}
        <span>Session started · {String(event.model ?? "model ?")} · {String(event.session_id ?? "")}</span>
      </div>
    );
  }
  if (isThinkingProgress(event)) {
    return (
      <div className="flex gap-2">
        {time}
        <EventLine
          icon={live ? <Loader2 className="h-3 w-3 animate-spin" /> : <Brain className="h-3 w-3" />}
          text={`${live ? "Thinking…" : "Thought"} · ~${thinkingTokens(event)} tokens`}
        />
      </div>
    );
  }
  if (type === "system" && event.subtype === "compact_boundary") {
    const meta = (event.compact_metadata as Compaction | undefined) ?? {};
    return (
      <div className="flex gap-2">
        {time}
        <EventLine
          icon={<Shrink className="h-3 w-3" />}
          text={`Context compacted (${String(meta.trigger ?? "?")}) · ${compactionChange(meta)}`}
          detail={JSON.stringify(meta, null, 2)}
        />
      </div>
    );
  }
  if (type === "system" && event.subtype === "status" && event.compact_result === "failed") {
    return (
      <div className="flex gap-2">
        {time}
        <EventLine
          icon={<Shrink className="h-3 w-3" />}
          text={`Context compaction failed · ${String(event.compact_error ?? "no reason given")}`}
          tone="error"
        />
      </div>
    );
  }
  if (type === "system" && (event.subtype === "hook_started" || event.subtype === "status")) {
    return null; // the hook's response line and the run itself say enough
  }
  if (type === "system" && event.subtype === "hook_response") {
    const failed = event.outcome !== "success" || (typeof event.exit_code === "number" && event.exit_code !== 0);
    const detail = ["output", "stdout", "stderr"]
      .filter((key) => typeof event[key] === "string" && event[key])
      .map((key) => `${key}:\n${String(event[key])}`)
      .join("\n\n");
    return (
      <div className="flex gap-2">
        {time}
        <EventLine
          icon={<Plug className="h-3 w-3" />}
          text={`Hook ${String(event.hook_name ?? event.hook_event ?? "")} · ${String(event.outcome ?? "?")} (exit ${String(event.exit_code ?? "?")})`}
          tone={failed ? "error" : undefined}
          detail={detail || undefined}
        />
      </div>
    );
  }
  if (type === "system" && event.subtype === "task_started") {
    return (
      <div className="flex gap-2">
        {time}
        <EventLine
          icon={<Terminal className="h-3 w-3" />}
          text={`Background task started · ${String(event.description ?? event.task_id ?? "")} · ${String(event.task_type ?? "")}`}
        />
      </div>
    );
  }
  if (type === "system" && event.subtype === "task_notification") {
    return (
      <div className="flex gap-2">
        {time}
        <EventLine
          icon={<Terminal className="h-3 w-3" />}
          text={`Background task ${String(event.status ?? "")} · ${String(event.summary ?? event.task_id ?? "")}`}
          tone={event.status === "failed" ? "error" : undefined}
          detail={event.output_file ? `output file: ${String(event.output_file)}` : undefined}
        />
      </div>
    );
  }
  if (type === "rate_limit_event") {
    const info = (event.rate_limit_info as Block | undefined) ?? {};
    const windows = (info.unifiedWindows as Record<string, Block> | undefined) ?? {};
    const usage = Object.entries(windows)
      .map(([name, w]) => `${name} ${Math.round(Number(w.utilization ?? 0) * 100)}%`)
      .join(", ");
    return (
      <div className="flex gap-2">
        {time}
        <EventLine
          icon={<AlertTriangle className="h-3 w-3" />}
          text={`Rate limit · ${String(info.status ?? "?")}${usage ? ` · ${usage} used` : ""}`}
          tone={info.status && info.status !== "allowed" ? "error" : undefined}
          detail={JSON.stringify(info, null, 2)}
        />
      </div>
    );
  }
  if (type === "assistant") {
    const content = ((event.message as Block | undefined)?.content as Block[] | undefined) ?? [];
    return (
      <div className="flex gap-2">
        {time}
        <div className="min-w-0 flex-1 space-y-1.5">
          {content.map((block, i) => {
            if (block.type === "text") {
              return (
                <div key={i} className="rounded-lg bg-muted px-3 py-2 text-sm">
                  <div className="prose prose-invert prose-sm max-w-none [&>*:first-child]:mt-0 [&>*:last-child]:mb-0">
                    <ReactMarkdown>{String(block.text ?? "")}</ReactMarkdown>
                  </div>
                </div>
              );
            }
            if (block.type === "thinking") {
              const text = String(block.thinking ?? "");
              if (!text) return null; // not exposed; the thinking-progress line covers it
              return (
                <Collapsible key={i} icon={<Brain className="h-3 w-3" />} title={`Thinking (${text.length} chars)`}>
                  <Pre text={text} />
                </Collapsible>
              );
            }
            if (block.type === "tool_use") {
              const input = (block.input as Block | undefined) ?? {};
              return (
                <Collapsible
                  key={i}
                  icon={<Wrench className="h-3 w-3 text-primary" />}
                  title={<><span className="font-medium">{String(block.name)}</span> <span className="text-muted-foreground">{toolSummary(input)}</span></>}
                >
                  <Pre text={JSON.stringify(input, null, 2)} />
                </Collapsible>
              );
            }
            return null;
          })}
        </div>
      </div>
    );
  }
  if (type === "user") {
    const content = ((event.message as Block | undefined)?.content as Block[] | string | undefined) ?? [];
    const blocks = Array.isArray(content) ? content : [{ type: "text", text: content }];
    return (
      <div className="flex gap-2">
        {time}
        <div className="min-w-0 flex-1 space-y-1.5">
          {blocks.map((block, i) => {
            if (block.type === "text" && typeof block.text === "string" && block.text) {
              // A user turn on stdin: the prompt, or a message passed on mid-run.
              const text = block.text;
              return text.length > 400 ? (
                <Collapsible key={i} icon={<User className="h-3 w-3" />} title={`User turn · ${text.length} chars · ${text.slice(0, 80)}`}>
                  <Pre text={text} />
                </Collapsible>
              ) : (
                <div key={i} className="flex items-start gap-2 rounded-lg border border-primary/30 bg-primary/5 px-3 py-2 text-sm">
                  <User className="mt-0.5 h-3.5 w-3.5 shrink-0 text-primary" />
                  <span className="whitespace-pre-wrap">{text}</span>
                </div>
              );
            }
            if (block.type !== "tool_result") return null;
            const text = resultText(block.content);
            const firstLine = text.split("\n").find((line) => line.trim()) ?? "";
            return (
              <Collapsible
                key={i}
                tone={block.is_error ? "error" : undefined}
                title={`${block.is_error ? "Tool error" : "Tool result"} · ${text.length} chars · ${firstLine.slice(0, 120)}`}
              >
                <Pre text={text} />
              </Collapsible>
            );
          })}
        </div>
      </div>
    );
  }
  if (type === "result") {
    const ok = event.subtype === "success" && !event.is_error;
    // Claude's figure is its session's running total; the run's own cost is in the card's header.
    const cost = typeof event.total_cost_usd === "number" ? ` · session total ${formatUsd(event.total_cost_usd)}` : "";
    return (
      <div className="flex gap-2">
        {time}
        <div className={cn("min-w-0 flex-1 rounded-md border px-3 py-2 text-xs", ok ? "border-success/40" : "border-destructive/40")}>
          <p className={cn("font-medium", ok ? "text-success" : "text-destructive")}>
            Run finished · {String(event.subtype)} · {String(event.num_turns ?? "?")} turns{cost}
          </p>
          {typeof event.result === "string" && event.result && <Pre text={event.result} />}
        </div>
      </div>
    );
  }
  return (
    <div className="flex gap-2">
      {time}
      <EventLine
        icon={<Bot className="h-3 w-3" />}
        text={`${String(type ?? "event")}${event.subtype ? ` · ${String(event.subtype)}` : ""}`}
        detail={JSON.stringify(event, null, 2)}
      />
    </div>
  );
}

/** The run's own tokens (cache included), its context at the end, its compactions and its own cost. */
function RunUsage({ record }: { record: LlmRecord }) {
  const tokens: [string, number | null | undefined, string][] = [
    ["in", record.input_tokens, "Uncached input tokens"],
    ["cache read", record.cache_read_tokens, "Prompt tokens read from the cache"],
    ["cache write", record.cache_creation_tokens, "Prompt tokens written to the cache"],
    ["out", record.output_tokens, "Output tokens"],
  ];
  const shown = tokens.filter(([, value]) => value != null);
  const compactions = record.compactions ?? [];
  return (
    <>
      {shown.length > 0 && (
        <span className="font-mono text-muted-foreground" title="This run's own tokens">
          {shown.map(([label, value, title], i) => (
            <span key={label} title={title}>
              {i > 0 && " · "}
              {formatTokens(value)} {label}
            </span>
          ))}
        </span>
      )}
      {record.context_tokens != null && (
        <span
          className="font-mono text-muted-foreground"
          title="Context at the end of the run: the prompt of its last model request"
        >
          ctx {formatTokens(record.context_tokens)}
        </span>
      )}
      {compactions.length > 0 && (
        <Badge
          variant="outline"
          className="py-0 text-[10px]"
          title={compactions.map((c) => `${c.trigger ?? "?"}: ${compactionChange(c)}`).join("\n")}
        >
          <Shrink className="mr-1 h-3 w-3" />
          compacted{compactions.length > 1 ? ` ×${compactions.length}` : ""}
        </Badge>
      )}
      {record.cost_usd != null && (
        <span className="font-mono text-muted-foreground" title="This run's own cost">
          {formatUsd(record.cost_usd)}
        </span>
      )}
    </>
  );
}

export function RunCard({ initial, defaultOpen }: { initial: LlmRecord; defaultOpen: boolean }) {
  const [record, setRecord] = useState(initial);
  const [events, setEvents] = useState<LlmRecordEvent[]>([]);
  const [open, setOpen] = useState(defaultOpen);
  const [now, setNow] = useState(Date.now());
  const lastSeq = useRef(0);

  const load = useCallback(async () => {
    // Re-read the last row too: thinking progress is updated in place.
    const fresh = await fetchLlmRecordEvents(initial.id, Math.max(0, lastSeq.current - 1)).catch(() => []);
    if (fresh.length) {
      lastSeq.current = Math.max(lastSeq.current, fresh[fresh.length - 1].seq);
      setEvents((prev) => {
        const bySeq = new Map(prev.map((e) => [e.seq, e]));
        for (const e of fresh) bySeq.set(e.seq, e);
        return [...bySeq.values()].sort((a, b) => a.seq - b.seq);
      });
    }
  }, [initial.id]);

  useEffect(() => {
    if (open) load();
  }, [open, load]);

  // Follow a running agent: new events, status and heartbeat.
  useEffect(() => {
    if (record.status !== "running") return;
    const timer = setInterval(async () => {
      setNow(Date.now());
      const latest = await fetchLlmRecord(initial.id).catch(() => null);
      if (latest) setRecord(latest);
      if (open) await load();
    }, POLL_MS);
    return () => clearInterval(timer);
  }, [record.status, open, load, initial.id]);

  const started = new Date(record.created_at).getTime();
  const duration = record.duration_ms ?? now - started;
  const lastOutput = new Date(record.last_output_at ?? record.created_at).getTime();
  const idle = now - lastOutput;
  const stuck = record.status === "running" && idle > STUCK_AFTER_MS;
  const shown = displayEvents(events);
  const lastShown = shown[shown.length - 1];
  const thinkingNow = record.status === "running" && lastShown && isThinkingProgress(lastShown.item.event);

  return (
    <div className="rounded-lg border border-border bg-card">
      <button
        type="button"
        onClick={() => setOpen(!open)}
        className="flex w-full flex-wrap items-center gap-x-3 gap-y-1 px-4 py-3 text-left text-xs hover:bg-accent/30"
      >
        {open ? <ChevronDown className="h-3.5 w-3.5" /> : <ChevronRight className="h-3.5 w-3.5" />}
        <span className="font-mono tabular-nums" title={formatDateTime(record.created_at)}>{formatClock(record.created_at)}</span>
        <Badge variant={record.status === "completed" ? "success" : record.status === "failed" ? "destructive" : "warning"}>
          {record.status === "running" && <Loader2 className="mr-1 h-3 w-3 animate-spin" />}
          {record.status}
        </Badge>
        <span className="text-muted-foreground">{formatMs(duration)}</span>
        {record.model && <span className="text-muted-foreground">{record.model}</span>}
        <RunUsage record={record} />
        {record.status === "running" && (
          <span className={cn("flex items-center gap-1", stuck ? "text-warning" : "text-muted-foreground")}>
            {stuck && <AlertTriangle className="h-3.5 w-3.5" />}
            {stuck ? `no output for ${formatMs(idle)} — may be stuck` : `last output ${formatMs(idle)} ago`}
          </span>
        )}
        {thinkingNow && (
          <span className="flex items-center gap-1 text-primary">
            <Brain className="h-3.5 w-3.5" /> thinking · ~{thinkingTokens(lastShown.item.event)} tokens
          </span>
        )}
        <span className="ml-auto min-w-0 truncate text-muted-foreground">{record.input_summary}</span>
      </button>
      {open && (
        <div className="space-y-3 border-t border-border px-4 py-3">
          {record.error && <p className="text-xs text-destructive">Error: {record.error}</p>}
          <LaunchCommand record={record} />
          <SystemPrompt record={record} />
          <LoadedLibrary record={record} />
          {record.input_text && (
            <Collapsible icon={<User className="h-3 w-3" />} title={`Input · ${record.input_text.length} chars`} defaultOpen={record.input_text.length < 400}>
              <Pre text={record.input_text} />
            </Collapsible>
          )}
          {events.length > 0 ? (
            <div className="space-y-2">
              {shown.map(({ item, startedAt }, i) => (
                <EventView
                  key={item.seq}
                  item={item}
                  startedAt={startedAt}
                  live={record.status === "running" && i === shown.length - 1}
                />
              ))}
            </div>
          ) : (
            <div className="space-y-2 text-xs text-muted-foreground">
              <p>
                {record.status === "running"
                  ? "Waiting for the first event…"
                  : "No transcript for this run: it was recorded before full transcripts existed."}
              </p>
              {record.output_text && <Pre text={record.output_text} />}
            </div>
          )}
        </div>
      )}
    </div>
  );
}

function ActivityList({ events }: { events: ActivityEvent[] }) {
  if (!events.length) return <p className="text-xs text-muted-foreground">No activity recorded.</p>;
  return (
    <div className="space-y-1">
      {events.map((ev) => (
        <div key={ev.seq} className="flex items-start gap-2 text-xs">
          <span className="shrink-0 font-mono tabular-nums text-muted-foreground" title={formatDateTime(ev.created_at)}>
            {formatClock(ev.created_at)}
          </span>
          <Badge variant={ev.kind === "error" ? "destructive" : "outline"} className="shrink-0 text-[10px] py-0">{ev.kind}</Badge>
          <span className={cn("min-w-0 whitespace-pre-wrap break-words", ev.kind === "error" && "text-destructive")}>{ev.summary || ev.kind}</span>
        </div>
      ))}
    </div>
  );
}

export function AgentHistoryPage({ sub }: { sub: string }) {
  const { agentId, taskId: routeTaskId } = useMemo(() => parseHistoryRoute(sub), [sub]);
  const role = agentRole(agentId);
  const [runs, setRuns] = useState<LlmRecord[] | null>(null);
  const [activity, setActivity] = useState<ActivityEvent[]>([]);
  const [chat, setChat] = useState<DbChatMessage[]>([]);

  const taskId = routeTaskId ?? runs?.find((r) => r.task_id)?.task_id ?? (agentId.startsWith("planner-") ? agentId.slice("planner-".length) : null);

  useEffect(() => {
    setRuns(null);
    fetchLlmRecords({ agent_id: agentId, limit: 50 }).then(setRuns).catch(() => setRuns([]));
  }, [agentId]);

  useEffect(() => {
    if (!taskId) return;
    fetchTaskActivity(taskId, 0, 1000)
      .then(({ activity: all }) => setActivity(all.filter((ev) => activityBelongsTo(ev, agentId))))
      .catch(() => setActivity([]));
    if (role === "Planner") {
      fetchChatMessages(`planner-${taskId}`).then(setChat).catch(() => setChat([]));
    }
  }, [taskId, agentId, role]);

  return (
    <div className="mx-auto max-w-5xl space-y-5 p-6">
      <div className="flex items-center gap-3">
        <button
          type="button"
          onClick={() => window.history.back()}
          className="flex h-8 w-8 items-center justify-center rounded-md border border-border text-muted-foreground hover:bg-accent hover:text-foreground"
          title="Back"
        >
          <ArrowLeft className="h-4 w-4" />
        </button>
        <Bot className="h-5 w-5 text-primary" />
        <h1 className="text-lg font-semibold">{agentId}</h1>
        <Badge variant="outline">{role}</Badge>
        {taskId && (
          <a href={`#tasks/${taskId}`} className="text-xs text-muted-foreground hover:text-foreground">
            task {taskId}
          </a>
        )}
      </div>

      {role === "Planner" && (
        <p className="text-xs text-muted-foreground">
          The Planner is this task's Claude Code agent. It plans the steps, decides what happens after each Worker or
          Reviewer result, and answers your messages. Each run below is one of its turns.
        </p>
      )}

      {(role === "Manager" || role === "Planner") && <ContextUsagePanel key={agentId} agentId={agentId} />}

      <section className="space-y-2">
        <h2 className="text-sm font-semibold">Runs</h2>
        {runs === null ? (
          <Loader2 className="h-4 w-4 animate-spin text-muted-foreground" />
        ) : runs.length === 0 ? (
          <p className="text-xs text-muted-foreground">No Claude runs recorded for this agent.</p>
        ) : (
          runs.map((record, i) => <RunCard key={record.id} initial={record} defaultOpen={i === 0} />)
        )}
      </section>

      {role === "Planner" && (
        <section className="space-y-2">
          <h2 className="flex items-center gap-1.5 text-sm font-semibold">
            <MessageSquare className="h-4 w-4" /> Chat with this Planner
          </h2>
          {chat.length === 0 ? (
            <p className="text-xs text-muted-foreground">
              No messages. Select this Planner on the Chat page to message it; it replies, and may pass your message
              to the running Worker or add steps to the plan.
            </p>
          ) : (
            <div className="space-y-1">
              {chat.map((msg) => (
                <div key={msg.message_id} className="flex items-start gap-2 text-xs">
                  <span className="shrink-0 font-mono tabular-nums text-muted-foreground">{formatClock(msg.created_at)}</span>
                  <Badge variant="outline" className="shrink-0 text-[10px] py-0">{msg.role}</Badge>
                  <span className="whitespace-pre-wrap">{msg.content}</span>
                </div>
              ))}
            </div>
          )}
        </section>
      )}

      {taskId && (
        <section className="space-y-2">
          <h2 className="text-sm font-semibold">Activity in task</h2>
          <ActivityList events={activity} />
        </section>
      )}
    </div>
  );
}
