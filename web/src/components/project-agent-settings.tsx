import { useEffect, useId, useState } from "react";
import { Loader2 } from "lucide-react";
import { Badge } from "@/components/ui/badge";
import { CollapsibleRow } from "@/components/collapsible-row";
import { LayerSettingsForm } from "@/components/layer-settings";
import {
  fetchProjectAgentSettings,
  updateProjectLayer,
  updateProjectRoleSettings,
  type EffectiveSettings,
  type LayerValues,
  type ProjectAgentSettings,
  type ProjectRole,
  type SettingSource,
} from "@/lib/api";
import { roleLabel } from "@/lib/runtime-catalog";
import { settingsHref } from "@/lib/settings-pages";
import { useCollapsibleRows } from "@/lib/use-collapsible-rows";

const ROLES: ProjectRole[] = ["planner", "worker", "reviewer"];

/** The project layer's row: its id, and its link #projects/<id>/profiles/project. */
const PROJECT_ROW = "project";

/** Where a role's value comes from, as its row says it. */
const SOURCES: Partial<Record<SettingSource, string>> = {
  builtin: "shared defaults",
  system: "shared defaults",
  role: "role default",
  project: "this project",
  role_project: "this role in this project",
};

const effortText = (effort: string | null) => (effort ? `effort ${effort}` : "default effort");
const profileText = (effective: EffectiveSettings) =>
  `${effective.profile.value.name} (${SOURCES[effective.profile.source] ?? effective.profile.source})`;

/** A role's line: the Agent Profile in effect and where it comes from, the effort, the limit on how many run. */
function roleSummary(role: ProjectRole, effective: EffectiveSettings) {
  const limit = effective.max_concurrency?.value;
  return [
    profileText(effective),
    effortText(effective.effort.value),
    limit == null ? "" : role === "planner" ? `up to ${limit} ${limit === 1 ? "task" : "tasks"} at once` : `up to ${limit} at once`,
  ]
    .filter(Boolean)
    .join(" · ");
}

/** The project row's line: what the roles that do not set it again here run with, when they agree. */
function projectSummary(settings: ProjectAgentSettings) {
  const agreed = (field: "profile_id" | "effort", show: (effective: EffectiveSettings) => string, what: string) => {
    const shown = new Set(
      ROLES.filter((role) => settings.roles[role].values[field] == null).map((role) => show(settings.roles[role].effective)),
    );
    return shown.size === 1 ? [...shown][0] : shown.size === 0 ? `${what} set per role` : `${what} varies by role`;
  };
  return [
    agreed("profile_id", profileText, "Agent Profile"),
    agreed("effort", (effective) => effortText(effective.effort.value), "effort"),
  ].join(" · ");
}

/** Whether a layer sets anything itself. */
const setsAny = (values: LayerValues) =>
  values.env.length > 0 ||
  (Object.keys(values) as (keyof LayerValues)[]).some((key) => key !== "env" && values[key] != null);

/**
 * A project's Agent Profiles tab: a row for its own layer (all its agents), then one for each role in it (Planner,
 * Worker, Reviewer), the Agent Profile first. `linked` (#projects/<id>/profiles/<role>, or /project) opens one and
 * scrolls it into view.
 */
export function ProjectAgentSettingsSection({ projectId, linked }: { projectId: string; linked: string }) {
  const id = useId();
  const [settings, setSettings] = useState<ProjectAgentSettings | null>(null);
  const [error, setError] = useState("");
  const row = useCollapsibleRows(linked, settings !== null);

  useEffect(() => {
    let alive = true;
    fetchProjectAgentSettings(projectId)
      .then((next) => alive && setSettings(next))
      .catch((err) => alive && setError(err instanceof Error ? err.message : String(err)));
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
              summary={roleSummary(role, settings.roles[role].effective)}
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
        </div>
      )}
    </div>
  );
}
