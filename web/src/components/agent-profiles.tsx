import { useEffect, useId, useState } from "react";
import { AlertTriangle, Loader2 } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import { Input } from "@/components/ui/input";
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
  createAgentProfile,
  deleteAgentProfile,
  fetchPromptPreview,
  protocolLabel,
  updateAgentProfile,
  type AgentProfile,
  type AgentRole,
  type ProfileUsedBy,
  type PromptPreview,
  type Provider,
  type Model,
  type ProviderLogin,
  type Registry,
  type Runtime,
  type SelfCheck,
} from "@/lib/api";
import { LOGIN_HELP, PLANNED_RUNTIMES, RUNTIME_TYPES, loginUsable, roleLabel } from "@/lib/runtime-catalog";
import { AGENT_PROFILE, settingsHref } from "@/lib/settings-pages";
import { useSettingsTarget } from "@/lib/settings-target";
import { useAction } from "@/lib/use-action";
import { profileLabel, useRegistry } from "@/lib/use-registry";
import { useStartupChecks } from "@/lib/use-startup-checks";

// --- What a runtime can be paired with ---

function runtimeType(registry: Registry, runtime: Runtime | undefined) {
  return registry.runtime_types.find((t) => t.type === runtime?.type);
}

/** Match the model's exact protocol and account authentication to the runtime, as the server does. */
function pairProblem(registry: Registry, runtime: Runtime | undefined, model: Model): string | null {
  const type = runtimeType(registry, runtime);
  const provider = registry.providers.find((p) => p.provider_id === model.provider_id);
  if (!runtime || !type || !provider) return "not available";
  const translated = !type.protocols.includes(model.protocol) && type.translated_protocols.includes(model.protocol);
  if (!type.protocols.includes(model.protocol) && !translated) {
    return `speaks ${type.protocols.map((protocol) => protocolLabel(registry, protocol as Model["protocol"])).join(", ")}`;
  }
  if (translated && provider.auth !== "api_key") return "login providers cannot be translated";
  if (translated && runtime.launcher === "ttadk") return "TTADK manages its own provider; use the native launcher";
  if (type.supported_models && !type.supported_models.includes(model.name)) {
    return `runs only ${type.supported_models.join(", ")}`;
  }
  if (!runtime.auth_kinds.includes(provider.auth)) {
    return provider.auth === "api_key" ? "takes only its own login" : "cannot use another runtime's login";
  }
  if (provider.auth === "oauth" && provider.oauth_runtime_type !== type.type) {
    return `${provider.name} is the login of another runtime`;
  }
  return null;
}

/** Agent teams for a profile on `runtime` that does not say: on for Claude Code, whose feature they are, off for
 * any other runtime. The server decides the same way for a profile saved without them. */
function defaultTeams(registry: Registry, runtime: Runtime | undefined): boolean {
  return runtimeType(registry, runtime)?.type === "claude-code";
}

function runtimeTitle(runtime: Runtime): string {
  if (runtime.launcher === "ttadk") return `${runtime.name} (launcher: ${runtime.launcher})`;
  if (!runtime.builtin) return `${runtime.name} (custom, legacy)`;
  return RUNTIME_TYPES[runtime.type]?.title ?? runtime.type_label;
}

// --- The Claude Code login, as the startup self-check found it ---

/** The startup self-check's Claude login result, refreshed until all startup checks finish. */
function useClaudeLogin(): SelfCheck | null | undefined {
  const checks = useStartupChecks();
  return checks === undefined ? undefined : checks.find((c) => c.id === "claude_login") ?? null;
}

function loginText(check: SelfCheck | null | undefined): string {
  if (check === undefined || check?.status === "pending") return "checking the login…";
  if (!check) return "login not checked";
  if (check.status === "ok") return check.detail;
  if (check.status === "fail") return "not logged in: run claude auth login";
  return check.detail;
}

/** A runtime login the registry reports (Codex's `codex login status`, Grok's login file, or a fork a run found), in
 * words. */
function providerLoginText(login: ProviderLogin | null): string {
  if (!login) return "login not checked";
  return login.state === "unavailable" ? `unavailable ${login.detail}` : login.detail;
}

/** The provider's login under the form: what it is now and, when it cannot run, how to fix it. */
function LoginHint({ provider, typeLabel, claude }: { provider: Provider; typeLabel: string; claude?: SelfCheck | null }) {
  const own = provider.login;
  const text = own ? providerLoginText(own) : loginText(claude);
  const failing = own ? !loginUsable(own.state) : claude?.status === "fail";
  const help = LOGIN_HELP[provider.oauth_runtime_type ?? ""] ?? { signIn: "" };
  let fix: React.ReactNode = null;
  if (!own && failing) {
    fix = <>(<code className="font-mono">claude auth login</code>, then restart looper serve)</>;
  } else if (own?.state === "unavailable") {
    fix = <>(check {help.check ? <code className="font-mono">{help.check}</code> : "the login"} on the server&apos;s machine,
      then <a href={settingsHref("providers", provider.provider_id)} className="underline underline-offset-2">use the login
      again</a>)</>;
  } else if (own && failing) {
    fix = <>(<code className="font-mono">{help.signIn}</code>{help.how ? ` ${help.how}` : ""} on the server&apos;s machine)</>;
  }
  return (
    <p className={failing ? "flex items-start gap-1 text-warning" : undefined}>
      {failing && <AlertTriangle className="mt-0.5 h-3 w-3 shrink-0" aria-hidden="true" />}
      <span>
        {typeLabel} login (OAuth) · {text}
        {fix && <> {fix}</>}
      </span>
    </p>
  );
}

// --- The form ---

/**
 * Create or edit an Agent Profile: a name, then a model and its protocol, then a provider that offers it, then a
 * runtime that can run it, and its effort. `nested` renders it without a form element of its own, for use inside
 * another form; `onDone` gets the saved profile, or nothing
 * when cancelled.
 */
export function AgentProfileForm({
  profile,
  registry,
  onDone,
  nested = false,
}: {
  profile: AgentProfile | null;
  registry: Registry;
  onDone: (saved?: AgentProfile) => void;
  nested?: boolean;
}) {
  const id = useId();
  const login = useClaudeLogin();
  const selectable = registry.runtimes.filter((r) => r.builtin || r.runtime_id === profile?.runtime_id);
  const [draft, setDraft] = useState(() => {
    const runtime = registry.runtimes.find((r) => r.runtime_id === profile?.runtime_id) ?? selectable[0];
    return {
      name: profile?.name ?? "",
      runtime_id: runtime?.runtime_id ?? "",
      modelName: profile?.model.name ?? "",
      protocol: profile?.model.protocol ?? "",
      model_id: profile?.model_id ?? "",
      effort: profile?.effort ?? "",
      agent_teams: profile?.agent_teams ?? defaultTeams(registry, runtime),
      custom_prompt: profile?.custom_prompt ?? "",
      fallback_profile_id: profile?.fallback_profile_id ?? "",
    };
  });
  const action = useAction();
  const [runtimeNote, setRuntimeNote] = useState("");
  const [modelError, setModelError] = useState("");
  const modelProblem = modelError || action.error;
  const [warnings, setWarnings] = useState<string[]>([]);
  const [saved, setSaved] = useState<AgentProfile | null>(null);
  const set = (change: Partial<typeof draft>) => {
    setRuntimeNote("");
    setDraft((d) => ({ ...d, ...change }));
  };
  const runtime = registry.runtimes.find((r) => r.runtime_id === draft.runtime_id);
  const type = runtimeType(registry, runtime);
  const usable = registry.models.filter((m) => m.provider_enabled || m.model_id === profile?.model_id);
  const names = [...new Set(usable.map((m) => m.name))].sort((a, b) => a.toLowerCase().localeCompare(b.toLowerCase()));
  const protocolsFor = (name: string) => registry.limits.api_formats.flatMap((format) => format.protocols)
    .filter(({ protocol }) => usable.some((m) => m.name === name && m.protocol === protocol));
  const protocols = protocolsFor(draft.modelName);
  const offering = usable.filter((m) => m.name === draft.modelName && m.protocol === draft.protocol);
  const model = offering.find((m) => m.model_id === draft.model_id);
  const provider = registry.providers.find((p) => p.provider_id === model?.provider_id);
  const translated = model && !type?.protocols.includes(model.protocol) && type?.translated_protocols.includes(model.protocol);
  const nativeRuntimes = registry.runtime_types.filter((t) => t.protocols.includes(draft.protocol)).map((t) => t.label);
  const gatewayRuntimes = registry.runtime_types.filter((t) => t.translated_protocols.includes(draft.protocol)).map((t) => t.label);
  const noRuntime = model && !selectable.some((r) => !pairProblem(registry, r, model));
  const typeLabel = type?.label ?? runtime?.type_label ?? "";
  const effortLevels = type?.effort_levels ?? registry.limits.effort_levels;
  // Agent teams are a Claude Code feature: the server refuses them on another runtime.
  const teams = type?.type === "claude-code";
  const undetected = registry.runtime_types.filter((t) => !registry.runtimes.some((r) => r.builtin && r.type === t.type));

  const applyRuntime = (next: Runtime) => {
    const nextContract = runtimeType(registry, next);
    const nextType = nextContract?.type;
    const nextEfforts = nextContract?.effort_levels ?? registry.limits.effort_levels;
    // Another runtime type takes its default (on for Claude Code); back on the profile's own type, its saved setting.
    const agentTeams = nextType === type?.type ? draft.agent_teams
      : profile && profile.runtime_type === nextType ? profile.agent_teams : defaultTeams(registry, next);
    set({ runtime_id: next.runtime_id,
          effort: nextEfforts.includes(draft.effort) ? draft.effort : "",
          agent_teams: agentTeams });
  };
  const selectModel = (nextModel: Model | undefined) => {
    setModelError("");
    setRuntimeNote("");
    set({ model_id: nextModel?.model_id ?? "" });
    if (!nextModel) return;
    const reason = pairProblem(registry, runtime, nextModel);
    if (!reason) return;
    const next = selectable.find((r) => !pairProblem(registry, r, nextModel));
    if (next) {
      applyRuntime(next);
      setRuntimeNote(`Runtime changed to ${runtimeTitle(next)}: ${runtime ? runtimeTitle(runtime) : "Runtime"} ${reason}.`);
    }
  };
  const chooseModel = (modelName: string, protocol: string) => {
    const choices = usable.filter((m) => m.name === modelName && m.protocol === protocol);
    set({ modelName, protocol });
    selectModel(choices.length === 1 ? choices[0] : choices.find((m) => m.model_id === draft.model_id));
  };
  const chooseName = (modelName: string) => {
    const choices = protocolsFor(modelName).map((p) => p.protocol);
    chooseModel(modelName, choices.length === 1 ? choices[0] : choices.find((p) => p === draft.protocol) ?? "");
  };

  const save = async (e?: React.FormEvent) => {
    e?.preventDefault();
    if (!model) {
      setModelError("Choose a model, its protocol and a provider before saving.");
      return;
    }
    if (pairProblem(registry, runtime, model)) {
      setModelError("Choose a runtime that can run this model.");
      return;
    }
    setModelError("");
    const body = { name: draft.name.trim(), runtime_id: draft.runtime_id, model_id: model.model_id,
                   custom_prompt: draft.custom_prompt, effort: draft.effort || null,
                   fallback_profile_id: draft.fallback_profile_id || null,
                   agent_teams: teams && draft.agent_teams };
    let answer: { profile: AgentProfile; warnings: string[] } | null = null;
    const done = await action.run(async () => {
      answer = await (profile ? updateAgentProfile(profile.profile_id, body) : createAgentProfile(body));
    });
    const result = answer as { profile: AgentProfile; warnings: string[] } | null;
    if (!done || !result) return;
    // Saved; a warning (a model without a price) keeps the form open until it is read.
    if (result.warnings.length) {
      setWarnings(result.warnings);
      setSaved(result.profile);
    } else onDone(result.profile);
  };

  const fields = (
    <>
      <div className="grid gap-3 sm:grid-cols-2">
        <div className="space-y-1 sm:col-span-2">
          <label htmlFor={`${id}-name`} className={LABEL}>Name *</label>
          <Input id={`${id}-name`} value={draft.name} maxLength={registry.limits.name_max_chars} onChange={(e) => set({ name: e.target.value })} placeholder="claude-code/sonnet" spellCheck={false} autoCapitalize="none" className="font-mono" autoFocus />
        </div>
        <div className="space-y-1">
          <label htmlFor={`${id}-model`} className={LABEL}>Model *</label>
          <Input id={`${id}-model`} list={`${id}-model-names`} value={draft.modelName} onChange={(e) => chooseName(e.target.value)} placeholder="Type or choose a model" className="font-mono" aria-invalid={!!modelError} aria-describedby={modelProblem ? `${id}-model-error` : undefined} />
          <datalist id={`${id}-model-names`}>
            {names.map((name) => <option key={name} value={name} />)}
          </datalist>
          {!usable.length && <p className="text-[11px] text-muted-foreground">No compatible models. Add a model in <a href={settingsHref("providers")} className="underline underline-offset-2">LLM Providers</a>.</p>}
          {draft.modelName && !usable.some((m) => m.name === draft.modelName) && <p className="text-[11px] text-muted-foreground">No enabled provider offers {draft.modelName}. Add it under <a href={settingsHref("providers")} className="underline underline-offset-2">LLM Providers</a>: Models, or Fetch models.</p>}
          {modelProblem && <p id={`${id}-model-error`} role="alert" className="text-xs text-destructive">{modelProblem}</p>}
        </div>
        <div className="space-y-1">
          <label htmlFor={`${id}-protocol`} className={LABEL}>Protocol *</label>
          <select id={`${id}-protocol`} value={draft.protocol} disabled={!protocols.length} onChange={(e) => chooseModel(draft.modelName, e.target.value)} className={SELECT}>
            <option value="">Choose a protocol</option>
            {protocols.map((p) => <option key={p.protocol} value={p.protocol}>{p.label} ({p.protocol})</option>)}
          </select>
          {nativeRuntimes.length > 0 && <p className="text-[11px] text-muted-foreground">Runtimes that speak it: {nativeRuntimes.join(", ")}</p>}
          {gatewayRuntimes.length > 0 && <p className="text-[11px] text-muted-foreground">Runtimes that reach it through the local gateway: {gatewayRuntimes.join(", ")}</p>}
        </div>
        <div className="space-y-1">
          <label htmlFor={`${id}-provider`} className={LABEL}>Provider *</label>
          <select id={`${id}-provider`} value={model?.provider_id ?? ""} onChange={(e) => selectModel(offering.find((m) => m.provider_id === e.target.value))} className={SELECT} aria-describedby={`${id}-provider-hint`}>
            <option value="">Choose a provider</option>
            {offering.map((m) => <option key={m.provider_id} value={m.provider_id}>{m.provider_name}{m.provider_enabled ? "" : " (disabled)"}</option>)}
          </select>
          <div id={`${id}-provider-hint`} className="space-y-0.5 text-[11px] text-muted-foreground">
            {provider?.auth === "oauth" && (provider.login || type?.type === "claude-code") && (
              <LoginHint provider={provider} typeLabel={typeLabel} claude={login} />
            )}
            {provider?.enabled === false && <p className="text-warning">Provider {provider.name} is disabled: this profile&apos;s runs do not start.{" "}<a href={settingsHref("providers", provider.provider_id)} className="underline underline-offset-2">Enable it in LLM Providers</a>.</p>}
          </div>
        </div>
        <div className="min-w-0 space-y-1">
          <label htmlFor={`${id}-runtime`} className={LABEL}>Runtime</label>
          <select id={`${id}-runtime`} value={draft.runtime_id} onChange={(e) => {
            const next = selectable.find((r) => r.runtime_id === e.target.value);
            setRuntimeNote("");
            setModelError("");
            if (next) applyRuntime(next);
          }} className={`${SELECT} min-w-0 truncate`}>
            {selectable.map((r) => {
              const reason = model ? pairProblem(registry, r, model) : null;
              return <option key={r.runtime_id} value={r.runtime_id} disabled={r.builtin && !!reason}>
                {runtimeTitle(r)}{reason ? ` — ${reason}` : model && !runtimeType(registry, r)?.protocols.includes(model.protocol) && runtimeType(registry, r)?.translated_protocols.includes(model.protocol) ? " · via local gateway" : ""}
              </option>;
            })}
            {undetected.map((t) => {
              const hint = t.in_process ? t.probe_error || "not available" : "not on the server's PATH";
              return (
                <option key={t.type} value={t.runtime_id} disabled title={hint}>
                  {RUNTIME_TYPES[t.type]?.title ?? t.label} ({hint})
                </option>
              );
            })}
            {PLANNED_RUNTIMES.map((p) => (
              <option key={p.title} value="" disabled>{p.title} (planned)</option>
            ))}
          </select>
          {translated && !pairProblem(registry, runtime, model) && <p className="text-[11px] text-muted-foreground">Requests are translated to OpenAI Chat Completions through Looper’s local gateway. Runs are refused when the gateway is unavailable.</p>}
          {runtimeNote && <p className="text-[11px] text-muted-foreground">{runtimeNote}</p>}
          {noRuntime && <p role="alert" className="text-xs text-destructive">No runtime here can run this model.</p>}
        </div>
        <div className="space-y-1">
          <label htmlFor={`${id}-effort`} className={LABEL}>Effort</label>
          <select id={`${id}-effort`} value={draft.effort} onChange={(e) => set({ effort: e.target.value })} className={SELECT}>
            <option value="">Runtime default</option>
            {effortLevels.map((level) => (
              <option key={level} value={level}>{level}</option>
            ))}
          </select>
        </div>
        <div className="space-y-1 sm:col-span-2">
          <label htmlFor={`${id}-fallback`} className={LABEL}>Fallback to</label>
          <select id={`${id}-fallback`} value={draft.fallback_profile_id}
            onChange={(e) => set({ fallback_profile_id: e.target.value })} className={SELECT}>
            <option value="">No fallback</option>
            {registry.profiles.filter((p) => p.profile_id !== profile?.profile_id).map((p) => (
              <option key={p.profile_id} value={p.profile_id}
                disabled={(runtime?.roles ?? []).some((role) => !p.roles.includes(role))}>{profileLabel(p)}</option>
            ))}
          </select>
          <p className="text-[11px] text-muted-foreground">Used when this profile cannot start. At most three fallback hops.</p>
        </div>
        <p className="text-[11px] text-muted-foreground sm:col-span-2" data-testid="prompt-mode-per-role">
          The system prompt mode (append to the runtime&apos;s default or replace it) is set per role in Settings → Roles.
          {type?.replace_system_prompt_refusal && <> {type.replace_system_prompt_refusal}.</>}
        </p>
        {teams && (
          <div className="space-y-1 sm:col-span-2">
            <label className="flex items-center gap-1.5 text-xs">
              <input type="checkbox" className="h-4 w-4 accent-primary" checked={draft.agent_teams} onChange={(e) => set({ agent_teams: e.target.checked })} aria-describedby={`${id}-teams-hint`} />
              Agent teams
            </label>
            <p id={`${id}-teams-hint`} className="text-[11px] text-muted-foreground">
              On by default for the Manager, Workers and Reviewers: they can start teammates that work beside them
              (Claude Code&apos;s experimental agent teams), whose cost is counted in the run&apos;s once it ends.
              Planners always run without them, since a Planner&apos;s decisions come from its own turn. Untick it to
              run this profile without teammates.
            </p>
          </div>
        )}
        <div className="space-y-1 sm:col-span-2">
          <label htmlFor={`${id}-custom-prompt`} className={LABEL}>Custom prompt</label>
          <textarea
            id={`${id}-custom-prompt`}
            value={draft.custom_prompt}
            onChange={(e) => set({ custom_prompt: e.target.value })}
            rows={4}
            aria-describedby={`${id}-custom-prompt-hint`}
            className="w-full resize-y rounded-md border border-input bg-transparent p-3 font-mono text-sm text-foreground focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring"
          />
          <p id={`${id}-custom-prompt-hint`} className="text-[11px] text-muted-foreground">
            Text placed first in this profile&apos;s system prompt, before the harness. Empty adds nothing.
          </p>
        </div>
      </div>
      <p className="text-[11px] text-muted-foreground">
        Runs of this profile use this effort; to run a role with another effort, select or create a profile with it.
      </p>
      {warnings.length > 0 && (
        <div role="status" className="space-y-1 rounded-md border border-warning/40 bg-warning/10 px-3 py-2 text-xs">
          <p className="font-medium">Saved, with a warning:</p>
          {warnings.map((w) => (
            <p key={w}>{w}</p>
          ))}
          <Button type="button" size="sm" variant="outline" onClick={() => onDone(saved ?? undefined)}>OK</Button>
        </div>
      )}
      {!warnings.length && (
        <FormFooter
          error=""
          busy={action.busy}
          submit={profile ? "Save" : `Add ${AGENT_PROFILE.name}`}
          onCancel={() => onDone()}
          onSubmit={nested ? () => save() : undefined}
        />
      )}
    </>
  );

  const label = profile ? `Edit ${AGENT_PROFILE.name} ${profile.name}` : `New ${AGENT_PROFILE.name}`;
  return nested ? (
    <div role="group" aria-label={label} className="space-y-3">{fields}</div>
  ) : (
    <form onSubmit={save} aria-label={label} className="space-y-3">{fields}</form>
  );
}

// --- Where a profile is used ---

/** Each place that selects an Agent Profile, as a link to where it is changed (a project's Planner list by its name
 * only: the registry does not give its id). */
export function usedByPlaces(used: ProfileUsedBy): { text: string; href?: string }[] {
  const places: { text: string; href?: string }[] = used.layers.map((layer) => {
    const role = layer.role ? roleLabel(layer.role) : "";
    switch (layer.scope) {
      case "system":
        return { text: "the shared defaults", href: settingsHref("roles", "shared") };
      case "role":
        return { text: `the ${role}`, href: settingsHref("roles", layer.role ?? "") };
      case "project":
        return {
          text: `project ${layer.project_name}`,
          href: `#projects/${layer.project_id}/${AGENT_PROFILE.slug}/project`,
        };
      default:
        return {
          text: `the ${role} in ${layer.project_name}`,
          href: `#projects/${layer.project_id}/${AGENT_PROFILE.slug}/${layer.role ?? ""}`,
        };
    }
  });
  if (used.task_count) places.push({ text: `${used.task_count} ${used.task_count === 1 ? "task" : "tasks"}` });
  for (const project of used.allowed_in) places.push({ text: `the Planner's list in ${project}` });
  if (used.manager_session) places.push({ text: "the Manager's current session", href: "#chat" });
  for (const name of used.workflows ?? []) places.push({ text: `workflow ${name}`, href: "#settings/workflows" });
  return places;
}

function usedByText(used: ProfileUsedBy): string {
  return usedByPlaces(used).map((p) => p.text).join(", ");
}

function UsedBy({ used }: { used: ProfileUsedBy }) {
  const places = usedByPlaces(used);
  if (!places.length) return <p className="text-xs text-muted-foreground">Not used</p>;
  return (
    <p className="text-xs text-muted-foreground">
      Used by{" "}
      {places.map((place, i) => (
        <span key={place.text}>
          {i > 0 && ", "}
          {place.href ? (
            <a href={place.href} className="text-foreground underline-offset-2 hover:underline">{place.text}</a>
          ) : (
            <span className="text-foreground">{place.text}</span>
          )}
        </span>
      ))}
    </p>
  );
}

// --- The list ---

function SystemPromptPreview({ profile }: { profile: AgentProfile }) {
  const id = useId();
  const [role, setRole] = useState<AgentRole>(profile.roles[0] ?? "worker");
  const [preview, setPreview] = useState<PromptPreview | null>(null);
  const action = useAction();
  useEffect(() => setPreview(null), [profile]);
  const load = () => action.run(async () => {
    setPreview(null);
    setPreview(await fetchPromptPreview(profile.profile_id, role));
  });
  return (
    <details className="pt-2 text-xs">
      <summary className="cursor-pointer rounded-sm text-muted-foreground focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring">
        Preview system prompt
      </summary>
      <div className="mt-3 space-y-3">
        <div className="flex flex-wrap items-end gap-2">
          <div className="space-y-1">
            <label htmlFor={`${id}-role`} className={LABEL}>Role</label>
            <select
              id={`${id}-role`}
              value={role}
              onChange={(e) => { setRole(e.target.value as AgentRole); setPreview(null); }}
              disabled={action.busy || !profile.roles.length}
              className={SELECT}
            >
              {profile.roles.map((item) => <option key={item} value={item}>{roleLabel(item)}</option>)}
            </select>
          </div>
          <Button type="button" size="sm" variant="outline" onClick={load} disabled={action.busy || !profile.roles.length}>
            {action.busy && <Loader2 className="h-3.5 w-3.5 animate-spin" aria-hidden="true" />}Load
          </Button>
        </div>
        {action.error && <p role="alert" className="text-destructive">{action.error}</p>}
        {preview && (
          <>
            <ol className="space-y-3" aria-label="System prompt blocks">
              {preview.blocks.map((block, index) => (
                <li key={`${index}-${block.name}`} className="space-y-1">
                  <div className="flex flex-wrap items-center gap-2">
                    <span className="font-medium">{block.name}</span>
                    <Badge variant="outline">{block.owner === "looper" ? "Looper" : "Runtime"}</Badge>
                  </div>
                  {block.note && <p className="text-muted-foreground">{block.note}</p>}
                  {block.owner === "looper" && block.text !== null && (
                    <textarea
                      aria-label={block.name}
                      readOnly
                      value={block.text}
                      rows={8}
                      className="max-h-80 w-full resize-y overflow-auto rounded-md border border-input bg-transparent p-3 font-mono text-xs leading-relaxed text-foreground focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring"
                    />
                  )}
                </li>
              ))}
            </ol>
            {preview.notes.map((note, index) => <p key={index} className="text-muted-foreground">{note}</p>)}
          </>
        )}
      </div>
    </details>
  );
}

/** How a profile's runs are priced, in words. */
function costBasisText(profile: AgentProfile): string {
  if (profile.cost.source === "runtime") {
    return profile.cost.equivalent ? "cost: the runtime's figure, a subscription equivalent" : "cost: the runtime's figure";
  }
  if (profile.cost.source !== "provider_price") return "";
  return profile.cost.equivalent ? "cost: tokens at API prices, a subscription equivalent" : "cost: tokens at the provider's prices";
}

function AgentProfileRow({ profile, registry }: { profile: AgentProfile; registry: Registry }) {
  const [editing, setEditing] = useState(false);
  const ref = useSettingsTarget<HTMLLIElement>("profile", profile.profile_id, () => setEditing(!profile.builtin));
  const action = useAction();
  const remove = () => {
    if (confirm(`Delete ${AGENT_PROFILE.name} "${profile.name}"?`)) action.run(() => deleteAgentProfile(profile.profile_id));
  };
  const runtime = registry.runtimes.find((r) => r.runtime_id === profile.runtime_id);
  const fallbackUsers = registry.profiles.filter((p) => p.fallback_profile_id === profile.profile_id);
  const inUse = [usedByText(profile.used_by), fallbackUsers.length
    ? `fallback of ${fallbackUsers.map((p) => p.name).join(", ")}` : ""].filter(Boolean).join(", ");
  return (
    <Row
      ref={ref}
      title={<><RowName name={profile.name} builtin={profile.builtin} />{profile.model_path === "translated" && <Badge variant="outline">via local gateway</Badge>}</>}
      actions={
        !profile.builtin && (
          <EditDelete
            what={AGENT_PROFILE.name}
            name={profile.name}
            editing={editing}
            busy={action.busy}
            onEdit={() => setEditing(!editing)}
            onDelete={remove}
            deleteBlocked={inUse ? `in use by ${inUse}` : undefined}
          />
        )
      }
      below={
        <>
          {action.error && <p role="alert" className="text-xs text-destructive">{action.error}</p>}
          <SystemPromptPreview profile={profile} />
          {editing && (
            <div className={EDIT_BOX}>
              <AgentProfileForm profile={profile} registry={registry} onDone={() => setEditing(false)} />
            </div>
          )}
        </>
      }
    >
      <p className="break-words text-xs text-muted-foreground">
        {runtime ? runtimeTitle(runtime) : profile.runtime_name} · {profile.provider.name} · model{" "}
        <span className="font-mono text-foreground/80">{profile.model.name} / {profile.model.protocol}</span> · effort {profile.effort ?? "runtime default"}
        {profile.agent_teams && <> · <span className="text-foreground">agent teams on</span></>}
        {costBasisText(profile) && ` · ${costBasisText(profile)}`}
      </p>
      <p className="text-xs text-muted-foreground">
        Can run{" "}
        <span className="text-foreground">
          {profile.roles.length ? profile.roles.map(roleLabel).join(", ") : "no role"}
        </span>
      </p>
      {profile.fallback_profile_id && <p className="text-xs text-muted-foreground">
        Fallback to {registry.profiles.find((p) => p.profile_id === profile.fallback_profile_id)?.name ?? profile.fallback_profile_id}
      </p>}
      <UsedBy used={profile.used_by} />
      {profile.provider.enabled === false && (
        <p className="flex items-start gap-1 text-xs text-warning">
          <AlertTriangle className="mt-0.5 h-3.5 w-3.5 shrink-0" aria-hidden="true" />
          <span>Provider {profile.provider.name} is disabled: this profile&apos;s runs do not start.{" "}
            <a href={settingsHref("providers", profile.provider.provider_id)} className="underline underline-offset-2">Enable it in LLM Providers</a>.
          </span>
        </p>
      )}
      {profile.cost.unpriced.length > 0 && (
        <p className="flex items-start gap-1 text-xs text-warning">
          <AlertTriangle className="mt-0.5 h-3.5 w-3.5 shrink-0" />
          No price for {profile.cost.unpriced.join(", ")}: its runs record tokens only and spend caps do not apply. Add
          the prices to the provider&apos;s model catalog.
        </p>
      )}
    </Row>
  );
}

/** Settings → Agent Profiles: every profile, what it is made of, the roles it can run and where it is used. */
export function AgentProfilesSection() {
  const { registry, error } = useRegistry();
  if (!registry) {
    return error ? (
      <div role="alert" className="rounded-lg border border-destructive/50 bg-destructive/10 px-5 py-4 text-sm text-destructive">{error}</div>
    ) : (
      <div className="flex h-32 items-center justify-center">
        <Loader2 className="h-6 w-6 animate-spin text-muted-foreground" aria-label="Loading" />
      </div>
    );
  }
  return (
    <RegistryList
      title={AGENT_PROFILE.plural}
      hint="Selected in Settings → Roles, a project's Agent Profiles tab and a new task."
      addLabel={`Add ${AGENT_PROFILE.name}`}
      defaultAdding={window.location.hash === settingsHref("profiles", "new")}
      form={(close) => <AgentProfileForm profile={null} registry={registry} onDone={close} />}
    >
      {registry.profiles.map((profile) => (
        <AgentProfileRow key={profile.profile_id} profile={profile} registry={registry} />
      ))}
    </RegistryList>
  );
}

// --- The dropdown ---

const NEW_PROFILE = "__new_profile__";

/**
 * Choose an Agent Profile, or "+ New Agent Profile", which opens the form below.
 * Creating registers it without changing the current role or task selection.
 * A profile whose runtime cannot run one of `roles` is listed but disabled.
 */
export function AgentProfileSelect({
  id,
  disabled = false,
  value,
  onChange,
  emptyLabel,
  roles = [],
  describedBy,
  unlistedName,
  className = SELECT,
}: {
  id: string;
  disabled?: boolean;
  value: string;
  onChange: (profileId: string) => void;
  emptyLabel: string;
  roles?: AgentRole[];
  describedBy?: string;
  /** What to call a selected profile the registry does not list (yet). */
  unlistedName?: string;
  className?: string;
}) {
  const { registry, error } = useRegistry();
  const [creating, setCreating] = useState(false);
  const profiles = registry?.profiles ?? [];
  const unlisted = value && !profiles.some((p) => p.profile_id === value) ? unlistedName || value : "";

  return (
    <div className="space-y-2">
      <select
        id={id}
        disabled={disabled}
        value={value}
        onChange={(e) => (e.target.value === NEW_PROFILE ? setCreating(true) : onChange(e.target.value))}
        aria-describedby={describedBy}
        className={className}
      >
        <option value="">{emptyLabel}</option>
        {profiles.map((profile) => {
          const missing = roles.filter((role) => !profile.roles.includes(role));
          return (
            <option key={profile.profile_id} value={profile.profile_id} disabled={missing.length > 0 && profile.profile_id !== value}>
              {profileLabel(profile)}
              {missing.length > 0 ? ` (cannot run the ${missing.map(roleLabel).join(", ")})` : ""}
            </option>
          );
        })}
        {unlisted && <option value={value}>{unlisted}</option>}
        {registry && <option value={NEW_PROFILE}>+ New {AGENT_PROFILE.name}</option>}
      </select>
      {error && <p className="text-xs text-destructive">Could not load the {AGENT_PROFILE.plural}: {error}</p>}
      {creating && registry && (
        <div className={EDIT_BOX}>
          <AgentProfileForm
            profile={null}
            registry={registry}
            nested
            onDone={() => setCreating(false)}
          />
        </div>
      )}
    </div>
  );
}
