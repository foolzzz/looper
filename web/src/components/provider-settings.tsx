import { useId, useState } from "react";
import { Loader2, Plus, X } from "lucide-react";
import { Input } from "@/components/ui/input";
import { Button } from "@/components/ui/button";
import {
  EDIT_BOX,
  EditDelete,
  FormFooter,
  LABEL,
  RegistryList,
  Row,
  RowName,
  SELECT,
  SMALL_LABEL,
} from "@/components/registry-ui";
import {
  createProvider,
  deleteProvider,
  updateProvider,
  type PriceField,
  type Provider,
  type ProviderAuth,
  type ProviderFields,
  type ProviderModel,
  type ProviderPreset,
  type Registry,
} from "@/lib/api";
import { AGENT_PROFILE } from "@/lib/settings-pages";
import { useAction } from "@/lib/use-action";
import { formatUsd } from "@/lib/usage";
import { useRegistry } from "@/lib/use-registry";
import { useSettingsTarget } from "@/lib/settings-target";

const usedBy = (count: number) =>
  count === 0 ? `no ${AGENT_PROFILE.plural}` : `used by ${count} ${count === 1 ? AGENT_PROFILE.name : AGENT_PROFILE.plural}`;

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
          the provider serves any model; with some, its {AGENT_PROFILE.plural} must use one of them.
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
  // An import's "secrets to fill in" links here (#settings/providers/<id>) to open the form.
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
    usedBy(provider.profile_count),
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

/**
 * Settings → LLM Providers: where agents' requests go and how Looper signs in to them. `adding` opens the add
 * form (the link #settings/providers/new, from an Agent Profile's provider list).
 */
export function ProvidersSection({ adding = false }: { adding?: boolean }) {
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
      title="Providers"
      hint="Where the model runs and how Looper signs in to it."
      addLabel="Add provider"
      defaultAdding={adding}
      form={(close) => <ProviderForm provider={null} registry={registry} onDone={close} />}
    >
      {registry.providers.map((provider) => (
        <ProviderRow key={provider.provider_id} provider={provider} registry={registry} />
      ))}
    </RegistryList>
  );
}
