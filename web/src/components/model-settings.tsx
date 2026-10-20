import { useId, useState } from "react";
import { Input } from "@/components/ui/input";
import { Button } from "@/components/ui/button";
import { EDIT_BOX, EditDelete, FormFooter, LABEL, RegistryList, Row, RowName, SELECT } from "@/components/registry-ui";
import {
  createModel, deleteModel, updateModel,
  type ApiProtocolV3, type ModelFieldsV3, type ModelV3, type PriceField, type ProviderV3, type RegistryV3,
} from "@/lib/api";
import { settingsHref } from "@/lib/settings-pages";
import { useAction } from "@/lib/use-action";

const PRICES: { key: PriceField; label: string }[] = [
  { key: "input_usd_per_mtok", label: "Input" },
  { key: "output_usd_per_mtok", label: "Output" },
  { key: "cache_read_usd_per_mtok", label: "Cache read" },
  { key: "cache_write_usd_per_mtok", label: "Cache write" },
];
type NumericField = PriceField | "context_window";
type ModelDefaults = Partial<Pick<ModelV3, NumericField | "efforts" | "background_model" | "name">>;
type EffortSupport = "unknown" | "none" | "supported";
interface ModelDraft {
  protocol: ApiProtocolV3 | "";
  name: string;
  suggestion: string;
  numbers: Record<NumericField, string>;
  effortSupport: EffortSupport;
  efforts: string;
  background: string;
}

function modelDraft(model: ModelDefaults = {}, protocol: ModelDraft["protocol"] = ""): ModelDraft {
  return {
    protocol, name: model.name ?? "", suggestion: "",
    numbers: {
      context_window: model.context_window == null ? "" : String(model.context_window),
      input_usd_per_mtok: model.input_usd_per_mtok == null ? "" : String(model.input_usd_per_mtok),
      output_usd_per_mtok: model.output_usd_per_mtok == null ? "" : String(model.output_usd_per_mtok),
      cache_read_usd_per_mtok: model.cache_read_usd_per_mtok == null ? "" : String(model.cache_read_usd_per_mtok),
      cache_write_usd_per_mtok: model.cache_write_usd_per_mtok == null ? "" : String(model.cache_write_usd_per_mtok),
    },
    effortSupport: model.efforts == null ? "unknown" : model.efforts.length ? "supported" : "none",
    efforts: model.efforts?.join(", ") ?? "", background: model.background_model ?? "",
  };
}

function numericValue(raw: string) {
  if (!raw.trim()) return null;
  const value = Number(raw);
  return Number.isFinite(value) ? value : raw;
}

function modelSettings(draft: ModelDraft): ModelFieldsV3 {
  return {
    context_window: numericValue(draft.numbers.context_window),
    input_usd_per_mtok: numericValue(draft.numbers.input_usd_per_mtok),
    output_usd_per_mtok: numericValue(draft.numbers.output_usd_per_mtok),
    cache_read_usd_per_mtok: numericValue(draft.numbers.cache_read_usd_per_mtok),
    cache_write_usd_per_mtok: numericValue(draft.numbers.cache_write_usd_per_mtok),
    efforts: draft.effortSupport === "unknown" ? null : draft.effortSupport === "none" ? []
      : draft.efforts.split(",").map((effort) => effort.trim()).filter(Boolean),
    background_model: draft.background || null,
  };
}

function ModelForm({ model, provider, registry, onDone }: {
  model: ModelV3 | null; provider: ProviderV3; registry: RegistryV3; onDone: () => void;
}) {
  const id = useId();
  const [draft, setDraft] = useState(() => modelDraft(model ?? {}, model?.protocol));
  const [warnings, setWarnings] = useState<string[]>([]);
  const action = useAction();
  const set = (change: Partial<ModelDraft>) => setDraft((current) => ({ ...current, ...change }));
  const suggestions = registry.vendor_types.find((vendor) => vendor.vendor_type === provider.vendor_type)
    ?.suggested_models.filter((suggestion) => suggestion.protocol === draft.protocol) ?? [];
  const profiles = model ? registry.profiles.filter((profile) => profile.model_id === model.model_id) : [];
  const save = async (event: React.FormEvent) => {
    event.preventDefault();
    const protocol = draft.protocol;
    if (!protocol) return;
    const fields = modelSettings(draft);
    let savedWarnings: string[] = [];
    const done = await action.run(async () => {
      const answer = model ? await updateModel(model.model_id, fields)
        : await createModel({ provider_id: provider.provider_id, name: draft.name, protocol, ...fields });
      savedWarnings = answer.warnings;
    });
    if (!done) return;
    if (savedWarnings.length) setWarnings(savedWarnings);
    else onDone();
  };
  const numberInput = (key: NumericField, label: string) => <div key={key} className="space-y-1">
    <label htmlFor={`${id}-${key}`} className={LABEL}>{label}</label>
    <Input id={`${id}-${key}`} inputMode={key === "context_window" ? "numeric" : "decimal"} value={draft.numbers[key]}
      onChange={(event) => set({ numbers: { ...draft.numbers, [key]: event.target.value } })}
      placeholder={key === "context_window" ? "Unknown" : "Use built-in price, if known"} />
  </div>;
  return <form onSubmit={save} aria-label={model ? `Edit model ${model.name} (${model.protocol})` : `Add model to ${provider.name}`} className="space-y-3">
    <p className="text-xs text-muted-foreground">Provider: {provider.name}. {model && "Provider, protocol and exact name cannot be changed; create a new model to change its identity."}</p>
    <div className="grid gap-3 sm:grid-cols-2">
      <div className="space-y-1">
        <label htmlFor={`${id}-protocol`} className={LABEL}>Protocol *</label>
        <select id={`${id}-protocol`} className={SELECT} required disabled={!!model} value={draft.protocol}
          onChange={(event) => setDraft(modelDraft({}, event.target.value as ApiProtocolV3))}>
          <option value="">Choose a protocol</option>
          {provider.endpoints.map(({ protocol }) => <option key={protocol} value={protocol}>{protocol}</option>)}
        </select>
      </div>
      <div className="space-y-1">
        <label htmlFor={`${id}-name`} className={LABEL}>Exact model name *</label>
        <Input id={`${id}-name`} required disabled={!!model || !draft.protocol} value={draft.name} spellCheck={false} autoCapitalize="none"
          onChange={(event) => set({ name: event.target.value, suggestion: "" })} placeholder="Exact name sent to the runtime" />
      </div>
    </div>
    {!model && !provider.endpoints.length && <p role="alert" className="text-xs text-destructive">Add a provider endpoint before adding a model.</p>}
    {!model && suggestions.length > 0 && <div className="space-y-1">
      <label htmlFor={`${id}-suggestion`} className={LABEL}>Vendor suggestion</label>
      <select id={`${id}-suggestion`} className={SELECT} value={draft.suggestion} onChange={(event) => {
        const suggestion = suggestions.find((entry) => entry.name === event.target.value);
        setDraft({ ...modelDraft(suggestion, draft.protocol), suggestion: suggestion?.name ?? "" });
      }}>
        <option value="">Custom exact name</option>
        {suggestions.map((suggestion) => <option key={suggestion.name} value={suggestion.name}>{suggestion.name}</option>)}
      </select>
      <p className="text-[11px] text-muted-foreground">Suggestions for {draft.protocol} only. A suggestion is registered only when you choose Add model.</p>
    </div>}
    {numberInput("context_window", "Context window")}
    <p className="text-[11px] text-muted-foreground">Context does not rewrite the exact model name.</p>
    <div className="grid gap-3 sm:grid-cols-2">
      <div className="space-y-1">
        <label htmlFor={`${id}-effort-support`} className={LABEL}>Effort support</label>
        <select id={`${id}-effort-support`} className={SELECT} value={draft.effortSupport}
          onChange={(event) => set({ effortSupport: event.target.value as EffortSupport })}>
          <option value="unknown">Unknown</option><option value="none">No reasoning efforts</option><option value="supported">Supported efforts</option>
        </select>
      </div>
      {draft.effortSupport === "supported" && <div className="space-y-1">
        <label htmlFor={`${id}-efforts`} className={LABEL}>Supported efforts</label>
        <Input id={`${id}-efforts`} value={draft.efforts} onChange={(event) => set({ efforts: event.target.value })}
          placeholder={registry.limits.effort_levels.join(", ")} />
        <p className="text-[11px] text-muted-foreground">Comma-separated effort names.</p>
      </div>}
      <div className="space-y-1">
        <label htmlFor={`${id}-background`} className={LABEL}>Background model</label>
        <Input id={`${id}-background`} value={draft.background} onChange={(event) => set({ background: event.target.value })} spellCheck={false} autoCapitalize="none" placeholder="This model" />
      </div>
    </div>
    <p className="text-[11px] text-muted-foreground">Background uses the same provider and protocol, only in runtimes that support it. Empty uses this model.</p>
    <fieldset className="space-y-2">
      <legend className={LABEL}>Price overrides · USD per million tokens</legend>
      <div className="grid gap-3 sm:grid-cols-2">{PRICES.map(({ key, label }) => numberInput(key, `${label} price`))}</div>
      <p className="text-[11px] text-muted-foreground">Empty clears the override and uses a built-in price if known. 0 means explicitly free.</p>
    </fieldset>
    {model && <div className="space-y-1 text-xs text-muted-foreground">
      <p>Changes affect {model.profile_count} Agent Profiles. Changes to efforts apply to subsequent runs; existing runs keep their launch settings.</p>
      {profiles.length > 0 && <ul className="list-inside list-disc">{profiles.map((profile) => <li key={profile.profile_id}>{profile.name}</li>)}</ul>}
      <a href={settingsHref("profiles")} className="text-primary underline">Open Agent Profiles</a>
    </div>}
    {warnings.length ? <div role="status" className="space-y-1 rounded-md border border-warning/40 bg-warning/10 px-3 py-2 text-xs">
      <p className="font-medium">Saved, with a warning:</p>
      {warnings.map((warning) => <p key={warning}>{warning}</p>)}
      <Button type="button" size="sm" variant="outline" onClick={onDone}>OK</Button>
    </div> : <FormFooter error={action.error} busy={action.busy} submit={model ? "Save" : "Add model"} onCancel={onDone} />}
  </form>;
}

function ModelRow({ model, provider, registry }: { model: ModelV3; provider: ProviderV3; registry: RegistryV3 }) {
  const [editing, setEditing] = useState(false);
  const action = useAction();
  const name = `${model.name} (${model.protocol})`;
  const remove = () => {
    if (confirm(`Delete model "${model.name}" (${model.protocol}) from ${provider.name}?`)) action.run(() => deleteModel(model.model_id));
  };
  return <Row title={<RowName name={model.name} builtin={model.builtin} kind={model.protocol} />}
    actions={!model.builtin && <EditDelete what="model" name={name} editing={editing} busy={action.busy} onEdit={() => setEditing(!editing)} onDelete={remove} />}
    below={<>
      {action.error && <p role="alert" className="text-xs text-destructive">{action.error}</p>}
      {editing && <div className={EDIT_BOX}><ModelForm model={model} provider={provider} registry={registry} onDone={() => setEditing(false)} /></div>}
    </>}>
    <p className="break-words text-xs text-muted-foreground">
      Context: {model.context_window ?? "unknown"} · Efforts: {model.efforts === null ? "unknown" : model.efforts.length ? model.efforts.join(", ") : "none"}
      {" · "}Background: {model.background_model ?? "this model"} · Used by {model.profile_count} Agent Profiles
    </p>
    <p className="break-words text-xs text-muted-foreground">USD per million tokens · {PRICES.map(({ key, label }) => {
      const price = model.prices[key];
      return `${label}: ${price.effective === null ? "unknown" : `${price.effective} (${price.source})`}`;
    }).join(" · ")}</p>
    {model.warnings.map((warning) => <p key={warning} className="text-xs text-warning">{warning}</p>)}
  </Row>;
}

/** A provider's callable models; vendor suggestions are creation inputs, never saved implicitly. */
export function ModelsSection({ provider, registry }: { provider: ProviderV3; registry: RegistryV3 }) {
  const models = registry.models.filter((model) => model.provider_id === provider.provider_id);
  return <RegistryList title="Models" hint="One exact model name and protocol per entry." addLabel="Add model"
    form={(close) => <ModelForm model={null} provider={provider} registry={registry} onDone={close} />}>
    {models.length ? models.map((model) => <ModelRow key={model.model_id} model={model} provider={provider} registry={registry} />)
      : <li className="px-5 py-3 text-xs text-muted-foreground">No models registered. Add a model explicitly to use it in an Agent Profile.</li>}
  </RegistryList>;
}
