import { useEffect, useState } from "react";
import { Loader2 } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { EDIT_BOX } from "@/components/registry-ui";
import { addModels, fetchProviderModels, protocolLabel, type ModelListing, type Provider, type Registry } from "@/lib/api";
import { errorText, useAction } from "@/lib/use-action";

/** Lists only saved endpoints; a selection adds each missing protocol, never a suggested model. */
export function ProviderModelPicker({ provider, registry, intro, onDone }: {
  provider: Provider; registry: Registry; intro?: string; onDone: () => void;
}) {
  const [listings, setListings] = useState<ModelListing[] | null>(null);
  const [fetchError, setFetchError] = useState("");
  const [attempt, setAttempt] = useState(0);
  const [filter, setFilter] = useState("");
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [report, setReport] = useState<{ added: number; skipped: string[]; warnings: string[] } | null>(null);
  const action = useAction();
  useEffect(() => {
    let active = true;
    setListings(null); setFetchError(""); setSelected(new Set()); setReport(null);
    fetchProviderModels(provider.provider_id).then((answer) => {
      if (active) setListings(answer.listings);
    }).catch((error: unknown) => { if (active) setFetchError(errorText(error)); });
    return () => { active = false; };
  }, [provider.provider_id, attempt]);
  const key = (index: number, name: string) => JSON.stringify([index, name]);
  const entries = (listings ?? []).flatMap((listing, index) => listing.models.flatMap((model) =>
    selected.has(key(index, model.name)) && model.addable
      ? listing.protocols.filter((protocol) => !model.registered.includes(protocol)).map((protocol) => ({
        name: model.name, protocol, ...(model.context_window == null ? {} : { context_window: model.context_window }),
      })) : []));
  const toggle = (keys: string[], checked: boolean) => setSelected((current) => {
    const next = new Set(current);
    for (const item of keys) { if (checked) next.add(item); else next.delete(item); }
    return next;
  });
  const add = () => action.run(async () => {
    const answer = await addModels(provider.provider_id, entries);
    const registered = [...answer.models, ...answer.skipped];
    setListings((current) => current?.map((listing) => ({ ...listing, models: listing.models.map((model) => ({
      ...model, registered: [...new Set([...model.registered, ...registered.filter((entry) => entry.name === model.name)
        .map((entry) => entry.protocol).filter((protocol) => listing.protocols.includes(protocol))])],
    })) })) ?? null);
    setSelected(new Set());
    setReport({ added: answer.models.length, skipped: answer.skipped.map((item) => `${item.name} (${item.protocol}): ${item.reason}`), warnings: answer.warnings });
  });
  const retry = <Button type="button" size="sm" variant="outline" onClick={() => setAttempt((value) => value + 1)}>Fetch again</Button>;
  return <div className="space-y-3">
    {intro && <p className="text-sm">{intro}</p>}
    {!listings && !fetchError && <p role="status" className="flex items-center gap-2 text-xs text-muted-foreground">
      <Loader2 className="h-4 w-4 animate-spin motion-reduce:animate-none" aria-hidden="true" />Asking {provider.name} for its models…
    </p>}
    {fetchError && <div role="alert" className="space-y-2 text-xs"><p>{fetchError}. The provider is saved; add models by hand under Models, or fetch again.</p>{retry}</div>}
    {listings?.map((listing, index) => {
      const label = registry.limits.api_formats.find((format) => format.family === listing.family)?.label ?? listing.family;
      const location = `${label} at ${listing.base_url}`;
      const shown = listing.models.filter((model) => model.name.toLocaleLowerCase().includes(filter.toLocaleLowerCase()));
      const available = shown.filter((model) => model.addable && listing.protocols.some((protocol) => !model.registered.includes(protocol)));
      return <div key={JSON.stringify([listing.family, listing.base_url])} className={EDIT_BOX}>
        {listing.state === "failed" ? <div role="alert" className="space-y-2 text-xs">
          <p>{location}: could not list models: {listing.problem}. The provider is saved; add models by hand under Models, or fetch again.</p>{retry}
        </div> : listing.state === "skipped" ? <p className="text-xs text-muted-foreground">{listing.problem}</p>
          : !listing.models.length ? <p className="text-xs text-muted-foreground">{location} lists no models.</p> : <div className="space-y-2">
            <p className="break-all text-xs text-muted-foreground">{location}</p>
            {listing.truncated && <p className="text-xs text-muted-foreground">Showing the first 1000 models.</p>}
            {listing.models.length > 12 && <>
              <Input aria-label="Filter models" placeholder="Filter models" value={filter} onChange={(event) => setFilter(event.target.value)} />
              <label className="flex min-h-11 items-center gap-2 text-xs">
                <input type="checkbox" className="h-4 w-4 accent-primary" checked={available.length > 0 && available.every((model) => selected.has(key(index, model.name)))}
                  disabled={action.busy || !available.length} onChange={(event) => toggle(available.map((model) => key(index, model.name)), event.target.checked)} />Select all shown
              </label>
            </>}
            <ul className="divide-y divide-border">{shown.map((model) => {
              const added = listing.protocols.every((protocol) => model.registered.includes(protocol));
              return <li key={model.name} className="py-1">
                <label className="flex min-h-11 items-start gap-2 py-2 text-xs">
                  <input type="checkbox" aria-label={model.name} className="h-4 w-4 shrink-0 accent-primary" checked={added || selected.has(key(index, model.name))}
                    disabled={action.busy || added || !model.addable} onChange={(event) => toggle([key(index, model.name)], event.target.checked)} />
                  <span className="min-w-0 space-y-1"><span className="break-all font-mono">{model.name}</span>
                    <span className="block text-muted-foreground">{listing.protocols.map((protocol) => protocolLabel(registry, protocol)).join(" + ")}
                      {model.context_window != null && ` · context ${model.context_window}`} · {model.builtin_price ? "priced by Looper" : "price 0 until you set one"}
                      {added && " · added"}{!model.addable && ` · ${model.problem}`}
                    </span>
                  </span>
                </label>
              </li>;
            })}</ul>
          </div>}
      </div>;
    })}
    {report && <div role="status" className="space-y-1 text-xs"><p>Added {report.added} models.</p>
      {report.skipped.map((item) => <p key={item}>{item}</p>)}{report.warnings.map((warning) => <p key={warning}>{warning}</p>)}
    </div>}
    {action.error && <p role="alert" className="text-xs text-destructive">{action.error}</p>}
    {entries.length > 200 && <p role="alert" className="text-xs text-destructive">Choose at most 200 models at a time.</p>}
    <div className="flex gap-2">
      <Button type="button" size="sm" disabled={action.busy || !entries.length || entries.length > 200} onClick={add}>Add {entries.length} {entries.length === 1 ? "model" : "models"}</Button>
      <Button type="button" size="sm" variant="ghost" disabled={action.busy} onClick={onDone}>Done</Button>
    </div>
  </div>;
}
