import { useEffect, useId, useState } from "react";
import { AlertTriangle, Loader2 } from "lucide-react";
import { Button } from "@/components/ui/button";
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
  fetchStatus,
  updateAgentProfile,
  type AgentProfile,
  type AgentRole,
  type ProfileUsedBy,
  type Provider,
  type ProviderLogin,
  type Registry,
  type Runtime,
  type SelfCheck,
} from "@/lib/api";
import { PLANNED_RUNTIMES, RUNTIME_TYPES, roleLabel } from "@/lib/runtime-catalog";
import { AGENT_PROFILE, settingsHref } from "@/lib/settings-pages";
import { useAction } from "@/lib/use-action";
import { profileLabel, useRegistry } from "@/lib/use-registry";

// --- What a runtime can be paired with ---

function runtimeType(registry: Registry, runtime: Runtime | undefined) {
  return registry.runtime_types.find((t) => t.type === runtime?.type);
}

/** Whether `provider` can serve `runtime`: a protocol in common, and an OAuth provider only for the runtime type
 * whose login it is. The server checks the same (and more) when the Agent Profile is saved. */
function pairs(registry: Registry, runtime: Runtime | undefined, provider: Provider): boolean {
  const type = runtimeType(registry, runtime);
  if (!type || !provider.protocols.some((p) => type.protocols.includes(p))) return false;
  if (provider.auth === "oauth") return type.auth_kinds.includes("oauth") && provider.oauth_runtime_type === type.type;
  return type.auth_kinds.includes("api_key");
}

/** The provider a runtime starts with: its own login when it has one Looper uses, else the first API-key one. */
function firstProvider(registry: Registry, runtime: Runtime | undefined): string {
  const fitting = registry.providers.filter((p) => pairs(registry, runtime, p));
  return (fitting.find((p) => p.auth === "oauth") ?? fitting[0])?.provider_id ?? "";
}

/** A model the provider lists: the current one if it is there, else its first. A provider that lists none takes
 * any model; a runtime login that suggests some (Codex's) starts from its first unless the current one is one. */
function fittingModel(provider: Provider | undefined, current: string): string {
  if (provider?.models.length) return provider.models.some((m) => m.name === current) ? current : provider.models[0].name;
  const suggested = provider?.suggested_models ?? [];
  return suggested.length && !suggested.includes(current) ? suggested[0] : current;
}

/** Agent teams for a profile on `runtime` that does not say: on for Claude Code, whose feature they are, off for
 * any other runtime. The server decides the same way for a profile saved without them. */
function defaultTeams(registry: Registry, runtime: Runtime | undefined): boolean {
  return runtimeType(registry, runtime)?.type === "claude-code";
}

function runtimeTitle(runtime: Runtime): string {
  if (!runtime.builtin) return `${runtime.name} (custom, legacy)`;
  return RUNTIME_TYPES[runtime.type]?.title ?? runtime.type_label;
}

// --- The Claude Code login, as the startup self-check found it ---

let statusChecks: Promise<SelfCheck[]> | null = null;

/** The startup self-check's Claude login result (`claude auth status`), fetched once per page load: undefined
 * while it loads, null when the server has none. */
function useClaudeLogin(): SelfCheck | null | undefined {
  const [check, setCheck] = useState<SelfCheck | null | undefined>(undefined);
  useEffect(() => {
    statusChecks ??= fetchStatus().then((s) => s.checks ?? []).catch(() => []);
    let alive = true;
    statusChecks.then((checks) => alive && setCheck(checks.find((c) => c.id === "claude_login") ?? null));
    return () => {
      alive = false;
    };
  }, []);
  return check;
}

function loginText(check: SelfCheck | null | undefined): string {
  if (check === undefined) return "checking the login…";
  if (!check) return "login not checked";
  if (check.status === "ok") return check.detail;
  if (check.status === "fail") return "not logged in: run claude auth login";
  return check.detail;
}

/** A runtime login the registry reports (Codex's `codex login status`, or a fork a run found), in words. */
function providerLoginText(login: ProviderLogin | null): string {
  if (!login) return "login not checked";
  return login.state === "unavailable" ? `unavailable ${login.detail}` : login.detail;
}

/** The provider's login under the form: what it is now and, when it cannot run, how to fix it. */
function LoginHint({ provider, typeLabel, claude }: { provider: Provider; typeLabel: string; claude?: SelfCheck | null }) {
  const own = provider.login;
  const text = own ? providerLoginText(own) : loginText(claude);
  const failing = own ? own.state !== "chatgpt" : claude?.status === "fail";
  let fix: React.ReactNode = null;
  if (!own && failing) {
    fix = <>(<code className="font-mono">claude auth login</code>, then restart looper serve)</>;
  } else if (own?.state === "unavailable") {
    fix = <>(check <code className="font-mono">codex login status</code> on the server&apos;s machine, then{" "}
      <a href={settingsHref("providers", provider.provider_id)} className="underline underline-offset-2">use the login again</a>)</>;
  } else if (own && failing) {
    fix = <>(<code className="font-mono">codex login</code> with ChatGPT on the server&apos;s machine)</>;
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

const NEW_PROVIDER = "__new_provider__";

/**
 * Create or edit an Agent Profile: a name, then Runtime → Provider (its own login, or an API-key provider that
 * speaks its protocol) → Model (from the provider's catalog, or any), and a default effort. `nested` renders it
 * without a form element of its own, for use inside another form; `onDone` gets the saved profile, or nothing
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
    const providerId = profile?.provider_id ?? firstProvider(registry, runtime);
    const provider = registry.providers.find((p) => p.provider_id === providerId);
    return {
      name: profile?.name ?? "",
      runtime_id: runtime?.runtime_id ?? "",
      provider_id: providerId,
      model: profile?.model ?? fittingModel(provider, ""),
      effort: profile?.effort ?? "",
      agent_teams: profile?.agent_teams ?? defaultTeams(registry, runtime),
    };
  });
  const action = useAction();
  const [warnings, setWarnings] = useState<string[]>([]);
  const [saved, setSaved] = useState<AgentProfile | null>(null);
  const set = (change: Partial<typeof draft>) => setDraft((d) => ({ ...d, ...change }));
  const runtime = registry.runtimes.find((r) => r.runtime_id === draft.runtime_id);
  const type = runtimeType(registry, runtime);
  const provider = registry.providers.find((p) => p.provider_id === draft.provider_id);
  const fitting = registry.providers.filter((p) => pairs(registry, runtime, p));
  const ownLogins = fitting.filter((p) => p.auth === "oauth");
  const keyProviders = fitting.filter((p) => p.auth === "api_key");
  const typeLabel = type?.label ?? runtime?.type_label ?? "";
  // Agent teams are a Claude Code feature: the server refuses them on another runtime.
  const teams = type?.type === "claude-code";
  const undetected = registry.runtime_types.filter((t) => !registry.runtimes.some((r) => r.builtin && r.type === t.type));

  const chooseRuntime = (runtimeId: string) => {
    const next = registry.runtimes.find((r) => r.runtime_id === runtimeId);
    const providerId = firstProvider(registry, next);
    const nextType = runtimeType(registry, next)?.type;
    // Another runtime type takes its default (on for Claude Code); back on the profile's own type, its saved setting.
    const agentTeams = nextType === type?.type ? draft.agent_teams
      : profile && profile.runtime_type === nextType ? profile.agent_teams : defaultTeams(registry, next);
    set({ runtime_id: runtimeId, provider_id: providerId,
          model: fittingModel(registry.providers.find((p) => p.provider_id === providerId), draft.model),
          agent_teams: agentTeams });
  };
  const chooseProvider = (providerId: string) => {
    if (providerId === NEW_PROVIDER) {
      window.location.hash = settingsHref("providers", "new");
      return;
    }
    set({ provider_id: providerId,
          model: fittingModel(registry.providers.find((p) => p.provider_id === providerId), draft.model) });
  };

  const save = async (e?: React.FormEvent) => {
    e?.preventDefault();
    const body = { ...draft, name: draft.name.trim(), model: draft.model.trim(), effort: draft.effort || null,
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
          <label htmlFor={`${id}-runtime`} className={LABEL}>Runtime</label>
          <select id={`${id}-runtime`} value={draft.runtime_id} onChange={(e) => chooseRuntime(e.target.value)} className={SELECT}>
            {selectable.map((r) => (
              <option key={r.runtime_id} value={r.runtime_id}>{runtimeTitle(r)}</option>
            ))}
            {undetected.map((t) => (
              <option key={t.type} value={t.runtime_id} disabled>
                {RUNTIME_TYPES[t.type]?.title ?? t.label} (not on the server&apos;s PATH)
              </option>
            ))}
            {PLANNED_RUNTIMES.map((p) => (
              <option key={p.title} value="" disabled>{p.title} (planned)</option>
            ))}
          </select>
        </div>
        <div className="space-y-1">
          <label htmlFor={`${id}-provider`} className={LABEL}>Provider</label>
          <select id={`${id}-provider`} value={draft.provider_id} onChange={(e) => chooseProvider(e.target.value)} className={SELECT} aria-describedby={`${id}-provider-hint`}>
            {!provider && <option value="">Choose a provider</option>}
            {type?.auth_kinds.includes("oauth") ? (
              <optgroup label={`${typeLabel} login (OAuth)`}>
                {ownLogins.map((p) => (
                  <option key={p.provider_id} value={p.provider_id}>
                    {p.name}
                    {p.login ? ` (${providerLoginText(p.login)})` : type.type === "claude-code" ? ` (${loginText(login)})` : ""}
                  </option>
                ))}
              </optgroup>
            ) : null}
            <optgroup label="API key">
              {keyProviders.map((p) => (
                <option key={p.provider_id} value={p.provider_id}>{p.name}</option>
              ))}
              <option value={NEW_PROVIDER}>+ New provider (Settings → LLM Providers)</option>
            </optgroup>
          </select>
        </div>
        <div className="space-y-1">
          <label htmlFor={`${id}-model`} className={LABEL}>Model *</label>
          {provider?.models.length ? (
            <select id={`${id}-model`} value={draft.model} onChange={(e) => set({ model: e.target.value })} className={`${SELECT} font-mono`}>
              {provider.models.map((m) => (
                <option key={m.name} value={m.name}>{m.name}</option>
              ))}
            </select>
          ) : (
            <>
              <Input id={`${id}-model`} value={draft.model} onChange={(e) => set({ model: e.target.value })} placeholder={provider?.suggested_models[0] ?? "sonnet"} list={provider?.suggested_models.length ? `${id}-suggested` : undefined} spellCheck={false} autoCapitalize="none" className="font-mono" />
              {provider?.suggested_models.length ? (
                <datalist id={`${id}-suggested`}>
                  {provider.suggested_models.map((m) => (
                    <option key={m} value={m} />
                  ))}
                </datalist>
              ) : null}
            </>
          )}
        </div>
        <div className="space-y-1">
          <label htmlFor={`${id}-effort`} className={LABEL}>Default effort</label>
          <select id={`${id}-effort`} value={draft.effort} onChange={(e) => set({ effort: e.target.value })} className={SELECT}>
            <option value="">Runtime default</option>
            {registry.limits.effort_levels.map((level) => (
              <option key={level} value={level}>{level}</option>
            ))}
          </select>
        </div>
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
      </div>
      <div id={`${id}-provider-hint`} className="space-y-0.5 text-[11px] text-muted-foreground">
        {provider?.auth === "oauth" && (provider.login || type?.type === "claude-code") && (
          <LoginHint provider={provider} typeLabel={typeLabel} claude={login} />
        )}
        <p>
          {provider?.models.length
            ? `${provider.name} lists ${provider.models.map((m) => m.name).join(", ")}.`
            : provider?.suggested_models.length
              ? `Any model the login offers, such as ${provider.suggested_models.slice(0, 3).join(", ")}.`
              : "The model as the runtime takes it: an alias such as sonnet or a full name."}{" "}
          A role&apos;s effort overrides the default.
        </p>
      </div>
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
          error={action.error}
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

/** Each place that selects an Agent Profile, as a link to where it is changed. */
function usedByPlaces(used: ProfileUsedBy): { text: string; href?: string }[] {
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
  const action = useAction();
  const remove = () => {
    if (confirm(`Delete ${AGENT_PROFILE.name} "${profile.name}"?`)) action.run(() => deleteAgentProfile(profile.profile_id));
  };
  const runtime = registry.runtimes.find((r) => r.runtime_id === profile.runtime_id);
  const inUse = usedByText(profile.used_by);
  return (
    <Row
      title={<RowName name={profile.name} builtin={profile.builtin} />}
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
          {editing && (
            <div className={EDIT_BOX}>
              <AgentProfileForm profile={profile} registry={registry} onDone={() => setEditing(false)} />
            </div>
          )}
        </>
      }
    >
      <p className="break-words text-xs text-muted-foreground">
        {runtime ? runtimeTitle(runtime) : profile.runtime_name} · {profile.provider_name} · model{" "}
        <span className="font-mono text-foreground/80">{profile.model}</span> · effort {profile.effort ?? "runtime default"}
        {profile.agent_teams && <> · <span className="text-foreground">agent teams on</span></>}
        {costBasisText(profile) && ` · ${costBasisText(profile)}`}
      </p>
      <p className="text-xs text-muted-foreground">
        Can run{" "}
        <span className="text-foreground">
          {profile.roles.length ? profile.roles.map(roleLabel).join(", ") : "no role"}
        </span>
      </p>
      <UsedBy used={profile.used_by} />
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
 * Choose an Agent Profile, or "+ New Agent Profile", which opens the form below and selects what it saves.
 * A profile whose runtime cannot run one of `roles` is listed but disabled.
 */
export function AgentProfileSelect({
  id,
  value,
  onChange,
  emptyLabel,
  roles = [],
  describedBy,
  unlistedName,
  className = SELECT,
}: {
  id: string;
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
            onDone={(saved) => {
              setCreating(false);
              if (saved) onChange(saved.profile_id);
            }}
          />
        </div>
      )}
    </div>
  );
}
