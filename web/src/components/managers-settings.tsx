import { useCallback, useEffect, useId, useRef, useState } from "react";
import { Loader2, Plus, Trash2 } from "lucide-react";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Switch } from "@/components/ui/switch";
import {
  EDIT_BOX,
  EditDelete,
  FormFooter,
  LABEL,
  RegistryList,
  Row,
  RowName,
  SELECT,
} from "@/components/registry-ui";
import {
  createManager,
  deleteManager,
  deleteManagerTemplate,
  fetchManagers,
  fetchProjects,
  fetchRoleTemplates,
  putManagerTemplate,
  updateManager,
  type ManagerFields,
  type ManagerInfo,
  type ManagerTools,
  type Project,
  type TaskPolicy,
  type TaskScope,
  type TaskTemplate,
  type TaskTemplateFields,
  type TemplateInput,
} from "@/lib/api";
import { ManagerChatBindings, useManagerChatBindings } from "@/components/manager-chat-bindings";
import { ManagerCleanup } from "@/components/manager-cleanup";
import { ManagerInstalls } from "@/components/manager-installs";
import type { Binding } from "@/lib/chat-bindings-api";
import { MANAGER_EXAMPLE } from "@/lib/manager-example";
import { settingsHref } from "@/lib/settings-pages";
import { errorText, useAction } from "@/lib/use-action";
import { profileLabel, useRegistry } from "@/lib/use-registry";
import { formatUsd } from "@/lib/usage";

const TEXTAREA =
  "w-full rounded-md border border-input bg-transparent p-2 text-sm text-foreground focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring";
const POLL_MS = 5000;
const MAX_INPUTS = 10;

type Reload = () => Promise<void>;

// --- Manager form ---

interface ManagerDraft {
  key: string;
  name: string;
  description: string;
  system_prompt: string;
  profile_id: string | null;
  tools: ManagerTools;
  task_scope: TaskScope;
  task_policy: TaskPolicy;
  idle_timeout_seconds: string;
  enabled: boolean;
}

function draftOf(manager: ManagerInfo | null): ManagerDraft {
  return {
    key: "",
    name: manager?.name ?? "",
    description: manager?.description ?? "",
    system_prompt: manager?.system_prompt ?? "",
    profile_id: manager?.profile_id ?? null,
    tools: manager?.tools ?? "tasks",
    task_scope: manager?.task_scope ?? "own",
    task_policy: manager?.task_policy ?? "free",
    idle_timeout_seconds: String(manager?.idle_timeout_seconds ?? 1800),
    enabled: manager?.enabled ?? true,
  };
}

function exampleDraft(): ManagerDraft {
  const { manager } = MANAGER_EXAMPLE;
  return {
    ...draftOf(null), ...manager,
    tools: manager.tools as ManagerTools,
    task_scope: manager.task_scope as TaskScope,
    task_policy: manager.task_policy as TaskPolicy,
  };
}

function ManagerForm({
  manager,
  initial,
  reload,
  onDone,
}: {
  manager: ManagerInfo | null;
  initial?: ManagerDraft;
  reload: Reload;
  onDone: () => void;
}) {
  const id = useId();
  const { registry } = useRegistry();
  const [draft, setDraft] = useState<ManagerDraft>(initial ?? draftOf(manager));
  const [keyError, setKeyError] = useState("");
  const action = useAction();
  const fixed = manager?.builtin ?? false;
  const set = (patch: Partial<ManagerDraft>) => setDraft((d) => ({ ...d, ...patch }));
  const profiles = (registry?.profiles ?? []).filter((p) => p.roles.includes("manager"));

  const save = async (e?: React.FormEvent) => {
    e?.preventDefault();
    setKeyError("");
    const name = draft.name.trim();
    const description = draft.description.trim();
    if (manager && !draft.enabled && manager.enabled && manager.state.running
        && !confirm("Disabling stops this Manager now, also a reply under way.")) return;
    let keyRejected = false;
    const ok = await action.run(async () => {
      try {
        const idleTimeout = Number(draft.idle_timeout_seconds);
        if (!fixed && (!draft.idle_timeout_seconds.trim() || !Number.isInteger(idleTimeout)
            || (idleTimeout !== 0 && (idleTimeout < 60 || idleTimeout > 86400)))) {
          throw new Error("Idle timeout must be 0 (off) or a whole number from 60 to 86400 seconds.");
        }
        if (!manager) {
          const { key, ...rest } = draft;
          await createManager({ key: key.trim(), ...rest, name, description, idle_timeout_seconds: idleTimeout });
        } else if (fixed) {
          await updateManager(manager.id, { name, description });
        } else {
          const changes: ManagerFields = {};
          if (name !== manager.name) changes.name = name;
          if (description !== manager.description) changes.description = description;
          if (draft.system_prompt !== (manager.system_prompt ?? "")) changes.system_prompt = draft.system_prompt;
          if (draft.profile_id !== manager.profile_id) changes.profile_id = draft.profile_id;
          if (draft.tools !== manager.tools) changes.tools = draft.tools;
          if (draft.task_scope !== manager.task_scope) changes.task_scope = draft.task_scope;
          if (draft.task_policy !== manager.task_policy) changes.task_policy = draft.task_policy;
          if (idleTimeout !== manager.idle_timeout_seconds) changes.idle_timeout_seconds = idleTimeout;
          if (draft.enabled !== manager.enabled) changes.enabled = draft.enabled;
          await updateManager(manager.id, changes);
        }
      } catch (err) {
        // The Switch returns to off when the enabled limit refused it.
        if (draft.enabled && /enabled/i.test(errorText(err))) set({ enabled: false });
        if (!manager && /deleted manager/i.test(errorText(err))) {
          setKeyError(errorText(err));
          keyRejected = true;
          return;
        }
        throw err;
      }
    });
    if (ok && !keyRejected) {
      await reload();
      onDone();
    }
  };

  return (
    <form onSubmit={save} className="space-y-3">
      <div className="grid gap-3 sm:grid-cols-2">
        {!manager && (
          <div className="space-y-1 sm:col-span-2">
            <label htmlFor={`${id}-key`} className={LABEL}>Key *</label>
            <Input id={`${id}-key`} value={draft.key} onChange={(e) => set({ key: e.target.value })} spellCheck={false}
                   autoCapitalize="none" className="font-mono" aria-invalid={!!keyError} />
            <p className="text-[11px] text-muted-foreground">
              lowercase letters, digits and -; the id becomes manager-{draft.key || "<key>"}
            </p>
            {keyError && <p role="alert" className="text-xs text-destructive">{keyError}</p>}
          </div>
        )}
        <div className="space-y-1">
          <label htmlFor={`${id}-name`} className={LABEL}>Name *</label>
          <Input id={`${id}-name`} value={draft.name} onChange={(e) => set({ name: e.target.value })} />
        </div>
        <div className="space-y-1">
          <label htmlFor={`${id}-description`} className={LABEL}>Description</label>
          <Input id={`${id}-description`} value={draft.description} onChange={(e) => set({ description: e.target.value })} />
        </div>
      </div>
      {fixed ? (
        <p className="text-xs text-muted-foreground">
          <a href={settingsHref("roles")} className="underline">Prompt and Agent Profile: Settings → Roles</a>
        </p>
      ) : (
        <>
          <div className="space-y-1">
            <label htmlFor={`${id}-prompt`} className={LABEL}>Responsibility prompt *</label>
            <textarea id={`${id}-prompt`} rows={5} value={draft.system_prompt} className={TEXTAREA}
                      onChange={(e) => set({ system_prompt: e.target.value })} />
          </div>
          <div className="grid gap-3 sm:grid-cols-2">
            <div className="space-y-1">
              <label htmlFor={`${id}-profile`} className={LABEL}>Agent Profile</label>
              <select id={`${id}-profile`} value={draft.profile_id ?? ""} className={SELECT}
                      onChange={(e) => set({ profile_id: e.target.value || null })}>
                <option value="">Follow the Manager role</option>
                {profiles.map((p) => <option key={p.profile_id} value={p.profile_id}>{profileLabel(p)}</option>)}
              </select>
            </div>
            <div className="space-y-1">
              <label htmlFor={`${id}-tools`} className={LABEL}>Tools</label>
              <select id={`${id}-tools`} value={draft.tools} className={SELECT}
                      onChange={(e) => set({ tools: e.target.value as ManagerTools })}>
                <option value="tasks">Tasks only</option>
                <option value="all">All tools</option>
              </select>
              <p className="text-[11px] text-muted-foreground">
                {draft.tools === "tasks"
                  ? "Tasks only: create tasks, answer about them, and nothing that changes projects, settings or files."
                  : "All tools: also manages projects, schedules and settings, as the default Manager does."}
              </p>
            </div>
            <div className="space-y-1">
              <label htmlFor={`${id}-scope`} className={LABEL}>Task visibility</label>
              <select id={`${id}-scope`} value={draft.task_scope} className={SELECT}
                      onChange={(e) => set({ task_scope: e.target.value as TaskScope })}>
                <option value="own">Own tasks</option>
                <option value="all">Every task</option>
              </select>
              <p className="text-[11px] text-muted-foreground">
                {draft.task_scope === "own"
                  ? "Only the tasks this Manager created: its task tools, overview, spend and task counts see nothing else."
                  : "Every task, also those other Managers created."}
                {" "}A change applies at the next tool call, also on a running Manager.
              </p>
            </div>
            <div className="space-y-1">
              <label htmlFor={`${id}-policy`} className={LABEL}>Task creation</label>
              <select id={`${id}-policy`} value={draft.task_policy} className={SELECT}
                      onChange={(e) => set({ task_policy: e.target.value as TaskPolicy })}>
                <option value="free">Free</option>
                <option value="templates_only">Templates only</option>
              </select>
              <p className="text-[11px] text-muted-foreground">
                {draft.task_policy === "free"
                  ? "Free: any task, with or without a template."
                  : "Templates only: every task comes from one of this Manager's task templates."}
              </p>
            </div>
            <div className="space-y-1">
              <label htmlFor={`${id}-idle`} className={LABEL}>Idle timeout (seconds)</label>
              <Input id={`${id}-idle`} type="number" min={0} max={86400} step={1} required
                     value={draft.idle_timeout_seconds} aria-describedby={`${id}-idle-help`}
                     onChange={(e) => set({ idle_timeout_seconds: e.target.value })} />
              <p id={`${id}-idle-help`} className="text-[11px] text-muted-foreground">
                0 = off; otherwise 60–86400 seconds. Idle processes sleep and resume the same conversation on the next message.
              </p>
            </div>
          </div>
          <label className="flex items-center gap-2 text-sm">
            <Switch checked={draft.enabled} onChange={(enabled) => set({ enabled })} label="Enabled" />
            Enabled
          </label>
        </>
      )}
      <FormFooter error={action.error} busy={action.busy} submit="Save" onCancel={onDone} />
    </form>
  );
}

// --- Template form ---

interface TemplateDraft {
  key: string;
  name: string;
  description: string;
  project_id: string;
  work_dir: string;
  workspace_mode: "" | "worktree" | "in_place";
  requirement: string;
  planner_instructions: string;
  profiles: { planner: string; worker: string; reviewer: string };
  role_templates: string[];
  inputs: TemplateInput[];
}

type ExampleTemplate = (typeof MANAGER_EXAMPLE.templates)[keyof typeof MANAGER_EXAMPLE.templates];

function templateDraft(key: string, t: TaskTemplate | ExampleTemplate | null): TemplateDraft {
  const full = t as Partial<TaskTemplate> | null;
  return {
    key,
    name: t?.name ?? "",
    description: t?.description ?? "",
    project_id: full?.project_id ?? "",
    work_dir: full?.work_dir ?? "",
    workspace_mode: full?.workspace_mode ?? "",
    requirement: t?.requirement ?? "",
    planner_instructions: t?.planner_instructions ?? "",
    profiles: {
      planner: full?.profiles?.planner ?? "",
      worker: full?.profiles?.worker ?? "",
      reviewer: full?.profiles?.reviewer ?? "",
    },
    role_templates: full?.role_templates ?? [],
    inputs: (t?.inputs ?? []).map((i) => {
      const input = i as Partial<TemplateInput> & { key: string };
      return {
        key: input.key, label: input.label ?? "", kind: input.kind ?? "text",
        required: input.required ?? true, pattern: input.pattern ?? "", hint: input.hint ?? "",
      };
    }),
  };
}

const ROLES = ["planner", "worker", "reviewer"] as const;

function TemplateForm({
  manager,
  template,
  reload,
  onDone,
}: {
  manager: ManagerInfo;
  template: TaskTemplate | null;
  reload: Reload;
  onDone: () => void;
}) {
  const id = useId();
  const { registry } = useRegistry();
  const [draft, setDraft] = useState<TemplateDraft>(templateDraft(template?.key ?? "", template));
  const [projects, setProjects] = useState<Project[]>([]);
  const [roleOptions, setRoleOptions] = useState<string[]>([]);
  const action = useAction();
  const set = (patch: Partial<TemplateDraft>) => setDraft((d) => ({ ...d, ...patch }));
  const setInput = (index: number, patch: Partial<TemplateInput>) =>
    set({ inputs: draft.inputs.map((input, i) => (i === index ? { ...input, ...patch } : input)) });

  useEffect(() => {
    let alive = true;
    fetchProjects().then((list) => { if (alive) setProjects(list); }).catch(() => {});
    return () => { alive = false; };
  }, []);

  useEffect(() => {
    if (!draft.project_id) {
      setRoleOptions([]);
      return;
    }
    let alive = true;
    fetchRoleTemplates(draft.project_id)
      .then((r) => { if (alive) setRoleOptions(r.templates.map((t) => t.key)); })
      .catch(() => { if (alive) setRoleOptions([]); });
    return () => { alive = false; };
  }, [draft.project_id]);

  const fillExample = (key: string, example: ExampleTemplate) => setDraft(templateDraft(key, example));

  const save = async (e?: React.FormEvent) => {
    e?.preventDefault();
    const profiles: TaskTemplateFields["profiles"] = {};
    for (const role of ROLES) if (draft.profiles[role]) profiles[role] = draft.profiles[role];
    const body: TaskTemplateFields = {
      name: draft.name.trim(),
      description: draft.description.trim(),
      project_id: draft.project_id || null,
      work_dir: draft.work_dir.trim(),
      workspace_mode: draft.workspace_mode || null,
      requirement: draft.requirement,
      planner_instructions: draft.planner_instructions,
      profiles,
      role_templates: draft.project_id ? draft.role_templates : [],
      inputs: draft.inputs,
    };
    const ok = await action.run(() => putManagerTemplate(manager.id, draft.key.trim(), body));
    if (ok) {
      await reload();
      onDone();
    }
  };

  const examples = Object.entries(MANAGER_EXAMPLE.templates);
  return (
    <form onSubmit={save} className="space-y-3">
      {!template && manager.templates.length === 0 && (
        <div className="flex flex-wrap gap-2">
          {examples.map(([key, example]) => (
            <Button key={key} type="button" size="sm" variant="outline" onClick={() => fillExample(key, example)}>
              Start from example: {example.name}
            </Button>
          ))}
        </div>
      )}
      <div className="grid gap-3 sm:grid-cols-2">
        {!template && (
          <div className="space-y-1">
            <label htmlFor={`${id}-key`} className={LABEL}>Key *</label>
            <Input id={`${id}-key`} value={draft.key} onChange={(e) => set({ key: e.target.value })} spellCheck={false}
                   className="font-mono" />
          </div>
        )}
        <div className="space-y-1">
          <label htmlFor={`${id}-name`} className={LABEL}>Name *</label>
          <Input id={`${id}-name`} value={draft.name} onChange={(e) => set({ name: e.target.value })} />
        </div>
        <div className="space-y-1 sm:col-span-2">
          <label htmlFor={`${id}-description`} className={LABEL}>Description</label>
          <Input id={`${id}-description`} value={draft.description} onChange={(e) => set({ description: e.target.value })} />
        </div>
        <div className="space-y-1">
          <label htmlFor={`${id}-project`} className={LABEL}>Project</label>
          <select id={`${id}-project`} value={draft.project_id} className={SELECT}
                  onChange={(e) => set({ project_id: e.target.value, role_templates: [] })}>
            <option value="">Any project</option>
            {projects.map((p) => <option key={p.project_id} value={p.project_id}>{p.name}</option>)}
          </select>
        </div>
        <div className="space-y-1">
          <label htmlFor={`${id}-workdir`} className={LABEL}>Work directory</label>
          <Input id={`${id}-workdir`} value={draft.work_dir} onChange={(e) => set({ work_dir: e.target.value })}
                 spellCheck={false} className="font-mono" />
        </div>
        <div className="space-y-1">
          <label htmlFor={`${id}-mode`} className={LABEL}>Run mode</label>
          <select id={`${id}-mode`} value={draft.workspace_mode} className={SELECT}
                  onChange={(e) => set({ workspace_mode: e.target.value as TemplateDraft["workspace_mode"] })}>
            <option value="">Default</option>
            <option value="worktree">Worktree</option>
            <option value="in_place">In place</option>
          </select>
        </div>
      </div>
      <div className="space-y-1">
        <label htmlFor={`${id}-requirement`} className={LABEL}>Requirement *</label>
        <textarea id={`${id}-requirement`} rows={3} value={draft.requirement} className={TEXTAREA}
                  onChange={(e) => set({ requirement: e.target.value })} />
        <p className="text-[11px] text-muted-foreground">Use {"{{input_key}}"} for an input</p>
      </div>
      <div className="space-y-1">
        <label htmlFor={`${id}-instructions`} className={LABEL}>Planner instructions</label>
        <textarea id={`${id}-instructions`} rows={3} value={draft.planner_instructions} className={TEXTAREA}
                  onChange={(e) => set({ planner_instructions: e.target.value })} />
      </div>
      <div className="grid gap-3 sm:grid-cols-3">
        {ROLES.map((role) => (
          <div key={role} className="space-y-1">
            <label htmlFor={`${id}-profile-${role}`} className={LABEL}>
              {role[0].toUpperCase() + role.slice(1)} Agent Profile
            </label>
            <select id={`${id}-profile-${role}`} value={draft.profiles[role]} className={SELECT}
                    onChange={(e) => set({ profiles: { ...draft.profiles, [role]: e.target.value } })}>
              <option value="">Settings</option>
              {(registry?.profiles ?? []).filter((p) => p.roles.includes(role)).map((p) => (
                <option key={p.profile_id} value={p.profile_id}>{profileLabel(p)}</option>
              ))}
            </select>
          </div>
        ))}
      </div>
      <div className="space-y-1">
        <label htmlFor={`${id}-roletemplates`} className={LABEL}>Role templates</label>
        <select id={`${id}-roletemplates`} multiple value={draft.role_templates} className={`${SELECT} h-24`}
                disabled={!draft.project_id}
                onChange={(e) => set({ role_templates: Array.from(e.target.selectedOptions, (o) => o.value) })}>
          {roleOptions.map((key) => <option key={key} value={key}>{key}</option>)}
        </select>
        {!draft.project_id && <p className="text-[11px] text-muted-foreground">Choose a project first.</p>}
      </div>
      <div className="space-y-2">
        <div className="flex items-center gap-2">
          <span className={LABEL}>Inputs</span>
          <Button type="button" size="sm" variant="outline" disabled={draft.inputs.length >= MAX_INPUTS}
                  onClick={() => set({ inputs: [...draft.inputs, { key: "", label: "", kind: "text", required: true, pattern: "", hint: "" }] })}>
            <Plus className="h-3.5 w-3.5" /> Add input
          </Button>
        </div>
        {draft.inputs.map((input, index) => (
          <div key={index} role="group" aria-label={`Input ${index + 1}`}
               className="grid items-center gap-2 sm:grid-cols-[1fr_1fr_6rem_auto_1fr_1fr_auto]">
            <Input aria-label="Input key" placeholder="key" value={input.key} className="font-mono"
                   onChange={(e) => setInput(index, { key: e.target.value })} />
            <Input aria-label="Input label" placeholder="label" value={input.label}
                   onChange={(e) => setInput(index, { label: e.target.value })} />
            <select aria-label="Input kind" value={input.kind} className={SELECT}
                    onChange={(e) => setInput(index, { kind: e.target.value as TemplateInput["kind"] })}>
              <option value="text">text</option>
              <option value="url">url</option>
            </select>
            <label className="flex items-center gap-1 text-xs">
              <input type="checkbox" aria-label="Input required" checked={input.required}
                     onChange={(e) => setInput(index, { required: e.target.checked })} />
              required
            </label>
            <Input aria-label="Input pattern" placeholder="pattern" value={input.pattern} className="font-mono"
                   onChange={(e) => setInput(index, { pattern: e.target.value })} />
            <Input aria-label="Input hint" placeholder="hint" value={input.hint}
                   onChange={(e) => setInput(index, { hint: e.target.value })} />
            <Button type="button" variant="ghost" size="sm" aria-label={`Remove input ${index + 1}`}
                    onClick={() => set({ inputs: draft.inputs.filter((_, i) => i !== index) })}>
              <Trash2 className="h-3.5 w-3.5" />
            </Button>
          </div>
        ))}
      </div>
      <FormFooter error={action.error} busy={action.busy} submit="Save template" onCancel={onDone} />
    </form>
  );
}

function TemplateRow({ manager, template, reload }: { manager: ManagerInfo; template: TaskTemplate; reload: Reload }) {
  const [editing, setEditing] = useState(false);
  const action = useAction();
  const remove = () => {
    if (confirm(`Delete template ${template.name}?`)) {
      action.run(async () => {
        await deleteManagerTemplate(manager.id, template.key);
        await reload();
      });
    }
  };
  const project = template.project_id;
  return (
    <Row
      title={<RowName name={template.name} builtin={false} kind={template.key} />}
      actions={
        <EditDelete what="template" name={template.name} editing={editing} busy={action.busy}
                    onEdit={() => setEditing(!editing)} onDelete={remove} />
      }
      below={
        <>
          {action.error && <p role="alert" className="text-xs text-destructive">{action.error}</p>}
          {editing && (
            <div className={EDIT_BOX}>
              <TemplateForm manager={manager} template={template} reload={reload} onDone={() => setEditing(false)} />
            </div>
          )}
        </>
      }
    >
      <p className="text-xs text-muted-foreground">
        {project ? <ProjectName id={project} /> : "any project"} · {template.inputs.length}{" "}
        {template.inputs.length === 1 ? "input" : "inputs"}
      </p>
    </Row>
  );
}

function ProjectName({ id }: { id: string }) {
  const [name, setName] = useState(id);
  useEffect(() => {
    let alive = true;
    fetchProjects().then((list) => {
      const found = list.find((p) => p.project_id === id);
      if (alive && found) setName(found.name);
    }).catch(() => {});
    return () => { alive = false; };
  }, [id]);
  return <>{name}</>;
}

// --- The list ---

function ManagerRow({ manager, managers, bindings, snapshot, reload }: {
  manager: ManagerInfo; managers: ManagerInfo[]; bindings: Binding[];
  snapshot: Omit<ReturnType<typeof useManagerChatBindings>, "bindings">; reload: Reload;
}) {
  const [editing, setEditing] = useState(false);
  const action = useAction();
  const remove = () => {
    if (confirm(`Delete ${manager.name}? Its conversations stay stored but are no longer listed under any Manager, and its key cannot be used again.`)) {
      action.run(async () => {
        await deleteManager(manager.id);
        await reload();
      });
    }
  };
  const pending = manager.state.sync === "pending";
  const allTools = manager.builtin || manager.tools === "all";
  return (
    <Row
      title={
        <>
          <RowName name={manager.name} builtin={manager.builtin} />
          <Badge variant="outline">{allTools ? "all tools" : "tasks only"}</Badge>
          <Badge variant="outline">{manager.builtin || manager.task_scope === "all" ? "every task" : "own tasks"}</Badge>
          {manager.task_policy === "templates_only" && <Badge variant="outline">templates only</Badge>}
          {pending && (
            <Badge variant="warning" title={manager.state.sync_error}>Not in force yet</Badge>
          )}
          {!manager.enabled && <Badge variant="outline">Off</Badge>}
        </>
      }
      actions={
        manager.builtin ? (
          <Button type="button" variant="ghost" size="sm" onClick={() => setEditing(!editing)} aria-expanded={editing}
                  aria-label={`Edit Manager ${manager.name}`}>
            Edit
          </Button>
        ) : (
          <EditDelete what="Manager" name={manager.name} editing={editing} busy={action.busy}
                      onEdit={() => setEditing(!editing)} onDelete={remove} />
        )
      }
      below={
        <>
          {action.error && <p role="alert" className="text-xs text-destructive">{action.error}</p>}
          {editing && (
            <div className={EDIT_BOX}>
              <ManagerForm manager={manager} reload={reload} onDone={() => setEditing(false)} />
            </div>
          )}
          {!manager.builtin && (
            <RegistryList
              title="Task templates"
              hint="What this Manager's tasks run with."
              addLabel="Add template"
              form={(close) => <TemplateForm manager={manager} template={null} reload={reload} onDone={close} />}
            >
              {manager.templates.map((t) => <TemplateRow key={t.key} manager={manager} template={t} reload={reload} />)}
            </RegistryList>
          )}
          <ManagerChatBindings manager={manager} managers={managers} bindings={bindings} snapshot={snapshot} reload={reload} />
          <ManagerInstalls manager={manager} hasScenarios={managers.some((item) => !item.builtin)} />
        </>
      }
    >
      {manager.description && <p className="break-words text-xs text-muted-foreground">{manager.description}</p>}
      {pending && (
        <p className="text-xs text-warning">
          Not in force yet{manager.state.sync_error ? `: ${manager.state.sync_error}` : ""}; retrying
        </p>
      )}
      <p className="text-xs text-muted-foreground">
        {manager.templates.length} {manager.templates.length === 1 ? "template" : "templates"} · today{" "}
        {formatUsd(manager.spend.today_usd)}
      </p>
    </Row>
  );
}

/** Settings → Managers: each Manager's duty, tools, task visibility and task templates. */
export function ManagersSection() {
  const [managers, setManagers] = useState<ManagerInfo[] | null>(null);
  const [error, setError] = useState("");
  const [example, setExample] = useState(0);
  const snapshot = useManagerChatBindings();
  const { bindings: chatBindings, ...chatState } = snapshot;
  const { refresh: refreshChats } = chatState;
  const managerVersion = useRef(0);
  const load = useCallback(async () => {
    const version = ++managerVersion.current;
    const definitions = fetchManagers().then((list) => {
      if (version === managerVersion.current) { setManagers(list); setError(""); }
    }).catch((err: unknown) => {
      if (version === managerVersion.current) setError(errorText(err));
    });
    await Promise.all([definitions, refreshChats()]);
  }, [refreshChats]);

  useEffect(() => {
    void load();
    return () => { managerVersion.current++; };
  }, [load]);

  const anyPending = !!managers?.some((m) => m.state.sync === "pending");
  useEffect(() => {
    if (!anyPending) return;
    const timer = setInterval(() => void load(), POLL_MS);
    return () => clearInterval(timer);
  }, [anyPending, load]);

  if (!managers) {
    return error ? (
      <div className="space-y-3">
        <p role="alert" className="break-words text-sm text-destructive">{error}</p>
        <Button variant="outline" onClick={() => void load()}>Retry</Button>
      </div>
    ) : (
      <div className="flex h-32 items-center justify-center">
        <Loader2 className="h-6 w-6 animate-spin text-muted-foreground" aria-label="Loading" />
      </div>
    );
  }
  const grouped = new Map<string, Binding[]>();
  for (const binding of chatBindings ?? []) {
    const group = grouped.get(binding.manager_id) ?? [];
    group.push(binding);
    grouped.set(binding.manager_id, group);
  }
  return (
    <div className="space-y-2">
      <div className="flex flex-wrap items-center gap-2">
        <Button size="sm" variant="outline" disabled={snapshot.busy} onClick={() => void snapshot.refresh()}>
          {snapshot.error ? "Retry chats" : "Refresh chats"}
        </Button>
        {snapshot.loading && <span role="status" className="text-xs text-muted-foreground">Loading chats…</span>}
        {snapshot.error && <p role="alert" className="break-words text-xs text-destructive">{snapshot.error}</p>}
      </div>
      <RegistryList
        title="Managers"
        hint="Each Manager has its own prompt, tools and task visibility."
        addLabel="Add Manager"
        form={(close) => <ManagerForm manager={null} reload={load} onDone={close} />}
      >
        {managers.map((m) => <ManagerRow key={m.id} manager={m} managers={managers}
          bindings={grouped.get(m.id) ?? []} snapshot={chatState} reload={load} />)}
      </RegistryList>
      <ExampleManager reload={load} key={`example-${example}`} onDone={() => setExample((n) => n + 1)} />
      <ManagerCleanup />
      {error && <p role="alert" className="text-xs text-destructive">{error}</p>}
    </div>
  );
}

/** "New from example": the create form filled from the GitHub example; nothing is saved until Save. */
function ExampleManager({ reload, onDone }: { reload: Reload; onDone: () => void }) {
  const [open, setOpen] = useState(false);
  return (
    <div>
      <Button type="button" size="sm" variant="outline" onClick={() => setOpen(!open)} aria-expanded={open}>
        <Plus className="h-3.5 w-3.5" /> New from example
      </Button>
      {open && (
        <div className={EDIT_BOX}>
          <ManagerForm manager={null} initial={exampleDraft()} reload={reload} onDone={() => { setOpen(false); onDone(); }} />
        </div>
      )}
    </div>
  );
}
