import { useEffect, useId, useRef, useState } from "react";
import { Pencil } from "lucide-react";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { EDIT_BOX, EditDelete, FormFooter, LABEL, RegistryList, Row, SELECT } from "@/components/registry-ui";
import {
  deleteRoleTemplate,
  editTaskRole,
  fetchAllowedProfiles,
  fetchRoleTemplates,
  putRoleTemplate,
  type AllowedProfile,
  type PlannerRole,
  type RoleTemplate,
  type RoleTemplates,
} from "@/lib/api";
import { AGENT_PROFILE } from "@/lib/settings-pages";

/** Editing roles: a task's role on the task page (a new version by the user), and a project's role templates. */

type Kind = PlannerRole["kind"];

const errorText = (err: unknown) => (err instanceof Error ? err.message : String(err));
const TEXTAREA =
  "w-full rounded-md border border-input bg-transparent px-3 py-2 font-mono text-xs text-foreground shadow-sm focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring";
const builtinName = (kind: Kind) => (kind === "work" ? "Worker" : "Reviewer");

/** The profiles of the project's list that can run a role of `kind`; null while they load. */
function useAllowedProfiles(projectId: string | null, kind: Kind) {
  const [profiles, setProfiles] = useState<AllowedProfile[] | null>(projectId ? null : []);
  useEffect(() => {
    if (!projectId) return;
    let alive = true;
    fetchAllowedProfiles(projectId)
      .then((list) => alive && setProfiles(list.profiles))
      .catch(() => alive && setProfiles([]));
    return () => {
      alive = false;
    };
  }, [projectId]);
  const agent = kind === "work" ? "worker" : "reviewer";
  return profiles?.filter((p) => p.roles.includes(agent)) ?? null;
}

interface Definition {
  role_prompt: string;
  harness: string;
  profile_id: string;
}

/** A role's prompt, harness and profile; the profile from the project's list that can run its kind. */
function DefinitionFields({
  projectId,
  kind,
  draft,
  onChange,
  limit,
  autoFocus = false,
}: {
  projectId: string | null;
  kind: Kind;
  draft: Definition;
  onChange: (draft: Definition) => void;
  limit?: number;
  /** Focus the role prompt when the form opens. */
  autoFocus?: boolean;
}) {
  const id = useId();
  const profiles = useAllowedProfiles(projectId, kind);
  const set = (field: keyof Definition) => (e: { target: { value: string } }) =>
    onChange({ ...draft, [field]: e.target.value });
  // A profile the list no longer holds stays chosen until the user picks another, so saving says why.
  const listed = profiles?.some((p) => p.profile_id === draft.profile_id);
  return (
    <>
      <div className="space-y-1">
        <label htmlFor={`${id}-prompt`} className={LABEL}>
          Role prompt
        </label>
        <textarea
          id={`${id}-prompt`}
          value={draft.role_prompt}
          onChange={set("role_prompt")}
          rows={8}
          maxLength={limit}
          required
          autoFocus={autoFocus}
          className={TEXTAREA}
        />
      </div>
      <div className="space-y-1">
        <label htmlFor={`${id}-harness`} className={LABEL}>
          Harness
        </label>
        <textarea
          id={`${id}-harness`}
          value={draft.harness}
          onChange={set("harness")}
          rows={3}
          maxLength={limit}
          aria-describedby={`${id}-harness-help`}
          className={TEXTAREA}
        />
        <p id={`${id}-harness-help`} className="text-xs text-muted-foreground">
          Empty: the working style that applies. The boundaries always follow it.
        </p>
      </div>
      <div className="space-y-1">
        <label htmlFor={`${id}-profile`} className={LABEL}>
          {AGENT_PROFILE.name}
        </label>
        <select
          id={`${id}-profile`}
          value={draft.profile_id}
          onChange={set("profile_id")}
          disabled={profiles === null}
          aria-describedby={`${id}-profile-help`}
          className={SELECT}
        >
          <option value="">As the built-in {builtinName(kind)}</option>
          {draft.profile_id && profiles && !listed && (
            <option value={draft.profile_id}>Not on the project&apos;s list</option>
          )}
          {profiles?.map((p) => (
            <option key={p.profile_id} value={p.profile_id}>
              {p.name}
            </option>
          ))}
        </select>
        <p id={`${id}-profile-help`} className="text-xs text-muted-foreground">
          {profiles?.length === 0
            ? `The project's list of ${AGENT_PROFILE.plural} for roles is empty: it runs as the built-in ${builtinName(kind)}.`
            : `From the project's list (${AGENT_PROFILE.plural} tab).`}
        </p>
      </div>
    </>
  );
}

/** The user's edit of a task's role: its whole definition as a new version; the kind stays. */
export function TaskRoleEditor({
  taskId,
  projectId,
  role,
  onSaved,
}: {
  taskId: string;
  projectId: string | null;
  role: PlannerRole;
  onSaved: () => void;
}) {
  const [open, setOpen] = useState(false);
  const [draft, setDraft] = useState<Definition>({ role_prompt: "", harness: "", profile_id: "" });
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  // Closing the form gives the focus back to Edit.
  const editRef = useRef<HTMLButtonElement>(null);
  const wasOpen = useRef(false);
  useEffect(() => {
    if (!open && wasOpen.current) editRef.current?.focus();
    wasOpen.current = open;
  }, [open]);
  const start = () => {
    setDraft({ role_prompt: role.role_prompt, harness: role.harness ?? "", profile_id: role.profile?.profile_id ?? "" });
    setError("");
    setOpen(true);
  };
  const save = async (e: React.FormEvent) => {
    e.preventDefault();
    setBusy(true);
    try {
      await editTaskRole(taskId, role.id, {
        role_prompt: draft.role_prompt,
        harness: draft.harness.trim() ? draft.harness : null,
        profile_id: draft.profile_id || null,
      });
      setOpen(false);
      onSaved();
    } catch (err) {
      setError(errorText(err));
    } finally {
      setBusy(false);
    }
  };
  if (!open) {
    return (
      <Button ref={editRef} type="button" variant="outline" size="sm" onClick={start} aria-label={`Edit role ${role.id}`}>
        <Pencil className="h-3.5 w-3.5" /> Edit
      </Button>
    );
  }
  return (
    <form onSubmit={save} className={`${EDIT_BOX} space-y-3`} aria-label={`Edit role ${role.id}`}>
      <DefinitionFields projectId={projectId} kind={role.kind} draft={draft} onChange={setDraft} autoFocus />
      <p className="text-xs text-muted-foreground">
        Saving writes v{role.version + 1} by you. The steps that start from then on run it; the task&apos;s Planner is
        told, and cannot change this role afterwards.
      </p>
      <FormFooter error={error} busy={busy} submit={`Save v${role.version + 1}`} onCancel={() => setOpen(false)} />
    </form>
  );
}

// --- A project's role templates ---

const EMPTY_TEMPLATE = { key: "", kind: "work" as Kind, description: "", role_prompt: "", harness: "", profile_id: "" };
type TemplateDraft = typeof EMPTY_TEMPLATE;

function TemplateForm({
  projectId,
  template,
  limit,
  onSaved,
  onCancel,
}: {
  projectId: string;
  /** null: a new one. */
  template: RoleTemplate | null;
  limit: number;
  onSaved: (templates: RoleTemplates) => void;
  onCancel: () => void;
}) {
  const id = useId();
  const [draft, setDraft] = useState<TemplateDraft>(
    template
      ? { key: template.key, kind: template.kind, description: template.description, role_prompt: template.role_prompt,
          harness: template.harness ?? "", profile_id: template.profile?.profile_id ?? "" }
      : EMPTY_TEMPLATE,
  );
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const save = async (e: React.FormEvent) => {
    e.preventDefault();
    setBusy(true);
    try {
      onSaved(
        await putRoleTemplate(projectId, draft.key.trim(), {
          kind: draft.kind,
          description: draft.description,
          role_prompt: draft.role_prompt,
          harness: draft.harness.trim() ? draft.harness : null,
          profile_id: draft.profile_id || null,
        }),
      );
    } catch (err) {
      setError(errorText(err));
    } finally {
      setBusy(false);
    }
  };
  return (
    <form onSubmit={save} className="space-y-3" aria-label={template ? `Edit role template ${template.key}` : "New role template"}>
      <div className="grid gap-3 sm:grid-cols-2">
        <div className="space-y-1">
          <label htmlFor={`${id}-key`} className={LABEL}>
            Key
          </label>
          <Input
            id={`${id}-key`}
            value={draft.key}
            onChange={(e) => setDraft({ ...draft, key: e.target.value })}
            disabled={!!template}
            autoFocus={!template}
            required
            maxLength={32}
            placeholder="e.g. security-review"
            aria-describedby={`${id}-key-help`}
            className="font-mono"
          />
          <p id={`${id}-key-help`} className="text-xs text-muted-foreground">
            Letters, digits, &apos;-&apos;, &apos;_&apos; or &apos;.&apos;; the id a copied role gets.
          </p>
        </div>
        <div className="space-y-1">
          <label htmlFor={`${id}-kind`} className={LABEL}>
            Kind
          </label>
          <select
            id={`${id}-kind`}
            value={draft.kind}
            onChange={(e) => setDraft({ ...draft, kind: e.target.value as Kind, profile_id: "" })}
            className={SELECT}
          >
            <option value="work">Work: runs a step in the built-in Worker&apos;s place</option>
            <option value="review">Review: reviews a step after the built-in Reviewer</option>
          </select>
        </div>
      </div>
      <div className="space-y-1">
        <label htmlFor={`${id}-description`} className={LABEL}>
          When it fits
        </label>
        <Input
          id={`${id}-description`}
          value={draft.description}
          onChange={(e) => setDraft({ ...draft, description: e.target.value })}
          maxLength={300}
          placeholder="e.g. steps that change authentication or permissions"
          aria-describedby={`${id}-description-help`}
        />
        <p id={`${id}-description-help`} className="text-xs text-muted-foreground">
          The Planner reads this line with the template&apos;s key to decide whether a step needs it.
        </p>
      </div>
      <DefinitionFields
        projectId={projectId}
        kind={draft.kind}
        draft={draft}
        onChange={(d) => setDraft({ ...draft, ...d })}
        limit={limit}
        autoFocus={!!template}
      />
      <FormFooter error={error} busy={busy} submit={template ? "Save" : "Add template"} onCancel={onCancel} />
    </form>
  );
}

function TemplateRow({
  projectId,
  template,
  limit,
  onTemplates,
}: {
  projectId: string;
  template: RoleTemplate;
  limit: number;
  onTemplates: (templates: RoleTemplates) => void;
}) {
  const [editing, setEditing] = useState(false);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const remove = async () => {
    if (!confirm(`Delete role template "${template.key}"? Task roles copied from it keep their versions.`)) return;
    setBusy(true);
    try {
      onTemplates(await deleteRoleTemplate(projectId, template.key));
    } catch (err) {
      setError(errorText(err));
      setBusy(false);
    }
  };
  return (
    <Row
      title={
        <>
          <span className="break-all font-mono text-sm font-semibold">{template.key}</span>
          <Badge variant="outline">{template.kind === "work" ? "Work" : "Review"}</Badge>
          <span className="text-xs">
            <span className="text-muted-foreground">Profile</span>{" "}
            {template.profile ? template.profile.name : `as the built-in ${builtinName(template.kind)}`}
          </span>
          {template.profile && !template.profile.allowed && (
            <Badge variant="warning" title="The project no longer allows this profile: a copy leaves it out">
              not allowed now
            </Badge>
          )}
        </>
      }
      actions={
        <EditDelete
          what="role template"
          name={template.key}
          editing={editing}
          busy={busy}
          onEdit={() => setEditing(!editing)}
          onDelete={remove}
        />
      }
      below={
        <>
          {error && <p role="alert" className="text-xs text-destructive">{error}</p>}
          {editing && (
            <div className={EDIT_BOX}>
              <TemplateForm
                projectId={projectId}
                template={template}
                limit={limit}
                onSaved={(templates) => {
                  setEditing(false);
                  onTemplates(templates);
                }}
                onCancel={() => setEditing(false)}
              />
            </div>
          )}
        </>
      }
    >
      <p className="text-xs">{template.description || <span className="text-muted-foreground">No description</span>}</p>
      <p className="line-clamp-2 whitespace-pre-wrap break-words font-mono text-xs text-muted-foreground">
        {template.role_prompt}
      </p>
    </Row>
  );
}

/** The project's role templates, which its tasks' Planners copy as task roles; whether they can depends on the
 * project's Planner roles setting. */
export function ProjectRoleTemplates({ projectId, rolesOn }: { projectId: string; rolesOn: boolean }) {
  const [data, setData] = useState<RoleTemplates | null>(null);
  const [error, setError] = useState("");
  useEffect(() => {
    let alive = true;
    fetchRoleTemplates(projectId)
      .then((d) => alive && setData(d))
      .catch((err) => alive && setError(errorText(err)));
    return () => {
      alive = false;
    };
  }, [projectId]);
  if (error) return <p role="alert" className="text-sm text-destructive">{error}</p>;
  if (!data) return <p className="text-sm text-muted-foreground">Loading…</p>;
  return (
    <div className="space-y-3">
      {!rolesOn && (
        <p role="status" className="rounded-md border border-warning/40 px-3 py-2 text-xs">
          Planner roles are off for this project, so its Planners define no roles and use no template. Turn them on in
          the Settings tab.
        </p>
      )}
      <RegistryList
        title="Role templates"
        hint="Roles a task's Planner copies when a step fits one; its copy is the task's own role from then on."
        addLabel="New template"
        form={(close) => (
          <TemplateForm
            projectId={projectId}
            template={null}
            limit={data.role_text_max_chars}
            onSaved={(templates) => {
              close();
              setData(templates);
            }}
            onCancel={close}
          />
        )}
      >
        {data.templates.length === 0 && (
          <li className="px-5 py-3 text-xs text-muted-foreground">No templates yet.</li>
        )}
        {data.templates.map((template) => (
          <TemplateRow
            key={template.key}
            projectId={projectId}
            template={template}
            limit={data.role_text_max_chars}
            onTemplates={setData}
          />
        ))}
      </RegistryList>
    </div>
  );
}
