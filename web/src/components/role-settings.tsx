import { useCallback, useEffect, useId, useState } from "react";
import { History, Loader2, Power, PowerOff, RotateCcw, Save } from "lucide-react";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Switch } from "@/components/ui/switch";
import { CollapsibleRow } from "@/components/collapsible-row";
import { LayerSettingsForm } from "@/components/layer-settings";
import {
  fetchAgentSettings,
  fetchRoleConfigs,
  resetRolePrompt,
  updateRoleConfig,
  updateRoleSettings,
  updateSystemSettings,
  type AgentRole,
  type AgentSettings,
  type EffectiveSettings,
  type RoleConfig,
} from "@/lib/api";
import { roleLabel } from "@/lib/runtime-catalog";
import { useCollapsibleRows } from "@/lib/use-collapsible-rows";
import { profileName } from "@/lib/use-registry";
import { formatUsd } from "@/lib/usage";
import { cn } from "@/lib/utils";

const ROLES: readonly AgentRole[] = ["manager", "planner", "worker", "reviewer"];
const isRole = (role: string): role is AgentRole => (ROLES as readonly string[]).includes(role);

/** The role configs in the order a task meets them. */
function rank(role: string) {
  const at = (ROLES as readonly string[]).indexOf(role);
  return at < 0 ? ROLES.length : at;
}

// --- The role prompt ---

/** The role prompt: one text, the role layer of the agent's system prompt. */
function RolePromptEditor({ id, value, onChange }: { id: string; value: string; onChange: (v: string) => void }) {
  const lineCount = value.split("\n").length;

  return (
    <div className="space-y-2">
      <div className="flex items-center justify-between">
        <label htmlFor={id} className="text-xs font-medium text-muted-foreground">Role prompt</label>
        <span className="text-[10px] text-muted-foreground">
          {value.length} chars · {lineCount} lines
        </span>
      </div>
      <p id={`${id}-hint`} className="text-[11px] text-muted-foreground">
        This text is the role layer. At launch Looper combines it with the harness, the project, the task and the
        reply format into the agent&apos;s system prompt, added to the runtime&apos;s own; a run&apos;s history page
        (from Runs) shows that whole prompt.
      </p>
      <textarea
        id={id}
        aria-describedby={`${id}-hint`}
        value={value}
        onChange={(e) => onChange(e.target.value)}
        rows={Math.max(12, Math.min(lineCount + 2, 40))}
        className="w-full resize-y rounded-md border border-input bg-transparent p-3 text-sm text-foreground font-mono leading-relaxed placeholder:text-muted-foreground focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring"
        placeholder="What this kind of agent is responsible for..."
      />
    </div>
  );
}

// --- A role's own fields (role_configs) ---

/** The fields the form edits, as saved. */
function fieldsOf(config: RoleConfig) {
  return {
    display_name: config.display_name,
    max_turns: config.max_turns,
    timeout_seconds: config.timeout_seconds,
    enabled: config.enabled,
    system_prompt: config.system_prompt,
  };
}

function RoleConfigForm({
  config,
  onSave,
  onResetPrompt,
  onDirtyChange,
  saving,
}: {
  config: RoleConfig;
  onSave: (fields: Partial<RoleConfig>) => void;
  onResetPrompt: () => void;
  /** Told whether the form holds unsaved edits. */
  onDirtyChange: (dirty: boolean) => void;
  saving: boolean;
}) {
  const id = useId();
  const [form, setForm] = useState(() => fieldsOf(config));

  useEffect(() => {
    setForm(fieldsOf(config));
  }, [config]);

  const dirty = JSON.stringify(form) !== JSON.stringify(fieldsOf(config));
  useEffect(() => onDirtyChange(dirty), [dirty, onDirtyChange]);

  return (
    <div className="space-y-5">
      <div className="space-y-1.5">
        <label htmlFor={`${id}-name`} className="text-xs font-medium text-muted-foreground">Display Name</label>
        <Input
          id={`${id}-name`}
          value={form.display_name}
          onChange={(e) => setForm({ ...form, display_name: e.target.value })}
        />
      </div>

      <div className="grid gap-4 sm:grid-cols-2">
        <div className="space-y-1.5">
          <label htmlFor={`${id}-turns`} className="text-xs font-medium text-muted-foreground">Max Turns</label>
          <Input
            id={`${id}-turns`}
            type="number"
            min={0}
            value={form.max_turns}
            onChange={(e) => setForm({ ...form, max_turns: Number(e.target.value) })}
          />
          <p className="text-[11px] text-muted-foreground">0 = unlimited</p>
        </div>
        <div className="space-y-1.5">
          <label htmlFor={`${id}-timeout`} className="text-xs font-medium text-muted-foreground">Timeout (s)</label>
          <Input
            id={`${id}-timeout`}
            type="number"
            min={0}
            value={form.timeout_seconds}
            onChange={(e) => setForm({ ...form, timeout_seconds: Number(e.target.value) })}
          />
          <p className="text-[11px] text-muted-foreground">0 = unlimited</p>
        </div>
      </div>

      <div className="space-y-1">
        <span className="text-xs font-medium text-muted-foreground">Permissions</span>
        <p className="text-sm">
          Every tool, no permission prompts{" "}
          <code className="rounded bg-muted px-1 py-0.5 font-mono text-xs">--dangerously-skip-permissions</code>
        </p>
        <p className="text-[11px] text-muted-foreground">
          The same for every Looper agent; the role prompt says what this agent is for. Lark and Telegram pass on
          only the senders listed in Settings → Channels.
        </p>
      </div>

      <div className="flex items-center justify-between rounded-md border border-border px-4 py-3">
        <div className="flex items-center gap-2">
          {form.enabled ? (
            <Power className="h-4 w-4 text-success" aria-hidden="true" />
          ) : (
            <PowerOff className="h-4 w-4 text-muted-foreground" aria-hidden="true" />
          )}
          <span className="text-sm">{form.enabled ? "Enabled" : "Disabled"}</span>
        </div>
        <Switch
          checked={form.enabled}
          onChange={(enabled) => setForm({ ...form, enabled })}
          label={`Enable ${config.display_name || config.role}`}
        />
      </div>

      <RolePromptEditor
        id={`${id}-prompt`}
        value={form.system_prompt}
        onChange={(v) => setForm({ ...form, system_prompt: v })}
      />

      {config.default_prompt && config.system_prompt !== config.default_prompt && (
        <div className="flex items-center justify-between rounded-md border border-border bg-muted/20 px-3 py-2">
          <span className="text-[11px] text-muted-foreground">
            This prompt differs from the system-recommended one.
          </span>
          <Button variant="outline" size="sm" onClick={onResetPrompt} disabled={saving}>
            <RotateCcw className="h-3.5 w-3.5" />
            Reset to recommended
          </Button>
        </div>
      )}

      <Button onClick={() => onSave(form)} disabled={saving} className="w-full">
        {saving ? <Loader2 className="h-4 w-4 animate-spin" /> : <Save className="h-4 w-4" />}
        Save prompt and limits
      </Button>
    </div>
  );
}

// --- The page ---

/** What each role does, in a few words, on its row. */
const ROLE_JOBS: Record<AgentRole, string> = {
  manager: "talks with you and creates tasks",
  planner: "plans each task and decides every step",
  worker: "carries out one step",
  reviewer: "checks a step's result",
};

/** The Shared defaults row (the system layer): its id, and its link #settings/roles/shared. */
const SHARED = "shared";

const count = (n: number, one: string) => `${n} ${one}${n === 1 ? "" : "s"}`;

/** "Manager, Planner, Worker and Reviewer". */
function listed(names: string[]) {
  return names.length < 2 ? names.join("") : `${names.slice(0, -1).join(", ")} and ${names[names.length - 1]}`;
}

/** The Shared defaults row's line: what every role runs with unless it sets its own. */
function sharedSummary(effective: EffectiveSettings, roles: number) {
  const budget = effective.run_budget_usd.value;
  return [
    `Applies to all ${count(roles, "role")}`,
    profileName(effective.profile.value),
    budget == null ? "no spend cap" : `${formatUsd(budget)} cap per run`,
    effective.env.length ? count(effective.env.length, "environment variable") : "no environment variables",
  ].join(" · ");
}

/** A role's line: its job, then its Agent Profile (with its effort) and where that comes from, and for the Worker and
 * the Reviewer the default limit in each task. */
function roleSummary(role: AgentRole, effective: EffectiveSettings | undefined) {
  const parts = [ROLE_JOBS[role]];
  if (effective) {
    const own = effective.profile.source === "role";
    parts.push(`${profileName(effective.profile.value)} (${own ? "own" : "shared"})`);
    const limit = effective.max_concurrency?.value;
    if (limit != null && (role === "worker" || role === "reviewer")) parts.push(`up to ${limit} per task`);
  }
  return parts.join(" · ");
}

/**
 * Settings → Roles: a row for the shared defaults (the system layer), then one per role with its overrides of them
 * (its layer) and its own prompt and limits. Every row starts closed and any number can be open; `sub`
 * (#settings/roles/<role>, or /shared) opens one and scrolls it into view.
 */
export function RolesSection({ sub }: { sub: string }) {
  const [configs, setConfigs] = useState<RoleConfig[]>([]);
  const [settings, setSettings] = useState<AgentSettings | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [saving, setSaving] = useState(false);
  const [toast, setToast] = useState<{ role: string; text: string } | null>(null);
  // A link to a row (from Runs or an Agent Profile's "used by") opens it, also while the page is showing.
  const row = useCollapsibleRows(sub.split("/")[0], !loading);

  useEffect(() => {
    const roles = fetchRoleConfigs().then((list) => setConfigs([...list].sort((a, b) => rank(a.role) - rank(b.role))));
    // Both, so that a linked row is scrolled to once the rows above it are there.
    Promise.allSettled([roles, fetchAgentSettings().then(setSettings)]).then((results) => {
      const failed = results.find((result): result is PromiseRejectedResult => result.status === "rejected");
      if (failed) setError((failed.reason as Error).message);
      setLoading(false);
    });
  }, []);

  const flash = (role: string, text: string) => {
    setToast({ role, text });
    setTimeout(() => setToast(null), text.startsWith("Error") ? 3000 : 2000);
  };
  const run = useCallback(async (role: string, action: () => Promise<RoleConfig>, done: string) => {
    setSaving(true);
    try {
      const updated = await action();
      setConfigs((prev) => prev.map((c) => (c.role === updated.role ? updated : c)));
      flash(role, done);
    } catch (e) {
      flash(role, `Error: ${(e as Error).message}`);
    } finally {
      setSaving(false);
    }
  }, []);

  if (loading) {
    return (
      <div className="flex h-64 items-center justify-center">
        <Loader2 className="h-6 w-6 animate-spin text-muted-foreground" aria-label="Loading" />
      </div>
    );
  }

  const nameOf = (config: RoleConfig) => (isRole(config.role) ? roleLabel(config.role) : config.display_name || config.role);

  return (
    <div className="space-y-6">
      {configs.length > 0 && (
        // In the place of the page's description, right under its title: it names the roles the server has.
        <p className="-mt-5 max-w-3xl text-sm text-muted-foreground">
          Looper has {count(configs.length, "role")}: {listed(configs.map(nameOf))}. Each runs with the shared
          defaults unless it sets its own value. A project can override both on its Agent Profiles tab.
        </p>
      )}

      {error && (
        <div role="alert" className="rounded-lg border border-destructive/50 bg-destructive/10 px-5 py-4 text-sm text-destructive">
          {error}
        </div>
      )}

      {!error && configs.length === 0 && (
        <p className="text-sm text-muted-foreground">
          No roles configured. Start the server with a database connection to seed defaults.
        </p>
      )}

      <div className="space-y-2">
        {settings && (
          <CollapsibleRow
            id={`roles-${SHARED}`}
            {...row(SHARED)}
            name="Shared defaults"
            summary={sharedSummary(settings.system.effective, configs.length)}
          >
            {(marks) => (
              <LayerSettingsForm
                idPrefix="roles-system"
                layer="system"
                roles={[...ROLES]}
                values={settings.system.values}
                effective={settings.system.effective}
                limits={settings.limits}
                onDirtyChange={marks.layer}
                onSave={async (changes) => {
                  const next = await updateSystemSettings(changes);
                  setSettings(next);
                  return next.system.values;
                }}
                harnessHint={
                  <>
                    Every agent&apos;s working principles, boundaries and tone, placed before its role prompt. Empty: the
                    built-in harness.
                  </>
                }
              />
            )}
          </CollapsibleRow>
        )}

        {configs.map((config) => {
          const role = isRole(config.role) ? config.role : null;
          const name = nameOf(config);
          const runs = (
            <a
              href={`#runs/${encodeURIComponent(config.role)}`}
              className="flex shrink-0 items-center gap-1 text-xs text-primary underline-offset-2 hover:underline"
            >
              <History className="h-3.5 w-3.5" aria-hidden="true" />
              Its runs
            </a>
          );
          return (
            <CollapsibleRow
              key={config.role}
              id={`roles-${config.role}`}
              {...row(config.role)}
              name={name}
              detail={config.display_name && config.display_name !== name ? config.display_name : undefined}
              summary={role ? roleSummary(role, settings?.roles[role].effective) : ""}
              marks={!config.enabled && <Badge variant="outline" className="shrink-0">Disabled</Badge>}
            >
              {(marks) => (
                <>
                  {settings && role && (
                    <section aria-labelledby={`role-layer-${role}`} className="space-y-3">
                      <div className="flex flex-wrap items-start justify-between gap-2">
                        <div>
                          <h3 id={`role-layer-${role}`} className="text-xs font-semibold">Overrides for this role</h3>
                          <p className="mt-1 text-xs text-muted-foreground">Empty fields use the shared defaults.</p>
                        </div>
                        {runs}
                      </div>
                      <LayerSettingsForm
                        idPrefix={`role-${role}`}
                        layer="role"
                        roles={[role]}
                        values={settings.roles[role].values}
                        effective={settings.roles[role].effective}
                        limits={settings.limits}
                        submitLabel="Save overrides"
                        onDirtyChange={marks.layer}
                        onSave={async (changes) => {
                          const next = await updateRoleSettings(role, changes);
                          setSettings(next);
                          return next.roles[role].values;
                        }}
                        harnessHint={
                          role === "reviewer"
                            ? "Empty: the Worker's harness at this layer, else the shared defaults."
                            : "Working principles, boundaries and tone, placed before the role prompt."
                        }
                      />
                    </section>
                  )}

                  <section
                    aria-labelledby={`role-config-${config.role}`}
                    className={cn("space-y-3", settings && role && "border-t border-border pt-5")}
                  >
                    <div className="flex flex-wrap items-start justify-between gap-2">
                      <h3 id={`role-config-${config.role}`} className="text-xs font-semibold">Role prompt and limits</h3>
                      {!(settings && role) && runs}
                    </div>
                    <RoleConfigForm
                      config={config}
                      saving={saving}
                      onDirtyChange={marks.config}
                      onSave={(fields) => run(config.role, () => updateRoleConfig(config.role, fields), "Saved")}
                      onResetPrompt={() =>
                        run(config.role, () => resetRolePrompt(config.role), "Reset to recommended prompt")
                      }
                    />
                    {toast?.role === config.role && (
                      <div
                        role="status"
                        className={cn(
                          "rounded-lg border px-4 py-2.5 text-sm",
                          toast.text.startsWith("Error")
                            ? "border-destructive/50 bg-destructive/10 text-destructive"
                            : "border-success/50 bg-success/10 text-success",
                        )}
                      >
                        {toast.text}
                      </div>
                    )}
                  </section>
                </>
              )}
            </CollapsibleRow>
          );
        })}
      </div>
    </div>
  );
}
