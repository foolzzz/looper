import { useEffect, useState } from "react";
import { AlertCircle, ArrowRight, Folder, Loader2 } from "lucide-react";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { ClampedText } from "@/components/clamped-text";
import { ProjectKnowledgeSection } from "@/components/project-knowledge";
import { ProjectSkillsSection } from "@/components/project-skills";
import { ProjectSettingsForm } from "@/components/project-settings";
import {
  fetchKnowledge,
  fetchProjectAgentSettings,
  fetchProjectInstalls,
  fetchTasks,
  type Project,
  type ProjectAgentSettings,
  type ProjectInstalls,
  type ProjectRole,
  type ProjectKnowledge,
  type TaskInfo,
} from "@/lib/api";
import { useProjects } from "@/lib/use-projects";
import { formatUsd } from "@/lib/usage";
import { cn, formatBytes } from "@/lib/utils";

const TABS = [
  { id: "overview", label: "Overview" },
  { id: "knowledge", label: "Knowledge" },
  { id: "skills", label: "Skills" },
  { id: "settings", label: "Settings" },
] as const;
type Tab = (typeof TABS)[number]["id"];

/** How many tasks, documents and skills the overview lists before pointing to the full list. */
const PREVIEW = 5;

const tasksHref = (projectId: string) => `#tasks?project=${encodeURIComponent(projectId)}`;
const errorText = (err: unknown) => (err instanceof Error ? err.message : String(err));

/** Load `load()` once per key; the answer, or the error when it failed. */
function useLoad<T>(load: () => Promise<T>, key: string) {
  const [state, setState] = useState<{ key: string; data?: T; error?: string } | null>(null);
  useEffect(() => {
    let alive = true;
    load()
      .then((data) => alive && setState({ key, data }))
      .catch((err) => alive && setState({ key, error: errorText(err) }));
    return () => {
      alive = false;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [key]);
  return state?.key === key ? state : null;
}

function OverviewCard({
  title,
  meta,
  action,
  children,
}: {
  title: string;
  meta?: React.ReactNode;
  action?: React.ReactNode;
  children: React.ReactNode;
}) {
  return (
    <section aria-label={title} className="flex flex-col rounded-lg border border-border bg-card px-5 py-4">
      <div className="flex flex-wrap items-baseline justify-between gap-x-3 gap-y-1">
        <h2 className="text-sm font-semibold">{title}</h2>
        {meta && <span className="text-[11px] tabular-nums text-muted-foreground">{meta}</span>}
      </div>
      <div className="mt-3 flex-1 space-y-2">{children}</div>
      {action && <div className="mt-3 border-t border-border pt-3">{action}</div>}
    </section>
  );
}

function CardLink({ href, onClick, children }: { href?: string; onClick?: () => void; children: React.ReactNode }) {
  const className =
    "inline-flex items-center gap-1 rounded-sm text-xs font-medium text-primary underline-offset-2 hover:underline focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring";
  return href ? (
    <a href={href} className={className}>
      {children}
      <ArrowRight className="h-3 w-3" aria-hidden="true" />
    </a>
  ) : (
    <button type="button" onClick={onClick} className={className}>
      {children}
      <ArrowRight className="h-3 w-3" aria-hidden="true" />
    </button>
  );
}

function Loading({ error }: { error?: string }) {
  return error ? (
    <p role="alert" className="text-xs text-destructive">{error}</p>
  ) : (
    <Loader2 className="h-4 w-4 animate-spin text-muted-foreground" aria-label="Loading" />
  );
}

const STATUS_VARIANT: Record<string, "default" | "success" | "warning" | "destructive" | "outline"> = {
  running: "default",
  executing: "default",
  assigned: "default",
  pending: "warning",
  stopped: "warning",
  completed: "success",
  failed: "destructive",
};

function TasksCard({ project }: { project: Project }) {
  const loaded = useLoad(() => fetchTasks(project.project_id), `${project.project_id}:${project.task_count}`);
  const tasks = loaded?.data
    ? [...loaded.data].sort((a: TaskInfo, b: TaskInfo) => b.created_at.localeCompare(a.created_at)).slice(0, PREVIEW)
    : null;
  return (
    <OverviewCard
      title="Tasks"
      meta={`${project.task_count} ${project.task_count === 1 ? "task" : "tasks"}${
        project.active_count ? `, ${project.active_count} active` : ""
      }`}
      action={<CardLink href={tasksHref(project.project_id)}>All tasks in this project</CardLink>}
    >
      {!tasks ? (
        <Loading error={loaded?.error} />
      ) : tasks.length === 0 ? (
        <p className="text-xs text-muted-foreground">No tasks yet</p>
      ) : (
        <ul className="space-y-1.5">
          {tasks.map((task) => (
            <li key={task.task_id}>
              <a
                href={`#tasks/${encodeURIComponent(task.task_id)}`}
                className="flex items-center gap-2 rounded-sm text-sm hover:underline focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring"
              >
                <Badge variant={STATUS_VARIANT[task.status] ?? "outline"} className="shrink-0 capitalize">
                  {task.status}
                </Badge>
                <span className="min-w-0 flex-1 truncate">{task.requirement}</span>
                {task.cost && task.cost.runs > 0 && (
                  <span className="shrink-0 font-mono text-xs text-muted-foreground">{formatUsd(task.cost.cost_usd)}</span>
                )}
              </a>
            </li>
          ))}
        </ul>
      )}
    </OverviewCard>
  );
}

function KnowledgeCard({ project, onOpen }: { project: Project; onOpen: () => void }) {
  const loaded = useLoad<ProjectKnowledge>(() => fetchKnowledge(project.project_id), project.project_id);
  const knowledge = loaded?.data;
  const missing = knowledge?.documents.filter((doc) => doc.missing).length ?? 0;
  return (
    <OverviewCard
      title="Knowledge"
      meta={
        knowledge &&
        `${knowledge.count} ${knowledge.count === 1 ? "document" : "documents"} · ${formatBytes(knowledge.stored_bytes)} stored`
      }
      action={<CardLink onClick={onOpen}>Manage knowledge</CardLink>}
    >
      {!knowledge ? (
        <Loading error={loaded?.error} />
      ) : knowledge.count === 0 ? (
        <p className="text-xs text-muted-foreground">No documents. Agents get no knowledge index.</p>
      ) : (
        <>
          {missing > 0 && (
            <p className="flex items-center gap-1 text-xs text-warning">
              <AlertCircle className="h-3.5 w-3.5" aria-hidden="true" />
              {missing} referenced {missing === 1 ? "file is" : "files are"} missing
            </p>
          )}
          <ul className="space-y-1.5">
            {knowledge.documents.slice(0, PREVIEW).map((doc) => (
              <li key={doc.document_id} className="text-xs">
                <span className="font-medium text-foreground">{doc.title}</span>
                <span className="text-muted-foreground"> — {doc.summary}</span>
              </li>
            ))}
          </ul>
          {knowledge.count > PREVIEW && (
            <p className="text-xs text-muted-foreground">and {knowledge.count - PREVIEW} more</p>
          )}
        </>
      )}
    </OverviewCard>
  );
}

function SkillsCard({ project, onOpen }: { project: Project; onOpen: () => void }) {
  const loaded = useLoad<ProjectInstalls>(() => fetchProjectInstalls(project.project_id), project.project_id);
  const installs = loaded?.data?.installs;
  return (
    <OverviewCard
      title="Skills"
      meta={installs && `${installs.length} installed`}
      action={<CardLink onClick={onOpen}>Manage skills</CardLink>}
    >
      {!installs ? (
        <Loading error={loaded?.error} />
      ) : installs.length === 0 ? (
        <p className="text-xs text-muted-foreground">Nothing installed. Install plugins and skills from the Skills tab.</p>
      ) : (
        <>
          <ul className="space-y-1.5">
            {installs.slice(0, PREVIEW).map((entry) => (
              <li
                key={entry.install_id}
                className={cn("flex flex-wrap items-center gap-x-2 text-xs", !entry.enabled && "opacity-60")}
              >
                <span className="break-all font-mono font-medium text-foreground">{entry.item.qualified_name}</span>
                {!entry.enabled && <span className="text-muted-foreground">(off)</span>}
                {entry.inherited && (
                  <Badge variant="outline" className="py-0 text-[10px]">
                    global
                  </Badge>
                )}
              </li>
            ))}
          </ul>
          {installs.length > PREVIEW && (
            <p className="text-xs text-muted-foreground">and {installs.length - PREVIEW} more</p>
          )}
        </>
      )}
    </OverviewCard>
  );
}

/** The execution settings a project sets, in words; unset ones inherit and are left out. */
function executionRows(project: Project): [string, string][] {
  const rows: [string, string][] = [];
  if (project.review_policy)
    rows.push(["Review", project.review_policy === "all" ? "Every step goes to the Reviewer" : "The Planner decides"]);
  if (project.task_budget_usd != null) rows.push(["Spend cap per task", formatUsd(project.task_budget_usd)]);
  if (project.task_budget_action)
    rows.push([
      "At the task cap",
      project.task_budget_action === "wrap_up" ? "The Planner wraps up, then the task fails" : "The task fails at once",
    ]);
  if (project.supervise_interval_seconds != null)
    rows.push([
      "Planner supervision",
      project.supervise_interval_seconds === 0 ? "Off" : `Every ${project.supervise_interval_seconds} s`,
    ]);
  if (project.distill_lessons !== "off")
    rows.push([
      "Lessons",
      project.distill_lessons === "draft"
        ? "A draft to review in Knowledge when a task completes"
        : "Written into knowledge when a task completes",
    ]);
  return rows;
}

const PROJECT_ROLES = [
  ["planner", "Planner"],
  ["worker", "Worker"],
  ["reviewer", "Reviewer"],
] as const;

/** What the project's agents run with: each role's model, its spend cap per run when any has one, and which roles
 * this project gives a harness of their own. */
function agentRows(settings: ProjectAgentSettings): [string, string][] {
  const each = (show: (role: ProjectRole) => string) =>
    PROJECT_ROLES.map(([role, label]) => `${label} ${show(role)}`).join(" · ");
  const effective = (role: ProjectRole) => settings.roles[role].effective;
  const rows: [string, string][] = [["Models", each((role) => effective(role).model.value)]];
  if (PROJECT_ROLES.some(([role]) => effective(role).run_budget_usd.value != null))
    rows.push(["Spend cap per run", each((role) => {
      const cap = effective(role).run_budget_usd.value;
      return cap == null ? "none" : formatUsd(cap);
    })]);
  const harness = PROJECT_ROLES.filter(([role]) =>
    ["project", "role_project"].includes(effective(role).harness_prompt.source),
  ).map(([, label]) => label);
  rows.push(["Harness", harness.length ? `set in this project for ${harness.join(", ")}` : "from Settings or the Agents page"]);
  return rows;
}

function ExecutionCard({ project, onOpen }: { project: Project; onOpen: () => void }) {
  const agents = useLoad(() => fetchProjectAgentSettings(project.project_id), project.project_id);
  const rows = [...(agents?.data ? agentRows(agents.data) : []), ...executionRows(project)];
  return (
    <OverviewCard title="Execution" action={<CardLink onClick={onOpen}>Change in Settings</CardLink>}>
      {agents?.error && <p className="text-xs text-destructive">{agents.error}</p>}
      {rows.length === 0 ? (
        <p className="text-xs text-muted-foreground">
          Nothing set: the Planner, Workers and Reviewers run with the agents&apos; and Settings&apos; defaults.
        </p>
      ) : (
        <dl className="grid grid-cols-[auto_minmax(0,1fr)] gap-x-4 gap-y-1.5 text-xs">
          {rows.map(([label, value]) => (
            <div key={label} className="contents">
              <dt className="text-muted-foreground">{label}</dt>
              <dd className="break-words font-medium">{value}</dd>
            </div>
          ))}
        </dl>
      )}
    </OverviewCard>
  );
}

function Overview({ project, onTab }: { project: Project; onTab: (tab: Tab) => void }) {
  return (
    <div className="grid gap-4 lg:grid-cols-2">
      <TasksCard project={project} />
      <OverviewCard title="Background" action={<CardLink onClick={() => onTab("settings")}>Edit in Settings</CardLink>}>
        {project.background ? (
          <ClampedText
            key={project.background}
            text={project.background}
            className="font-mono text-xs leading-relaxed text-foreground/80"
          />
        ) : (
          <p className="text-xs text-muted-foreground">
            No background. It is what every agent on this project should know.
          </p>
        )}
      </OverviewCard>
      <KnowledgeCard project={project} onOpen={() => onTab("knowledge")} />
      <SkillsCard project={project} onOpen={() => onTab("skills")} />
      <ExecutionCard project={project} onOpen={() => onTab("settings")} />
    </div>
  );
}

interface ProjectPageProps {
  /** "<project id>", "<project id>/<tab>", or "<project id>/knowledge/<draft id>" to open one draft. */
  sub: string;
  onNavigate: (sub: string) => void;
}

/** One project: an overview, its knowledge, its skills and its settings, each a tab with its own address. */
export function ProjectPage({ sub, onNavigate }: ProjectPageProps) {
  const [projectId, tabPart, focus] = sub.split("/");
  const tab: Tab = TABS.some((t) => t.id === tabPart) ? (tabPart as Tab) : "overview";
  const { projects, error, reload } = useProjects();
  const project = projects?.find((p) => p.project_id === projectId) ?? null;
  // A project the list does not hold yet (just created) is reported missing only after a fresh load confirms it.
  const [confirmedMissing, setConfirmedMissing] = useState("");
  const unknown = !!projects && !project;
  useEffect(() => {
    if (!unknown) return;
    let alive = true;
    reload().then(() => alive && setConfirmedMissing(projectId));
    return () => {
      alive = false;
    };
  }, [unknown, projectId, reload]);

  const openTab = (next: Tab) => onNavigate(next === "overview" ? projectId : `${projectId}/${next}`);

  if (!project) {
    return (
      <div className="p-6">
        {unknown && confirmedMissing === projectId ? (
          <div className="flex flex-wrap items-center justify-between gap-4 rounded-lg border border-border bg-card px-5 py-4 text-sm text-muted-foreground">
            <span>
              Project <span className="font-mono">{projectId}</span> does not exist.
            </span>
            <Button type="button" variant="outline" size="sm" onClick={() => (window.location.hash = "tasks")}>
              Show all tasks
            </Button>
          </div>
        ) : error && !projects ? (
          <p role="alert" className="text-sm text-destructive">{error}</p>
        ) : (
          <Loader2 className="h-6 w-6 animate-spin text-muted-foreground" aria-label="Loading" />
        )}
      </div>
    );
  }

  return (
    <div className="mx-auto max-w-5xl space-y-5 p-6">
      <header className="space-y-1.5">
        <h1 className="flex items-center gap-2 text-2xl font-bold">
          <Folder className="h-5 w-5 shrink-0 text-muted-foreground" aria-hidden="true" />
          <span className="min-w-0 break-words">{project.name}</span>
        </h1>
        {project.description && (
          <p className="whitespace-pre-wrap text-sm text-muted-foreground">{project.description}</p>
        )}
        <p className="flex flex-wrap items-center gap-x-2 gap-y-1 text-xs text-muted-foreground">
          {project.work_dir ? (
            <span className="break-all font-mono text-foreground/80" title="Default directory">{project.work_dir}</span>
          ) : (
            <span>No default directory</span>
          )}
          <span aria-hidden="true">·</span>
          <a href={tasksHref(project.project_id)} className="underline-offset-2 hover:text-foreground hover:underline">
            {project.task_count} {project.task_count === 1 ? "task" : "tasks"}
            {project.active_count > 0 && `, ${project.active_count} active`}
          </a>
        </p>
      </header>

      <div role="tablist" aria-label="Project" className="flex gap-1 overflow-x-auto border-b border-border">
        {TABS.map((t) => (
          <button
            key={t.id}
            type="button"
            role="tab"
            id={`project-tab-${t.id}`}
            aria-selected={tab === t.id}
            aria-controls="project-tabpanel"
            tabIndex={tab === t.id ? 0 : -1}
            onClick={() => openTab(t.id)}
            onKeyDown={(e) => {
              if (e.key !== "ArrowRight" && e.key !== "ArrowLeft") return;
              const i = TABS.findIndex((x) => x.id === tab);
              const next = TABS[(i + (e.key === "ArrowRight" ? 1 : TABS.length - 1)) % TABS.length].id;
              openTab(next);
              document.getElementById(`project-tab-${next}`)?.focus();
            }}
            className={cn(
              "-mb-px whitespace-nowrap border-b-2 px-3 py-2 text-sm font-medium transition-colors focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring",
              tab === t.id
                ? "border-primary text-foreground"
                : "border-transparent text-muted-foreground hover:text-foreground",
            )}
          >
            {t.label}
          </button>
        ))}
      </div>

      <div id="project-tabpanel" role="tabpanel" aria-labelledby={`project-tab-${tab}`}>
        {tab === "overview" && <Overview project={project} onTab={openTab} />}
        {tab === "knowledge" && <ProjectKnowledgeSection projectId={project.project_id} openDraftId={focus} />}
        {tab === "skills" && <ProjectSkillsSection projectId={project.project_id} />}
        {tab === "settings" && (
          <ProjectSettingsForm
            key={project.project_id}
            project={project}
            onDeleted={() => (window.location.hash = "tasks")}
          />
        )}
      </div>
    </div>
  );
}
