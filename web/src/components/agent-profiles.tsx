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
  createRegistryAgent,
  deleteRegistryAgent,
  fetchStatus,
  updateRegistryAgent,
  type AgentRole,
  type AgentUsedBy,
  type Provider,
  type Registry,
  type RegistryAgent,
  type Runtime,
  type SelfCheck,
} from "@/lib/api";
import { PLANNED_RUNTIMES, RUNTIME_TYPES, roleLabel } from "@/lib/runtime-catalog";
import { AGENT_PROFILE, settingsHref } from "@/lib/settings-pages";
import { useAction } from "@/lib/use-action";
import { agentLabel, useRegistry } from "@/lib/use-registry";

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

/** A model the provider lists: the current one if it is there, else its first; any model when it lists none. */
function fittingModel(provider: Provider | undefined, current: string): string {
  if (!provider?.models.length || provider.models.some((m) => m.name === current)) return current;
  return provider.models[0].name;
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
  profile: RegistryAgent | null;
  registry: Registry;
  onDone: (saved?: RegistryAgent) => void;
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
    };
  });
  const action = useAction();
  const [warnings, setWarnings] = useState<string[]>([]);
  const [saved, setSaved] = useState<RegistryAgent | null>(null);
  const set = (change: Partial<typeof draft>) => setDraft((d) => ({ ...d, ...change }));
  const runtime = registry.runtimes.find((r) => r.runtime_id === draft.runtime_id);
  const type = runtimeType(registry, runtime);
  const provider = registry.providers.find((p) => p.provider_id === draft.provider_id);
  const fitting = registry.providers.filter((p) => pairs(registry, runtime, p));
  const ownLogins = fitting.filter((p) => p.auth === "oauth");
  const keyProviders = fitting.filter((p) => p.auth === "api_key");
  const info = RUNTIME_TYPES[runtime?.type ?? ""];
  const typeLabel = type?.label ?? runtime?.type_label ?? "";
  const undetected = registry.runtime_types.filter((t) => !registry.runtimes.some((r) => r.builtin && r.type === t.type));

  const chooseRuntime = (runtimeId: string) => {
    const next = registry.runtimes.find((r) => r.runtime_id === runtimeId);
    const providerId = firstProvider(registry, next);
    set({ runtime_id: runtimeId, provider_id: providerId,
          model: fittingModel(registry.providers.find((p) => p.provider_id === providerId), draft.model) });
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
    const body = { ...draft, name: draft.name.trim(), model: draft.model.trim(), effort: draft.effort || null };
    let answer: { agent: RegistryAgent; warnings: string[] } | null = null;
    const done = await action.run(async () => {
      answer = await (profile ? updateRegistryAgent(profile.agent_id, body) : createRegistryAgent(body));
    });
    const result = answer as { agent: RegistryAgent; warnings: string[] } | null;
    if (!done || !result) return;
    // Saved; a warning (a model without a price) keeps the form open until it is read.
    if (result.warnings.length) {
      setWarnings(result.warnings);
      setSaved(result.agent);
    } else onDone(result.agent);
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
                    {type.type === "claude-code" ? ` (${loginText(login)})` : ""}
                  </option>
                ))}
              </optgroup>
            ) : info?.loginComing ? (
              <optgroup label={`${typeLabel} login (OAuth)`}>
                <option value="" disabled>{typeLabel} login: {info.loginComing}</option>
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
            <Input id={`${id}-model`} value={draft.model} onChange={(e) => set({ model: e.target.value })} placeholder="sonnet" spellCheck={false} autoCapitalize="none" className="font-mono" />
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
      </div>
      <div id={`${id}-provider-hint`} className="space-y-0.5 text-[11px] text-muted-foreground">
        {provider?.auth === "oauth" && type?.type === "claude-code" && (
          <p className={login?.status === "fail" ? "flex items-start gap-1 text-warning" : undefined}>
            {login?.status === "fail" && <AlertTriangle className="mt-0.5 h-3 w-3 shrink-0" aria-hidden="true" />}
            {typeLabel} login: {loginText(login)}
            {login?.status === "fail" && (
              <>
                {" "}(<code className="font-mono">claude auth login</code>, then restart looper serve)
              </>
            )}
          </p>
        )}
        <p>
          {provider?.models.length
            ? `${provider.name} lists ${provider.models.map((m) => m.name).join(", ")}.`
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
function usedByPlaces(used: AgentUsedBy): { text: string; href?: string }[] {
  const places: { text: string; href?: string }[] = used.layers.map((layer) => {
    const role = layer.role ? roleLabel(layer.role) : "";
    switch (layer.scope) {
      case "system":
        return { text: "every role (defaults)", href: settingsHref("roles") };
      case "role":
        return { text: `the ${role}`, href: settingsHref("roles", layer.role ?? "") };
      case "project":
        return { text: `project ${layer.project_name}`, href: `#projects/${layer.project_id}/settings` };
      default:
        return { text: `the ${role} in ${layer.project_name}`, href: `#projects/${layer.project_id}/settings` };
    }
  });
  if (used.task_count) places.push({ text: `${used.task_count} ${used.task_count === 1 ? "task" : "tasks"}` });
  return places;
}

function usedByText(used: AgentUsedBy): string {
  return usedByPlaces(used).map((p) => p.text).join(", ");
}

function UsedBy({ used }: { used: AgentUsedBy }) {
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

/** How an agent's runs are priced, in words. */
function costBasisText(agent: RegistryAgent): string {
  if (agent.cost.source === "runtime") {
    return agent.cost.equivalent ? "cost: the runtime's figure, a subscription equivalent" : "cost: the runtime's figure";
  }
  return agent.cost.source === "provider_price" ? "cost: tokens at the provider's prices" : "";
}

function AgentProfileRow({ agent, registry }: { agent: RegistryAgent; registry: Registry }) {
  const [editing, setEditing] = useState(false);
  const action = useAction();
  const remove = () => {
    if (confirm(`Delete ${AGENT_PROFILE.name} "${agent.name}"?`)) action.run(() => deleteRegistryAgent(agent.agent_id));
  };
  const runtime = registry.runtimes.find((r) => r.runtime_id === agent.runtime_id);
  const inUse = usedByText(agent.used_by);
  return (
    <Row
      title={<RowName name={agent.name} builtin={agent.builtin} />}
      actions={
        !agent.builtin && (
          <EditDelete
            what={AGENT_PROFILE.name}
            name={agent.name}
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
              <AgentProfileForm profile={agent} registry={registry} onDone={() => setEditing(false)} />
            </div>
          )}
        </>
      }
    >
      <p className="break-words text-xs text-muted-foreground">
        {runtime ? runtimeTitle(runtime) : agent.runtime_name} · {agent.provider_name} · model{" "}
        <span className="font-mono text-foreground/80">{agent.model}</span> · effort {agent.effort ?? "runtime default"}
        {costBasisText(agent) && ` · ${costBasisText(agent)}`}
      </p>
      <p className="text-xs text-muted-foreground">
        Can run{" "}
        <span className="text-foreground">
          {agent.roles.length ? agent.roles.map(roleLabel).join(", ") : "no role"}
        </span>
      </p>
      <UsedBy used={agent.used_by} />
      {agent.cost.unpriced.length > 0 && (
        <p className="flex items-start gap-1 text-xs text-warning">
          <AlertTriangle className="mt-0.5 h-3.5 w-3.5 shrink-0" />
          No price for {agent.cost.unpriced.join(", ")}: its runs record tokens only and spend caps do not apply. Add
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
      hint="Selected in Settings → Roles, a project's settings and a new task."
      addLabel={`Add ${AGENT_PROFILE.name}`}
      form={(close) => <AgentProfileForm profile={null} registry={registry} onDone={close} />}
    >
      {registry.agents.map((agent) => (
        <AgentProfileRow key={agent.agent_id} agent={agent} registry={registry} />
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
  onChange: (agentId: string) => void;
  emptyLabel: string;
  roles?: AgentRole[];
  describedBy?: string;
  /** What to call a selected profile the registry does not list (yet). */
  unlistedName?: string;
  className?: string;
}) {
  const { registry, error } = useRegistry();
  const [creating, setCreating] = useState(false);
  const profiles = registry?.agents ?? [];
  const unlisted = value && !profiles.some((a) => a.agent_id === value) ? unlistedName || value : "";

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
        {profiles.map((agent) => {
          const missing = roles.filter((role) => !agent.roles.includes(role));
          return (
            <option key={agent.agent_id} value={agent.agent_id} disabled={missing.length > 0 && agent.agent_id !== value}>
              {agentLabel(agent)}
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
              if (saved) onChange(saved.agent_id);
            }}
          />
        </div>
      )}
    </div>
  );
}
