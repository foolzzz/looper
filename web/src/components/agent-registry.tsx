import { useId, useState } from "react";
import { AlertTriangle, Loader2, Pencil, Plus, RefreshCw, Trash2, X } from "lucide-react";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import {
  createProvider,
  createRegistryAgent,
  createRuntime,
  deleteProvider,
  deleteRegistryAgent,
  deleteRuntime,
  probeRuntime,
  updateProvider,
  updateRegistryAgent,
  updateRuntime,
  type PriceField,
  type Provider,
  type ProviderAuth,
  type ProviderFields,
  type ProviderModel,
  type ProviderPreset,
  type Registry,
  type RegistryAgent,
  type Runtime,
} from "@/lib/api";
import { formatUsd } from "@/lib/usage";
import { useRegistry } from "@/lib/use-registry";
import { useSettingsTarget } from "@/lib/settings-target";

const SELECT = "flex h-9 w-full rounded-md border border-input bg-transparent px-3 py-1 text-sm text-foreground";
const LABEL = "text-xs font-medium text-muted-foreground";
const SMALL_LABEL = "text-[11px] text-muted-foreground";

const errorText = (err: unknown) => (err instanceof Error ? err.message : String(err));
const usedBy = (count: number) => (count === 0 ? "no agents" : `used by ${count} ${count === 1 ? "agent" : "agents"}`);

/** One request at a time, with the error it ended with; `run` resolves to whether it succeeded. */
function useAction() {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const run = async (action: () => Promise<unknown>) => {
    setBusy(true);
    setError("");
    try {
      await action();
      return true;
    } catch (err) {
      setError(errorText(err));
      return false;
    } finally {
      setBusy(false);
    }
  };
  return { busy, error, run };
}

/** A titled list with an add form that opens above its rows. */
function RegistryList({
  title,
  hint,
  addLabel,
  form,
  children,
}: {
  title: string;
  hint: React.ReactNode;
  addLabel: string;
  form: (close: () => void) => React.ReactNode;
  children: React.ReactNode;
}) {
  const id = useId();
  const [adding, setAdding] = useState(false);
  return (
    <section aria-labelledby={`${id}-title`} className="rounded-lg border border-border bg-card">
      <div className="flex flex-wrap items-center gap-x-3 gap-y-1 px-5 py-3">
        <h3 id={`${id}-title`} className="text-sm font-semibold">{title}</h3>
        <p className="min-w-0 flex-1 text-[11px] text-muted-foreground">{hint}</p>
        <Button type="button" size="sm" variant="outline" onClick={() => setAdding(!adding)} aria-expanded={adding}>
          <Plus className="h-3.5 w-3.5" /> {addLabel}
        </Button>
      </div>
      {adding && <div className="border-t border-border bg-muted/20 px-5 py-3">{form(() => setAdding(false))}</div>}
      <ul className="divide-y divide-border border-t border-border">{children}</ul>
    </section>
  );
}

/** One registered entry: its name and badges, its actions, its details, then any error and its edit form. */
function Row({
  title,
  actions,
  children,
  below,
  ref,
}: {
  title: React.ReactNode;
  actions: React.ReactNode;
  children: React.ReactNode;
  below: React.ReactNode;
  ref?: React.Ref<HTMLLIElement>;
}) {
  return (
    <li ref={ref} className="space-y-1 px-5 py-3">
      <div className="flex flex-wrap items-center gap-x-2 gap-y-1">
        {title}
        <div className="ml-auto flex flex-wrap gap-1">{actions}</div>
      </div>
      {children}
      {below}
    </li>
  );
}

function RowName({ name, builtin, kind }: { name: string; builtin: boolean; kind?: string }) {
  return (
    <>
      <span className="break-all text-sm font-medium">{name}</span>
      {builtin && <Badge variant="outline">built-in</Badge>}
      {kind && <span className="text-xs text-muted-foreground">{kind}</span>}
    </>
  );
}

/** Edit and Delete for an entry that is not built in. */
function EditDelete({
  what,
  name,
  editing,
  busy,
  onEdit,
  onDelete,
}: {
  what: string;
  name: string;
  editing: boolean;
  busy: boolean;
  onEdit: () => void;
  onDelete: () => void;
}) {
  return (
    <>
      <Button type="button" variant="ghost" size="sm" onClick={onEdit} aria-expanded={editing} aria-label={`Edit ${what} ${name}`}>
        <Pencil className="h-3.5 w-3.5" /> Edit
      </Button>
      <Button
        type="button"
        variant="ghost"
        size="sm"
        onClick={onDelete}
        disabled={busy}
        aria-label={`Delete ${what} ${name}`}
        className="text-muted-foreground hover:bg-destructive/10 hover:text-destructive"
      >
        <Trash2 className="h-3.5 w-3.5" /> Delete
      </Button>
    </>
  );
}

function FormFooter({ error, busy, submit, onCancel }: { error: string; busy: boolean; submit: string; onCancel: () => void }) {
  return (
    <>
      {error && <p role="alert" className="text-xs text-destructive">{error}</p>}
      <div className="flex gap-2">
        <Button type="submit" size="sm" disabled={busy}>
          {busy && <Loader2 className="h-3.5 w-3.5 animate-spin" />}
          {submit}
        </Button>
        <Button type="button" size="sm" variant="ghost" onClick={onCancel} disabled={busy}>
          Cancel
        </Button>
      </div>
    </>
  );
}

const EDIT_BOX = "mt-2 rounded-md border border-border bg-muted/20 p-3";

// --- Runtimes ---

function RuntimeForm({ runtime, registry, onDone }: { runtime: Runtime | null; registry: Registry; onDone: () => void }) {
  const id = useId();
  const [name, setName] = useState(runtime?.name ?? "");
  const [type, setType] = useState(runtime?.type ?? registry.runtime_types[0]?.type ?? "");
  const [executable, setExecutable] = useState(runtime?.executable ?? "");
  const action = useAction();
  const defaultExecutable = registry.runtime_types.find((t) => t.type === type)?.default_executable ?? "";

  const save = async (e: React.FormEvent) => {
    e.preventDefault();
    const done = await action.run(() =>
      runtime
        ? updateRuntime(runtime.runtime_id, { name: name.trim(), executable: executable.trim() })
        : createRuntime({ name: name.trim(), type, executable: executable.trim() || undefined }),
    );
    if (done) onDone();
  };

  return (
    <form onSubmit={save} aria-label={runtime ? `Edit runtime ${runtime.name}` : "Add a runtime"} className="space-y-3">
      <div className="grid gap-3 sm:grid-cols-3">
        <div className="space-y-1">
          <label htmlFor={`${id}-name`} className={LABEL}>Name *</label>
          <Input id={`${id}-name`} value={name} maxLength={registry.limits.name_max_chars} onChange={(e) => setName(e.target.value)} placeholder="claude-beta" spellCheck={false} autoCapitalize="none" autoFocus />
        </div>
        <div className="space-y-1">
          <label htmlFor={`${id}-type`} className={LABEL}>Type</label>
          <select id={`${id}-type`} value={type} onChange={(e) => setType(e.target.value)} disabled={!!runtime} className={SELECT}>
            {registry.runtime_types.map((t) => (
              <option key={t.type} value={t.type}>{t.label}</option>
            ))}
          </select>
        </div>
        <div className="space-y-1">
          <label htmlFor={`${id}-executable`} className={LABEL}>Executable</label>
          <Input id={`${id}-executable`} value={executable} onChange={(e) => setExecutable(e.target.value)} placeholder={`Default: ${defaultExecutable}`} spellCheck={false} autoCapitalize="none" className="font-mono" />
        </div>
      </div>
      <p className="text-[11px] text-muted-foreground">
        A command on the server&apos;s PATH or an absolute path; empty runs the type&apos;s default. Saving runs it with
        --version to check it.
      </p>
      <FormFooter error={action.error} busy={action.busy} submit={runtime ? "Save" : "Add runtime"} onCancel={onDone} />
    </form>
  );
}

/** What the last --version probe found, and when. */
function ProbeState({ runtime }: { runtime: Runtime }) {
  const when = runtime.probed_at ? ` · checked ${new Date(runtime.probed_at).toLocaleString()}` : "";
  if (runtime.probe_error) {
    return (
      <span className="inline-flex min-w-0 items-center gap-1 text-warning">
        <AlertTriangle className="h-3 w-3 shrink-0" aria-hidden="true" />
        <span className="break-all">Probe failed: {runtime.probe_error}{when}</span>
      </span>
    );
  }
  if (runtime.version) return <span>version {runtime.version}{when}</span>;
  return <span>not probed yet</span>;
}

function RuntimeRow({ runtime, registry }: { runtime: Runtime; registry: Registry }) {
  const [editing, setEditing] = useState(false);
  const action = useAction();
  const remove = () => {
    if (confirm(`Delete runtime "${runtime.name}"?`)) action.run(() => deleteRuntime(runtime.runtime_id));
  };
  return (
    <Row
      title={<RowName name={runtime.name} builtin={runtime.builtin} kind={runtime.type_label} />}
      actions={
        <>
          <Button type="button" variant="ghost" size="sm" onClick={() => action.run(() => probeRuntime(runtime.runtime_id))} disabled={action.busy} aria-label={`Probe runtime ${runtime.name}`}>
            {action.busy ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <RefreshCw className="h-3.5 w-3.5" />} Probe
          </Button>
          {!runtime.builtin && (
            <EditDelete what="runtime" name={runtime.name} editing={editing} busy={action.busy} onEdit={() => setEditing(!editing)} onDelete={remove} />
          )}
        </>
      }
      below={
        <>
          {action.error && <p role="alert" className="text-xs text-destructive">{action.error}</p>}
          {editing && (
            <div className={EDIT_BOX}>
              <RuntimeForm runtime={runtime} registry={registry} onDone={() => setEditing(false)} />
            </div>
          )}
        </>
      }
    >
      <p className="flex flex-wrap items-center gap-x-2 gap-y-0.5 text-xs text-muted-foreground">
        <span className="break-all font-mono text-foreground/80">
          {runtime.executable || `default (${runtime.default_executable ?? "none"})`}
        </span>
        <span aria-hidden="true">·</span>
        <ProbeState runtime={runtime} />
        <span aria-hidden="true">·</span>
        <span>{usedBy(runtime.agent_count)}</span>
      </p>
      <p className="break-words font-mono text-[11px] text-muted-foreground">
        {runtime.capabilities.length ? runtime.capabilities.join(", ") : "no capabilities"}
      </p>
    </Row>
  );
}

// --- Providers ---

const PRICE_LABELS: Record<PriceField, string> = {
  input_usd_per_mtok: "Input",
  output_usd_per_mtok: "Output",
  cache_read_usd_per_mtok: "Cache read",
  cache_write_usd_per_mtok: "Cache write",
};

const AUTH_LABELS: Record<ProviderAuth, string> = { oauth: "OAuth (a runtime's login)", api_key: "API key" };

/** The built-in price of a model as the price table names it: without a [1m]-style or date suffix. */
function builtinPrice(registry: Registry, model: string, field: PriceField): number | null {
  const base = model.trim().replace(/\[[^\]]*\]$/, "").replace(/-\d{8}$/, "").toLowerCase();
  return registry.prices.models[base]?.[field] ?? null;
}

/** A provider's model catalog: every field but the name may be unknown; an unknown price is the built-in one. */
function ModelTable({ models, registry }: { models: ProviderModel[]; registry: Registry }) {
  const priceFields = registry.limits.price_fields;
  if (!models.length) return <p className="text-xs text-muted-foreground">Models: any (no catalog)</p>;
  const price = (model: ProviderModel, field: PriceField) => {
    if (model[field] != null) return formatUsd(model[field]);
    const builtin = builtinPrice(registry, model.name, field);
    return builtin == null ? "—" : `${formatUsd(builtin)}*`;
  };
  const cell = "py-1 pr-4 align-top";
  return (
    <div className="overflow-x-auto">
      <table className="text-left text-[11px]">
        <thead className="text-muted-foreground">
          <tr>
            <th scope="col" className={`${cell} font-medium`}>Model</th>
            <th scope="col" className={`${cell} font-medium`}>Context</th>
            {priceFields.map((field) => (
              <th key={field} scope="col" className={`${cell} font-medium`}>{PRICE_LABELS[field]}</th>
            ))}
            <th scope="col" className={`${cell} font-medium`}>Efforts</th>
            <th scope="col" className={`${cell} font-medium`}>Background model</th>
          </tr>
        </thead>
        <tbody className="font-mono">
          {models.map((model) => (
            <tr key={model.name}>
              <td className={`${cell} text-foreground`}>{model.name}</td>
              <td className={cell}>{model.context_window?.toLocaleString() ?? "—"}</td>
              {priceFields.map((field) => (
                <td key={field} className={cell}>{price(model, field)}</td>
              ))}
              <td className={cell}>{model.efforts?.join(", ") ?? "—"}</td>
              <td className={cell}>{model.background_model ?? "itself"}</td>
            </tr>
          ))}
        </tbody>
      </table>
      <p className="text-[11px] text-muted-foreground">
        Prices in USD per million tokens; * is the built-in price ({registry.prices.as_of}), — is unknown.
      </p>
    </div>
  );
}

type ModelDraft = { name: string; context_window: string; efforts: string; background_model: string } & Record<
  PriceField,
  string
>;

interface ProviderDraft {
  name: string;
  auth: ProviderAuth;
  oauthType: string;
  protocols: string[];
  baseUrl: string;
  maxConcurrency: string;
  models: ModelDraft[];
  /** Extra environment variables, one KEY=value per line. */
  env: string;
  /** A new key; empty keeps the stored one (or sets none). */
  apiKey: string;
  removeKey: boolean;
}

const text = (value: number | null) => (value == null ? "" : String(value));
/** Blank is null (unknown); a number that does not parse goes as typed, so the server's 400 explains it. */
const numberOrTyped = (raw: string) => {
  const value = raw.trim();
  if (!value) return null;
  const number = Number(value);
  return Number.isFinite(number) ? number : value;
};

function modelDraft(model: Partial<ProviderModel> | null, priceFields: PriceField[]): ModelDraft {
  const prices = Object.fromEntries(priceFields.map((f) => [f, text(model?.[f] ?? null)])) as Record<PriceField, string>;
  return {
    name: model?.name ?? "",
    context_window: text(model?.context_window ?? null),
    efforts: model?.efforts?.join(", ") ?? "",
    background_model: model?.background_model ?? "",
    ...prices,
  };
}

const envText = (env: { key: string; value: string }[]) => env.map((e) => `${e.key}=${e.value}`).join("\n");

/** KEY=value lines as entries; a line without "=" goes as a key with an empty value, so the server's 400 explains. */
function envEntries(raw: string) {
  return raw
    .split("\n")
    .map((line) => line.trim())
    .filter(Boolean)
    .map((line) => {
      const at = line.indexOf("=");
      return at < 0 ? { key: line, value: "" } : { key: line.slice(0, at).trim(), value: line.slice(at + 1) };
    });
}

/** A new provider's draft from one API of a preset: everything but the key. */
function presetDraft(preset: ProviderPreset, api: ProviderPreset["apis"][number], registry: Registry): ProviderDraft {
  return {
    ...providerDraft(null, registry),
    name: preset.name,
    auth: "api_key",
    protocols: api.protocols,
    baseUrl: api.base_url,
    models: preset.models.map((m) => modelDraft(m, registry.limits.price_fields)),
    // The vendor's Claude Code variables matter only to an Anthropic-format API.
    env: api.protocols.includes("anthropic-messages") ? envText(preset.env) : "",
  };
}

function providerDraft(provider: Provider | null, registry: Registry): ProviderDraft {
  const oauthTypes = registry.runtime_types.filter((t) => t.auth_kinds.includes("oauth"));
  return {
    name: provider?.name ?? "",
    auth: provider?.auth ?? "api_key",
    oauthType: provider?.oauth_runtime_type ?? oauthTypes[0]?.type ?? "",
    protocols: provider?.protocols ?? registry.limits.protocols.slice(0, 1),
    baseUrl: provider?.base_url ?? "",
    maxConcurrency: text(provider?.max_concurrency ?? null),
    models: (provider?.models ?? []).map((m) => modelDraft(m, registry.limits.price_fields)),
    env: envText(provider?.env ?? []),
    apiKey: "",
    removeKey: false,
  };
}

function providerBody(draft: ProviderDraft, provider: Provider | null, registry: Registry): ProviderFields {
  const fields: ProviderFields = {
    name: draft.name.trim(),
    auth: draft.auth,
    protocols: draft.protocols,
    max_concurrency: numberOrTyped(draft.maxConcurrency),
    models: draft.models.map((m) => ({
      name: m.name.trim(),
      context_window: numberOrTyped(m.context_window),
      ...Object.fromEntries(registry.limits.price_fields.map((f) => [f, numberOrTyped(m[f])])),
      efforts: m.efforts.trim() ? m.efforts.split(",").map((e) => e.trim()).filter(Boolean) : null,
      background_model: m.background_model.trim() || null,
    })),
    env: envEntries(draft.env),
  };
  if (draft.auth === "oauth") return { ...fields, oauth_runtime_type: draft.oauthType, base_url: "" };
  fields.oauth_runtime_type = null;
  fields.base_url = draft.baseUrl.trim();
  if (provider) fields.api_key = draft.removeKey ? null : draft.apiKey.trim() ? draft.apiKey : registry.limits.secret_mask;
  else if (draft.apiKey.trim()) fields.api_key = draft.apiKey;
  return fields;
}

function ProviderForm({ provider, registry, onDone }: { provider: Provider | null; registry: Registry; onDone: () => void }) {
  const id = useId();
  const [draft, setDraft] = useState<ProviderDraft>(() => providerDraft(provider, registry));
  const action = useAction();
  const set = (change: Partial<ProviderDraft>) => setDraft((d) => ({ ...d, ...change }));
  const setModel = (index: number, change: Partial<ModelDraft>) =>
    set({ models: draft.models.map((m, i) => (i === index ? { ...m, ...change } : m)) });
  const priceFields = registry.limits.price_fields;
  const keyStored = !!provider?.api_key;

  const save = async (e: React.FormEvent) => {
    e.preventDefault();
    const body = providerBody(draft, provider, registry);
    const done = await action.run(() => (provider ? updateProvider(provider.provider_id, body) : createProvider(body)));
    if (done) onDone();
  };

  const [presetKey, setPresetKey] = useState("");
  const preset = registry.provider_presets.find((p) => presetKey.startsWith(`${p.preset_id}:`));
  const choosePreset = (key: string) => {
    setPresetKey(key);
    const [presetId, index] = key.split(":");
    const chosen = registry.provider_presets.find((p) => p.preset_id === presetId);
    if (chosen) setDraft(presetDraft(chosen, chosen.apis[Number(index)], registry));
  };
  const apiLabel = (protocols: string[]) =>
    protocols.includes("anthropic-messages") ? "Anthropic API, for Claude Code" : `OpenAI API (${protocols.join(", ")})`;

  return (
    <form onSubmit={save} aria-label={provider ? `Edit provider ${provider.name}` : "Add a provider"} className="space-y-3">
      {!provider && registry.provider_presets.length > 0 && (
        <div className="space-y-1">
          <label htmlFor={`${id}-preset`} className={LABEL}>Start from a preset</label>
          <select id={`${id}-preset`} value={presetKey} onChange={(e) => choosePreset(e.target.value)} className={SELECT}>
            <option value="">None: fill in every field</option>
            {registry.provider_presets.map((p) =>
              p.apis.map((api, index) => (
                <option key={`${p.preset_id}:${index}`} value={`${p.preset_id}:${index}`}>
                  {p.label} · {apiLabel(api.protocols)}
                </option>
              )),
            )}
          </select>
          {preset && (
            <p className="text-[11px] text-muted-foreground">
              Filled from the vendor&apos;s documentation: add the API key below.
              {preset.note && ` ${preset.note[0].toUpperCase()}${preset.note.slice(1)}.`}
              {preset.docs.map((url, i) => (
                <a key={url} href={url} target="_blank" rel="noreferrer" className="ml-1 underline">
                  source {i + 1}
                </a>
              ))}
            </p>
          )}
        </div>
      )}
      <div className="grid gap-3 sm:grid-cols-3">
        <div className="space-y-1">
          <label htmlFor={`${id}-name`} className={LABEL}>Name *</label>
          <Input id={`${id}-name`} value={draft.name} maxLength={registry.limits.name_max_chars} onChange={(e) => set({ name: e.target.value })} placeholder="anthropic-api" spellCheck={false} autoCapitalize="none" autoFocus />
        </div>
        <div className="space-y-1">
          <label htmlFor={`${id}-auth`} className={LABEL}>Sign-in</label>
          <select id={`${id}-auth`} value={draft.auth} onChange={(e) => set({ auth: e.target.value as ProviderAuth })} className={SELECT}>
            {registry.limits.auth_kinds.map((kind) => (
              <option key={kind} value={kind}>{AUTH_LABELS[kind]}</option>
            ))}
          </select>
        </div>
        {draft.auth === "oauth" ? (
          <div className="space-y-1">
            <label htmlFor={`${id}-oauth`} className={LABEL}>Login of</label>
            <select id={`${id}-oauth`} value={draft.oauthType} onChange={(e) => set({ oauthType: e.target.value })} className={SELECT}>
              {registry.runtime_types
                .filter((t) => t.auth_kinds.includes("oauth"))
                .map((t) => (
                  <option key={t.type} value={t.type}>{t.label} runtimes</option>
                ))}
            </select>
          </div>
        ) : (
          <div className="space-y-1">
            <label htmlFor={`${id}-base`} className={LABEL}>Base URL</label>
            <Input id={`${id}-base`} value={draft.baseUrl} onChange={(e) => set({ baseUrl: e.target.value })} placeholder="The protocol's own endpoint" spellCheck={false} autoCapitalize="none" className="font-mono" />
          </div>
        )}
      </div>

      <div className="grid gap-3 sm:grid-cols-3">
        <fieldset className="space-y-1 sm:col-span-2">
          <legend className={LABEL}>Protocols *</legend>
          <div className="flex flex-wrap gap-x-4 gap-y-1">
            {registry.limits.protocols.map((protocol) => (
              <label key={protocol} className="flex items-center gap-1.5 font-mono text-xs">
                <input
                  type="checkbox"
                  className="h-4 w-4 accent-primary"
                  checked={draft.protocols.includes(protocol)}
                  onChange={(e) =>
                    set({
                      protocols: e.target.checked
                        ? registry.limits.protocols.filter((p) => p === protocol || draft.protocols.includes(p))
                        : draft.protocols.filter((p) => p !== protocol),
                    })
                  }
                />
                {protocol}
              </label>
            ))}
          </div>
        </fieldset>
        <div className="space-y-1">
          <label htmlFor={`${id}-concurrency`} className={LABEL}>Max concurrent runs</label>
          <Input id={`${id}-concurrency`} inputMode="numeric" value={draft.maxConcurrency} onChange={(e) => set({ maxConcurrency: e.target.value })} placeholder="No limit" />
        </div>
      </div>

      {draft.auth === "api_key" && (
        <div className="space-y-1">
          <label htmlFor={`${id}-key`} className={LABEL}>API key</label>
          <div className="flex flex-wrap items-center gap-3">
            <Input
              id={`${id}-key`}
              type="password"
              autoComplete="new-password"
              value={draft.apiKey}
              onChange={(e) => set({ apiKey: e.target.value })}
              disabled={draft.removeKey}
              placeholder={keyStored ? "Stored; leave empty to keep it" : "Not set"}
              className="min-w-48 flex-1 font-mono"
            />
            {keyStored && (
              <label className="flex items-center gap-1.5 text-xs text-muted-foreground">
                <input type="checkbox" className="h-4 w-4 accent-primary" checked={draft.removeKey} onChange={(e) => set({ removeKey: e.target.checked, apiKey: "" })} />
                Remove the stored key
              </label>
            )}
          </div>
          <p className="text-[11px] text-muted-foreground">Stored in Looper&apos;s database and never shown again.</p>
        </div>
      )}

      <fieldset className="space-y-2">
        <legend className={LABEL}>Models</legend>
        {draft.models.map((model, index) => (
          <div key={index} className="space-y-2 rounded-md border border-border p-2">
            <div className="grid grid-cols-2 gap-2 sm:grid-cols-4 lg:grid-cols-[minmax(0,2fr)_repeat(5,minmax(0,1fr))_minmax(0,1.5fr)]">
              <div className="col-span-2 space-y-0.5 lg:col-span-1">
                <label htmlFor={`${id}-m${index}-name`} className={SMALL_LABEL}>Name *</label>
                <Input id={`${id}-m${index}-name`} value={model.name} onChange={(e) => setModel(index, { name: e.target.value })} placeholder="claude-sonnet-4-5" spellCheck={false} autoCapitalize="none" className="h-8 font-mono text-xs" />
              </div>
              <div className="space-y-0.5">
                <label htmlFor={`${id}-m${index}-context`} className={SMALL_LABEL}>Context (tokens)</label>
                <Input id={`${id}-m${index}-context`} inputMode="numeric" value={model.context_window} onChange={(e) => setModel(index, { context_window: e.target.value })} className="h-8 font-mono text-xs" />
              </div>
              {priceFields.map((field) => (
                <div key={field} className="space-y-0.5">
                  <label htmlFor={`${id}-m${index}-${field}`} className={SMALL_LABEL}>{PRICE_LABELS[field]} $/Mtok</label>
                  <Input id={`${id}-m${index}-${field}`} inputMode="decimal" value={model[field]} onChange={(e) => setModel(index, { [field]: e.target.value })} placeholder={builtinPrice(registry, model.name, field)?.toString() ?? ""} className="h-8 font-mono text-xs" />
                </div>
              ))}
              <div className="space-y-0.5">
                <label htmlFor={`${id}-m${index}-efforts`} className={SMALL_LABEL}>Efforts</label>
                <Input id={`${id}-m${index}-efforts`} value={model.efforts} onChange={(e) => setModel(index, { efforts: e.target.value })} placeholder="low, high" spellCheck={false} autoCapitalize="none" className="h-8 font-mono text-xs" />
              </div>
            </div>
            <div className="space-y-0.5 sm:max-w-xs">
              <label htmlFor={`${id}-m${index}-background`} className={SMALL_LABEL}>Background model</label>
              <Input id={`${id}-m${index}-background`} value={model.background_model} onChange={(e) => setModel(index, { background_model: e.target.value })} placeholder="The model itself" spellCheck={false} autoCapitalize="none" className="h-8 font-mono text-xs" />
            </div>
            <Button type="button" variant="ghost" size="sm" onClick={() => set({ models: draft.models.filter((_, i) => i !== index) })} aria-label={`Remove model ${model.name || index + 1}`} className="text-muted-foreground">
              <X className="h-3.5 w-3.5" /> Remove model
            </Button>
          </div>
        ))}
        <Button type="button" variant="outline" size="sm" onClick={() => set({ models: [...draft.models, modelDraft(null, priceFields)] })} disabled={draft.models.length >= registry.limits.models_max}>
          <Plus className="h-3.5 w-3.5" /> Add model
        </Button>
        <p className="text-[11px] text-muted-foreground">
          Blank fields are unknown, and a blank price uses the built-in price shown greyed (from {registry.prices.as_of});
          a model with no price records tokens only and no spend cap applies to it. Efforts are comma-separated. The
          background model serves the runtime&apos;s side requests (titles, summaries, quick subagents). With no models
          the provider serves any model; with some, its agents must use one of them.
        </p>
      </fieldset>

      <div className="space-y-1">
        <label htmlFor={`${id}-env`} className={LABEL}>Extra environment</label>
        <textarea
          id={`${id}-env`}
          value={draft.env}
          onChange={(e) => set({ env: e.target.value })}
          rows={Math.max(2, draft.env.split("\n").length)}
          placeholder="API_TIMEOUT_MS=3000000"
          spellCheck={false}
          className="w-full rounded-md border border-input bg-transparent px-3 py-2 font-mono text-xs"
        />
        <p className="text-[11px] text-muted-foreground">
          One KEY=value per line, added to the runs that use this provider. Not for secrets (the key goes above) or
          what the runtime sets from the fields above ({registry.limits.provider_env_owned.join(", ")}).
        </p>
      </div>

      <FormFooter error={action.error} busy={action.busy} submit={provider ? "Save" : "Add provider"} onCancel={onDone} />
    </form>
  );
}

function ProviderRow({ provider, registry }: { provider: Provider; registry: Registry }) {
  const [editing, setEditing] = useState(false);
  // An import's "secrets to fill in" links here (#settings/provider/<id>) to open the form.
  const ref = useSettingsTarget<HTMLLIElement>("provider", provider.provider_id, () => setEditing(!provider.builtin));
  const action = useAction();
  const remove = () => {
    if (confirm(`Delete provider "${provider.name}"?`)) action.run(() => deleteProvider(provider.provider_id));
  };
  const oauthLabel = registry.runtime_types.find((t) => t.type === provider.oauth_runtime_type)?.label ?? provider.oauth_runtime_type;
  const details = [
    provider.auth === "oauth" ? `OAuth: the login of ${oauthLabel} runtimes` : `API key: ${provider.api_key ?? "not set"}`,
    provider.protocols.join(", "),
    provider.auth === "api_key" ? provider.base_url || "the protocol's own endpoint" : "",
    provider.max_concurrency == null ? "no concurrency limit" : `at most ${provider.max_concurrency} runs at once`,
    provider.env.length ? `env ${provider.env.map((e) => e.key).join(", ")}` : "",
    usedBy(provider.agent_count),
  ].filter(Boolean);
  return (
    <Row
      ref={ref}
      title={<RowName name={provider.name} builtin={provider.builtin} />}
      actions={
        !provider.builtin && (
          <EditDelete what="provider" name={provider.name} editing={editing} busy={action.busy} onEdit={() => setEditing(!editing)} onDelete={remove} />
        )
      }
      below={
        <>
          {action.error && <p role="alert" className="text-xs text-destructive">{action.error}</p>}
          {editing && (
            <div className={EDIT_BOX}>
              <ProviderForm provider={provider} registry={registry} onDone={() => setEditing(false)} />
            </div>
          )}
        </>
      }
    >
      <p className="break-words text-xs text-muted-foreground">{details.join(" · ")}</p>
      <ModelTable models={provider.models} registry={registry} />
    </Row>
  );
}

// --- Agents ---

function AgentForm({ agent, registry, onDone }: { agent: RegistryAgent | null; registry: Registry; onDone: () => void }) {
  const id = useId();
  const [draft, setDraft] = useState({
    name: agent?.name ?? "",
    runtime_id: agent?.runtime_id ?? registry.runtimes[0]?.runtime_id ?? "",
    provider_id: agent?.provider_id ?? registry.providers[0]?.provider_id ?? "",
    model: agent?.model ?? "",
    effort: agent?.effort ?? "",
  });
  const action = useAction();
  const set = (change: Partial<typeof draft>) => setDraft((d) => ({ ...d, ...change }));
  const provider = registry.providers.find((p) => p.provider_id === draft.provider_id);

  const [warnings, setWarnings] = useState<string[]>([]);
  const save = async (e: React.FormEvent) => {
    e.preventDefault();
    const body = { ...draft, name: draft.name.trim(), model: draft.model.trim(), effort: draft.effort || null };
    let answer: { warnings: string[] } | null = null;
    const done = await action.run(async () => {
      answer = await (agent ? updateRegistryAgent(agent.agent_id, body) : createRegistryAgent(body));
    });
    // Saved; a warning (a model without a price) keeps the form open until it is read.
    const saved = answer as { warnings: string[] } | null;
    if (done && saved?.warnings.length) setWarnings(saved.warnings);
    else if (done) onDone();
  };

  return (
    <form onSubmit={save} aria-label={agent ? `Edit agent ${agent.name}` : "Add an agent"} className="space-y-3">
      <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-3">
        <div className="space-y-1">
          <label htmlFor={`${id}-name`} className={LABEL}>Name *</label>
          <Input id={`${id}-name`} value={draft.name} maxLength={registry.limits.name_max_chars} onChange={(e) => set({ name: e.target.value })} placeholder="claude-code/sonnet" spellCheck={false} autoCapitalize="none" className="font-mono" autoFocus />
        </div>
        <div className="space-y-1">
          <label htmlFor={`${id}-runtime`} className={LABEL}>Runtime</label>
          <select id={`${id}-runtime`} value={draft.runtime_id} onChange={(e) => set({ runtime_id: e.target.value })} className={SELECT}>
            {registry.runtimes.map((r) => (
              <option key={r.runtime_id} value={r.runtime_id}>{r.name} ({r.type_label})</option>
            ))}
          </select>
        </div>
        <div className="space-y-1">
          <label htmlFor={`${id}-provider`} className={LABEL}>Provider</label>
          <select id={`${id}-provider`} value={draft.provider_id} onChange={(e) => set({ provider_id: e.target.value })} className={SELECT}>
            {registry.providers.map((p) => (
              <option key={p.provider_id} value={p.provider_id}>{p.name} ({AUTH_LABELS[p.auth]})</option>
            ))}
          </select>
        </div>
        <div className="space-y-1">
          <label htmlFor={`${id}-model`} className={LABEL}>Model *</label>
          <Input id={`${id}-model`} list={`${id}-models`} value={draft.model} onChange={(e) => set({ model: e.target.value })} placeholder="sonnet" spellCheck={false} autoCapitalize="none" className="font-mono" />
          <datalist id={`${id}-models`}>
            {provider?.models.map((m) => <option key={m.name} value={m.name} />)}
          </datalist>
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
      <p className="text-[11px] text-muted-foreground">
        {provider?.models.length
          ? `${provider.name} lists ${provider.models.map((m) => m.name).join(", ")}.`
          : "The model as the runtime takes it: an alias such as sonnet or a full name."}{" "}
        A settings layer&apos;s effort overrides the default.
      </p>
      {warnings.length > 0 && (
        <div role="status" className="space-y-1 rounded-md border border-amber-500/40 bg-amber-500/10 px-3 py-2 text-xs">
          <p className="font-medium">Saved, with a warning:</p>
          {warnings.map((w) => (
            <p key={w}>{w}</p>
          ))}
          <Button type="button" size="sm" variant="outline" onClick={onDone}>OK</Button>
        </div>
      )}
      <FormFooter error={action.error} busy={action.busy} submit={agent ? "Save" : "Add agent"} onCancel={onDone} />
    </form>
  );
}

/** How an agent's runs are priced, in words. */
function costBasisText(agent: RegistryAgent): string {
  if (agent.cost.source === "runtime") {
    return agent.cost.equivalent ? "cost: the runtime's figure, a subscription equivalent" : "cost: the runtime's figure";
  }
  return agent.cost.source === "provider_price" ? "cost: tokens at the provider's prices" : "";
}

function AgentRow({ agent, registry }: { agent: RegistryAgent; registry: Registry }) {
  const [editing, setEditing] = useState(false);
  const action = useAction();
  const remove = () => {
    if (confirm(`Delete agent "${agent.name}"?`)) action.run(() => deleteRegistryAgent(agent.agent_id));
  };
  const runtimeLabel = registry.runtimes.find((r) => r.runtime_id === agent.runtime_id)?.type_label ?? agent.runtime_type;
  return (
    <Row
      title={<RowName name={agent.name} builtin={agent.builtin} />}
      actions={
        !agent.builtin && (
          <EditDelete what="agent" name={agent.name} editing={editing} busy={action.busy} onEdit={() => setEditing(!editing)} onDelete={remove} />
        )
      }
      below={
        <>
          {action.error && <p role="alert" className="text-xs text-destructive">{action.error}</p>}
          {editing && (
            <div className={EDIT_BOX}>
              <AgentForm agent={agent} registry={registry} onDone={() => setEditing(false)} />
            </div>
          )}
        </>
      }
    >
      <p className="break-words text-xs text-muted-foreground">
        {agent.runtime_name} ({runtimeLabel}) · {agent.provider_name} · model{" "}
        <span className="font-mono text-foreground/80">{agent.model}</span> · effort {agent.effort ?? "runtime default"}
        {costBasisText(agent) && ` · ${costBasisText(agent)}`}
      </p>
      {agent.cost.unpriced.length > 0 && (
        <p className="flex items-start gap-1 text-xs text-amber-700 dark:text-amber-400">
          <AlertTriangle className="mt-0.5 h-3.5 w-3.5 shrink-0" />
          No price for {agent.cost.unpriced.join(", ")}: its runs record tokens only and spend caps do not apply. Add
          the prices to the provider&apos;s model catalog.
        </p>
      )}
    </Row>
  );
}

/**
 * The agents Looper can run, and what they are made of: runtimes (agent CLIs on this machine), LLM providers and
 * agents (a runtime, a provider and a model). The settings layers and tasks select an agent.
 */
export function AgentRegistrySection() {
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
    <div className="space-y-4">
      <RegistryList
        title="Agents"
        hint="What Settings, the Agents page, projects and tasks select."
        addLabel="Add agent"
        form={(close) => <AgentForm agent={null} registry={registry} onDone={close} />}
      >
        {registry.agents.map((agent) => (
          <AgentRow key={agent.agent_id} agent={agent} registry={registry} />
        ))}
      </RegistryList>
      <RegistryList
        title="Providers"
        hint="Where the model runs and how Looper signs in to it."
        addLabel="Add provider"
        form={(close) => <ProviderForm provider={null} registry={registry} onDone={close} />}
      >
        {registry.providers.map((provider) => (
          <ProviderRow key={provider.provider_id} provider={provider} registry={registry} />
        ))}
      </RegistryList>
      <RegistryList
        title="Runtimes"
        hint="The agent CLIs on this machine; their capabilities decide the roles their agents can run."
        addLabel="Add runtime"
        form={(close) => <RuntimeForm runtime={null} registry={registry} onDone={close} />}
      >
        {registry.runtimes.map((runtime) => (
          <RuntimeRow key={runtime.runtime_id} runtime={runtime} registry={registry} />
        ))}
      </RegistryList>
    </div>
  );
}
