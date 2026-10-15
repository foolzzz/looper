import { useEffect, useId, useState } from "react";
import { Loader2 } from "lucide-react";
import { LayerSettingsForm } from "@/components/layer-settings";
import {
  fetchProjectAgentSettings,
  updateProjectLayer,
  updateProjectRoleSettings,
  type ProjectAgentSettings,
  type ProjectRole,
} from "@/lib/api";
import { AGENT_PROFILE } from "@/lib/settings-pages";
import { cn } from "@/lib/utils";

const ROLES: { id: ProjectRole; label: string }[] = [
  { id: "planner", label: "Planner" },
  { id: "worker", label: "Worker" },
  { id: "reviewer", label: "Reviewer" },
];

/**
 * A project's agent settings: its own layer (all its agents), then one role at a time (Planner, Worker, Reviewer
 * in this project) with what applies to that role and where each value comes from.
 */
export function ProjectAgentSettingsSection({ projectId }: { projectId: string }) {
  const id = useId();
  const [state, setState] = useState<{ key: string; settings?: ProjectAgentSettings; error?: string } | null>(null);
  const [role, setRole] = useState<ProjectRole>("planner");

  useEffect(() => {
    let alive = true;
    fetchProjectAgentSettings(projectId)
      .then((settings) => alive && setState({ key: projectId, settings }))
      .catch((err) => alive && setState({ key: projectId, error: err instanceof Error ? err.message : String(err) }));
    return () => {
      alive = false;
    };
  }, [projectId]);
  const current = state?.key === projectId ? state : null;
  const settings = current?.settings ?? null;
  const error = current?.error ?? "";
  const setSettings = (next: ProjectAgentSettings) => setState({ key: projectId, settings: next });

  const body = !settings ? (
    error ? (
      <p role="alert" className="text-xs text-destructive">{error}</p>
    ) : (
      <div className="flex h-24 items-center justify-center">
        <Loader2 className="h-5 w-5 animate-spin text-muted-foreground" aria-label="Loading" />
      </div>
    )
  ) : (
    <div className="space-y-5">
      <div className="space-y-3">
        <div>
          <h3 className="text-xs font-semibold">All agents in this project</h3>
          <p className="mt-0.5 text-xs text-muted-foreground">
            Over Settings → Roles for this project&apos;s Planner, Workers and Reviewers. What applies to each role is
            shown below.
          </p>
        </div>
        <LayerSettingsForm
          idPrefix={`${id}-project`}
          layer="project"
          values={settings.project.values}
          limits={settings.limits}
          roles={ROLES.map((r) => r.id)}
          onSave={async (changes) => {
            const next = await updateProjectLayer(projectId, changes);
            setSettings(next);
            return next.project.values;
          }}
        />
      </div>

      <div className="space-y-3 border-t border-border pt-4">
        <div className="flex flex-wrap items-center justify-between gap-2">
          <h3 id={`${id}-roles`} className="text-xs font-semibold">Each role in this project</h3>
          <div role="group" aria-labelledby={`${id}-roles`} className="flex rounded-md border border-border p-0.5">
            {ROLES.map((r) => (
              <button
                key={r.id}
                type="button"
                aria-pressed={role === r.id}
                onClick={() => setRole(r.id)}
                className={cn(
                  "rounded px-3 py-1 text-xs font-medium transition-colors focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring",
                  role === r.id ? "bg-primary/10 text-primary" : "text-muted-foreground hover:text-foreground",
                )}
              >
                {r.label}
              </button>
            ))}
          </div>
        </div>
        <div className="space-y-3">
          <p className="text-xs text-muted-foreground">
            The {ROLES.find((r) => r.id === role)?.label} of this project&apos;s tasks, over everything above.
            {role === "reviewer" && " Without a harness of its own, a Reviewer uses the Worker's."}
          </p>
          <LayerSettingsForm
            key={role}
            idPrefix={`${id}-${role}`}
            layer="role_project"
            roles={[role]}
            values={settings.roles[role].values}
            effective={settings.roles[role].effective}
            limits={settings.limits}
            onSave={async (changes) => {
              const next = await updateProjectRoleSettings(projectId, role, changes);
              setSettings(next);
              return next.roles[role].values;
            }}
          />
        </div>
      </div>
    </div>
  );

  return (
    <section aria-labelledby={`${id}-title`} className="space-y-4 rounded-lg border border-border bg-card px-5 py-4">
      <div>
        <h2 id={`${id}-title`} className="text-sm font-semibold">Agent settings</h2>
        <p className="mt-1 text-xs text-muted-foreground">
          Harness, {AGENT_PROFILE.name}, effort, environment and spend cap per run. Each resolves Settings → Roles
          (the shared defaults, then the role) → this project → the role in this project; the most specific one set
          wins, and empty fields inherit. Each role in this project can also have its own limit on how many run at
          once here.
        </p>
      </div>
      {body}
    </section>
  );
}
