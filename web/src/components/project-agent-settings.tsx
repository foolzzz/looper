import { useEffect, useId, useState } from "react";
import { Loader2, Plus, Trash2 } from "lucide-react";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { AgentProfileSelect } from "@/components/agent-profiles";
import { CollapsibleRow } from "@/components/collapsible-row";
import { LayerSettingsForm } from "@/components/layer-settings";
import { LABEL } from "@/components/registry-ui";
import {
  fetchAllowedProfiles,
  fetchProjectAgentSettings,
  setAllowedProfiles,
  updateProjectLayer,
  updateProjectRoleSettings,
  type AllowedProfile,
  type EffectiveSettings,
  type LayerValues,
  type ProjectAgentSettings,
  type ProjectRole,
  type SettingSource,
} from "@/lib/api";
import { roleLabel } from "@/lib/runtime-catalog";
import { AGENT_PROFILE, settingsHref } from "@/lib/settings-pages";
import { formatUsd } from "@/lib/usage";
import { errorText, useAction } from "@/lib/use-action";
import { useCollapsibleRows } from "@/lib/use-collapsible-rows";
import { profileName } from "@/lib/use-registry";

const ROLES: ProjectRole[] = ["planner", "worker", "reviewer"];

/** The project layer's row: its id, and its link #projects/<id>/profiles/project. */
const PROJECT_ROW = "project";
/** The row of the profiles the Planner may choose: its link is #projects/<id>/profiles/planner-choices. */
const CHOICES_ROW = "planner-choices";

/** Where a role's value comes from, as its row says it. */
const SOURCES: Partial<Record<SettingSource, string>> = {
  builtin: "shared defaults",
  system: "shared defaults",
  role: "role default",
  project: "this project",
  role_project: "this role in this project",
};

/** The Agent Profile in effect (with its effort) and where it comes from: a role's line. */
const profileText = (effective: EffectiveSettings) =>
  `${profileName(effective.profile.value)} (${SOURCES[effective.profile.source] ?? effective.profile.source})`;

/** The project row's line: the Agent Profile the roles that do not set one here run with, when they agree. */
function projectSummary(settings: ProjectAgentSettings) {
  const shown = new Set(
    ROLES.filter((role) => settings.roles[role].values.profile_id == null).map((role) =>
      profileText(settings.roles[role].effective),
    ),
  );
  return shown.size === 1 ? [...shown][0] : shown.size === 0 ? "Agent Profile set per role" : "Agent Profile varies by role";
}

/** The Planner list's line: what is on it, or that it is empty. */
function choicesSummary(profiles: AllowedProfile[]) {
  if (!profiles.length) return "empty: the Planner cannot change a profile";
  return profiles.map(profileName).join(", ");
}

/** A price per million tokens, or that it is unknown. */
const perMillion = (usd: number | null) => (usd == null ? "unknown" : formatUsd(usd));

/**
 * The Agent Profiles this project's task Planners may choose for their Workers and Reviewers: each with its model,
 * the roles it can run and its price, and Remove; then a picker to add one. Each change saves the whole list and
 * shows it as the server answers.
 */
function PlannerChoices({
  projectId,
  profiles,
  loadError,
  onSaved,
}: {
  projectId: string;
  profiles: AllowedProfile[] | null;
  loadError: string;
  onSaved: (profiles: AllowedProfile[]) => void;
}) {
  const id = useId();
  const [adding, setAdding] = useState("");
  const action = useAction();

  if (!profiles) {
    return loadError ? (
      <p role="alert" className="text-xs text-destructive">{loadError}</p>
    ) : (
      <Loader2 className="h-5 w-5 animate-spin text-muted-foreground" aria-label="Loading" />
    );
  }
  const ids = profiles.map((p) => p.profile_id);
  const save = (next: string[]) =>
    action.run(async () => onSaved((await setAllowedProfiles(projectId, next)).profiles));
  // The server keeps a profile once, so adding one already listed changes nothing.
  const add = async () => {
    if (await save([...ids, adding])) setAdding("");
  };
  const remove = (profile: AllowedProfile) => {
    if (confirm(`Remove "${profileName(profile)}" from the list? What this project's Planners chose of it goes too.`)) {
      save(ids.filter((other) => other !== profile.profile_id));
    }
  };

  return (
    <>
      <p className="text-xs text-muted-foreground">
        A task&apos;s Planner may choose any profile on this list for its own task&apos;s Workers or Reviewers, for the
        rest of the task or for one step, without asking, a pricier one included. A profile you choose for a task
        wins over the Planner&apos;s choice.
      </p>
      {profiles.length === 0 ? (
        <p className="rounded-md border border-dashed border-border px-3 py-2 text-xs text-muted-foreground">
          The list is empty, so a task&apos;s Planner cannot change its Workers&apos; or Reviewers&apos;{" "}
          {AGENT_PROFILE.name}.
        </p>
      ) : (
        <ul aria-label="Profiles the Planner may choose" className="divide-y divide-border rounded-md border border-border">
          {profiles.map((profile) => {
            const { input, output } = profile.usd_per_million_tokens;
            return (
              <li key={profile.profile_id} className="flex flex-wrap items-center gap-x-3 gap-y-1 px-3 py-2">
                {/* Remove goes below the text when the row is too narrow for both. */}
                <div className="min-w-0 flex-1 basis-60 space-y-0.5">
                  <p className="break-words text-sm font-medium">{profileName(profile)}</p>
                  <p className="break-words text-xs text-muted-foreground">
                    model <span className="font-mono text-foreground/80">{profile.model}</span> · can run{" "}
                    <span className="text-foreground">{profile.roles.map(roleLabel).join(", ")}</span> ·{" "}
                    {input == null && output == null
                      ? "price unknown"
                      : `${perMillion(input)} input, ${perMillion(output)} output per million tokens`}
                  </p>
                </div>
                <Button
                  type="button"
                  variant="ghost"
                  size="sm"
                  onClick={() => remove(profile)}
                  disabled={action.busy}
                  aria-label={`Remove ${profileName(profile)} from the list`}
                  className="text-muted-foreground hover:bg-destructive/10 hover:text-destructive"
                >
                  <Trash2 className="h-3.5 w-3.5" /> Remove
                </Button>
              </li>
            );
          })}
        </ul>
      )}
      <div className="space-y-1">
        <label htmlFor={`${id}-add`} className={LABEL}>Add a profile</label>
        <div className="flex flex-col gap-2 sm:flex-row sm:items-start">
          <div className="min-w-0 flex-1">
            {/* The Worker and the Reviewer need the same of a runtime, so this leaves out a profile that can run
                neither. */}
            <AgentProfileSelect
              id={`${id}-add`}
              value={adding}
              onChange={setAdding}
              emptyLabel={`Choose an ${AGENT_PROFILE.name}`}
              roles={["worker"]}
            />
          </div>
          <Button type="button" size="sm" onClick={add} disabled={!adding || action.busy} className="h-9">
            {action.busy ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <Plus className="h-3.5 w-3.5" />} Add
          </Button>
        </div>
      </div>
      {action.error && <p role="alert" className="text-xs text-destructive">{action.error}</p>}
    </>
  );
}

/** Whether a layer sets anything itself. */
const setsAny = (values: LayerValues) =>
  values.env.length > 0 ||
  (Object.keys(values) as (keyof LayerValues)[]).some((key) => key !== "env" && values[key] != null);

/**
 * A project's Agent Profiles tab: a row for its own layer (all its agents), then one for each role in it (Planner,
 * Worker, Reviewer), the Agent Profile first, then the profiles its Planners may choose. `linked`
 * (#projects/<id>/profiles/<role>, /project or /planner-choices) opens one and scrolls it into view.
 */
export function ProjectAgentSettingsSection({ projectId, linked }: { projectId: string; linked: string }) {
  const id = useId();
  const [settings, setSettings] = useState<ProjectAgentSettings | null>(null);
  const [error, setError] = useState("");
  const [allowed, setAllowed] = useState<AllowedProfile[] | null>(null);
  const [allowedError, setAllowedError] = useState("");
  const row = useCollapsibleRows(linked, settings !== null);

  useEffect(() => {
    let alive = true;
    fetchProjectAgentSettings(projectId)
      .then((next) => alive && setSettings(next))
      .catch((err) => alive && setError(err instanceof Error ? err.message : String(err)));
    fetchAllowedProfiles(projectId)
      .then((next) => alive && setAllowed(next.profiles))
      .catch((err) => alive && setAllowedError(errorText(err)));
    return () => {
      alive = false;
    };
  }, [projectId]);

  const overridden = (values: LayerValues) =>
    setsAny(values) && <Badge className="shrink-0">overridden here</Badge>;

  return (
    <div className="space-y-3">
      <p className="text-sm text-muted-foreground">
        Which Agent Profile this project&apos;s Planner, Workers and Reviewers run, and their other overrides, over
        the shared defaults in{" "}
        <a href={settingsHref("roles", "shared")} className="text-primary underline-offset-2 hover:underline">
          Settings → Roles
        </a>
        . The Manager is not per project; set it in{" "}
        <a href={settingsHref("roles", "manager")} className="text-primary underline-offset-2 hover:underline">
          Settings → Roles
        </a>
        .
      </p>

      {!settings ? (
        error ? (
          <p role="alert" className="text-xs text-destructive">{error}</p>
        ) : (
          <div className="flex h-24 items-center justify-center">
            <Loader2 className="h-5 w-5 animate-spin text-muted-foreground" aria-label="Loading" />
          </div>
        )
      ) : (
        <div className="space-y-2">
          <CollapsibleRow
            id={`project-profiles-${PROJECT_ROW}`}
            {...row(PROJECT_ROW)}
            name="All roles in this project"
            summary={projectSummary(settings)}
            marks={overridden(settings.project.values)}
          >
            {(dirty) => (
              <>
                <p className="text-xs text-muted-foreground">
                  For this project&apos;s Planner, Workers and Reviewers, over Settings → Roles. Empty fields inherit;
                  each role&apos;s row shows what applies to it.
                </p>
                <LayerSettingsForm
                  idPrefix={`${id}-project`}
                  layer="project"
                  values={settings.project.values}
                  limits={settings.limits}
                  roles={ROLES}
                  profileFirst
                  submitLabel="Save overrides"
                  onDirtyChange={dirty.layer}
                  onSave={async (changes) => {
                    const next = await updateProjectLayer(projectId, changes);
                    setSettings(next);
                    return next.project.values;
                  }}
                />
              </>
            )}
          </CollapsibleRow>

          {ROLES.map((role) => (
            <CollapsibleRow
              key={role}
              id={`project-profiles-${role}`}
              {...row(role)}
              name={roleLabel(role)}
              summary={profileText(settings.roles[role].effective)}
              marks={overridden(settings.roles[role].values)}
            >
              {(dirty) => (
                <>
                  <p className="text-xs text-muted-foreground">
                    The {roleLabel(role)} of this project&apos;s tasks, over &ldquo;All roles in this project&rdquo; and
                    Settings → Roles. Empty fields inherit.
                    {role === "reviewer" && " Without a harness of its own, a Reviewer uses the Worker's."}
                  </p>
                  <LayerSettingsForm
                    idPrefix={`${id}-${role}`}
                    layer="role_project"
                    roles={[role]}
                    values={settings.roles[role].values}
                    effective={settings.roles[role].effective}
                    limits={settings.limits}
                    profileFirst
                    submitLabel="Save overrides"
                    onDirtyChange={dirty.layer}
                    onSave={async (changes) => {
                      const next = await updateProjectRoleSettings(projectId, role, changes);
                      setSettings(next);
                      return next.roles[role].values;
                    }}
                  />
                </>
              )}
            </CollapsibleRow>
          ))}

          <CollapsibleRow
            id={`project-profiles-${CHOICES_ROW}`}
            {...row(CHOICES_ROW)}
            name="Profiles the Planner may choose"
            summary={allowed ? choicesSummary(allowed) : allowedError ? "could not load" : ""}
          >
            {() => (
              <PlannerChoices projectId={projectId} profiles={allowed} loadError={allowedError} onSaved={setAllowed} />
            )}
          </CollapsibleRow>
        </div>
      )}
    </div>
  );
}
