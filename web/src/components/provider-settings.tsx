import { useId, useState } from "react";
import { AlertTriangle, Loader2 } from "lucide-react";
import { Input } from "@/components/ui/input";
import { Badge } from "@/components/ui/badge";
import { Switch } from "@/components/ui/switch";
import { Button } from "@/components/ui/button";
import { EDIT_BOX, EditDelete, FormFooter, LABEL, RegistryList, Row, RowName, SELECT } from "@/components/registry-ui";
import {
  clearLoginProblem, createProvider, deleteProvider, updateProvider, setProviderEnabled,
  type ApiFamily, type ProviderEndpoint, type CreateProviderFields, type Provider, type Registry,
  type VendorKind, type VendorType,
} from "@/lib/api";
import { LOGIN_HELP, loginUsable } from "@/lib/runtime-catalog";
import { AGENT_PROFILE } from "@/lib/settings-pages";
import { useAction } from "@/lib/use-action";
import { useRegistry } from "@/lib/use-registry";
import { useSettingsTarget } from "@/lib/settings-target";
import { ProviderModelPicker } from "@/components/provider-model-picker";
import { ModelsSection } from "@/components/model-settings";

const usedBy = (count: number) =>
  count === 0 ? `no ${AGENT_PROFILE.plural}` : `used by ${count} ${count === 1 ? AGENT_PROFILE.name : AGENT_PROFILE.plural}`;

interface ProviderDraft {
  name: string;
  vendorType: VendorKind;
  endpoints: ProviderEndpoint[];
  format: ApiFamily | "";
  openaiApis: "both" | "openai-chat" | "openai-responses";
  baseUrl: string;
  perProtocol: boolean;
  maxConcurrency: string;
  env: string;
  apiKey: string;
  removeKey: boolean;
  urlsConfirmed: boolean;
}

const envText = (env: { key: string; value: string }[]) => env.map((e) => `${e.key}=${e.value}`).join("\n");
const numberOrTyped = (raw: string) => {
  const value = raw.trim();
  if (!value) return null;
  const number = Number(value);
  return Number.isFinite(number) ? number : value;
};

/** Invalid entries reach the server so its validation can explain the problem. */
function envEntries(raw: string) {
  return raw.split("\n").map((line) => line.trim()).filter(Boolean).map((line) => {
    const at = line.indexOf("=");
    return at < 0 ? { key: line, value: "" } : { key: line.slice(0, at).trim(), value: line.slice(at + 1) };
  });
}

function providerDraft(provider: Provider | null): ProviderDraft {
  return {
    name: provider?.name ?? "", vendorType: provider?.vendor_type ?? "custom",
    endpoints: provider?.endpoints.map((endpoint) => ({ ...endpoint })) ?? [],
    format: "", openaiApis: "both", baseUrl: "", perProtocol: false,
    maxConcurrency: provider?.max_concurrency == null ? "" : String(provider.max_concurrency),
    env: envText(provider?.env ?? []), apiKey: "", removeKey: false, urlsConfirmed: false,
  };
}

/** Defaults are copied only when choosing a vendor for a new account. */
function presetDraft(vendor: VendorType): ProviderDraft {
  return {
    ...providerDraft(null), name: vendor.vendor_type === "custom" ? "" : vendor.vendor_type,
    vendorType: vendor.vendor_type, endpoints: vendor.endpoints.map((endpoint) => ({ ...endpoint })),
    env: envText(vendor.env),
  };
}

function simpleEndpoints(draft: ProviderDraft, registry: Registry): ProviderEndpoint[] {
  return (registry.limits.api_formats.find((format) => format.family === draft.format)?.protocols ?? [])
    .filter(({ protocol }) => draft.format !== "openai" || draft.openaiApis === "both" || protocol === draft.openaiApis)
    .map(({ protocol }) => ({ protocol, base_url: draft.baseUrl, use_runtime_default: false }));
}

function simpleBaseUrl(draft: ProviderDraft): string {
  const url = draft.baseUrl.trim();
  return draft.format === "anthropic" ? url.replace(/\/v1\/?$/, "") : url;
}

function providerBody(draft: ProviderDraft, provider: Provider | null, registry: Registry): CreateProviderFields {
  const simple = !provider && draft.vendorType === "custom" && !draft.perProtocol;
  const fields: CreateProviderFields = {
    name: draft.name.trim(), vendor_type: draft.vendorType,
    endpoints: (simple ? simpleEndpoints(draft, registry) : draft.endpoints).map(({ protocol, base_url, use_runtime_default }) => {
      const previous = provider?.endpoints.find((endpoint) => endpoint.protocol === protocol);
      const url = simple ? simpleBaseUrl(draft) : previous?.base_url === base_url ? base_url : base_url.trim();
      return { protocol, base_url: url, use_runtime_default: url ? false : use_runtime_default };
    }),
    env: envEntries(draft.env), max_concurrency: numberOrTyped(draft.maxConcurrency),
  };
  const auth = provider?.auth ?? registry.vendor_types.find((v) => v.vendor_type === draft.vendorType)?.auth;
  if (auth !== "oauth") {
    if (provider?.api_key) fields.api_key = draft.removeKey ? null : draft.apiKey.trim() ? draft.apiKey : registry.limits.secret_mask;
    else if (draft.apiKey.trim()) fields.api_key = draft.apiKey;
  }
  return fields;
}

function ProviderForm({ provider, registry, onDone }: { provider: Provider | null; registry: Registry; onDone: () => void }) {
  const id = useId();
  const [draft, setDraft] = useState<ProviderDraft>(() => providerDraft(provider));
  const [savedProvider, setSavedProvider] = useState<Provider | null>(null);
  const [warnings, setWarnings] = useState<string[]>([]);
  const [validationError, setValidationError] = useState("");
  const action = useAction();
  const set = (change: Partial<ProviderDraft>) => setDraft((d) => ({ ...d, ...change }));
  const vendor = registry.vendor_types.find((v) => v.vendor_type === draft.vendorType);
  const login = (provider?.auth ?? vendor?.auth) === "oauth";
  const keyStored = !!provider?.api_key;
  const needsConfirmation = !provider && !!vendor?.requires_url_confirmation;
  const simple = !provider && draft.vendorType === "custom" && !draft.perProtocol;
  const simpleUrl = simpleBaseUrl(draft);
  const displayUrl = simpleUrl.replace(/\/+$/, "") || "<base URL>";
  const openaiCalls = draft.openaiApis === "both"
    ? `${displayUrl}/responses or ${displayUrl}/chat/completions`
    : `${displayUrl}/${draft.openaiApis === "openai-responses" ? "responses" : "chat/completions"}`;
  const save = async (e: React.FormEvent) => {
    e.preventDefault();
    if (simple && !draft.format) {
      setValidationError("Choose the API format.");
      return;
    }
    if (simple && !draft.baseUrl.trim()) {
      setValidationError("Enter the base URL.");
      return;
    }
    if (needsConfirmation && !draft.urlsConfirmed) {
      setValidationError("Confirm the endpoint URLs before saving this provider.");
      return;
    }
    setValidationError("");
    const body = providerBody(draft, provider, registry);
    let answer: { provider: Provider; warnings: string[] } | null = null;
    const done = await action.run(async () => {
      answer = provider ? await updateProvider(provider.provider_id, body) : await createProvider(body);
    });
    const result = answer as { provider: Provider; warnings: string[] } | null;
    if (!done || !result) return;
    // Do not retain a submitted secret while showing a server warning.
    set({ apiKey: "" });
    setWarnings(result.warnings);
    if (!provider && result.provider.auth === "api_key") setSavedProvider(result.provider);
    else if (!result.warnings.length) onDone();
  };

  if (savedProvider) return <form onSubmit={(event) => event.preventDefault()} aria-label="Add a provider" className="space-y-3">
    {warnings.length > 0 && <div role="status" className="space-y-1 text-xs text-warning">
      <p className="font-medium">Saved, with a warning:</p>{warnings.map((warning) => <p key={warning}>{warning}</p>)}
    </div>}
    <ProviderModelPicker provider={savedProvider} registry={registry} intro={`Saved. Tick the models ${savedProvider.name} offers to add them:`} onDone={onDone} />
  </form>;

  return (
    <form onSubmit={save} aria-label={provider ? `Edit provider ${provider.name}` : "Add a provider"} className="space-y-3">
      <div className="grid gap-3 sm:grid-cols-3">
        <div className="space-y-1">
          <label htmlFor={`${id}-name`} className={LABEL}>Name *</label>
          <Input id={`${id}-name`} value={draft.name} maxLength={registry.limits.name_max_chars} onChange={(e) => set({ name: e.target.value })} placeholder="anthropic-api" spellCheck={false} autoCapitalize="none" autoFocus />
        </div>
        <div className="space-y-1">
          <label htmlFor={`${id}-vendor`} className={LABEL}>Vendor</label>
          <select id={`${id}-vendor`} value={draft.vendorType} disabled={!!provider} onChange={(e) => {
            const chosen = registry.vendor_types.find((v) => v.vendor_type === e.target.value);
            if (chosen) { setDraft(presetDraft(chosen)); setValidationError(""); }
          }} className={SELECT}>
            {registry.vendor_types.map((v) => <option key={v.vendor_type} value={v.vendor_type}>{v.vendor_type === "custom" ? "Custom" : v.vendor_type}</option>)}
          </select>
        </div>
        <div className="space-y-1">
          <label htmlFor={`${id}-concurrency`} className={LABEL}>Max concurrent runs</label>
          <Input id={`${id}-concurrency`} inputMode="numeric" value={draft.maxConcurrency} onChange={(e) => set({ maxConcurrency: e.target.value })} placeholder="No limit" />
        </div>
      </div>
      {login && <p className="text-xs text-muted-foreground">Uses the login of {provider?.oauth_runtime_type ?? vendor?.oauth_runtime_type} runtimes.</p>}
      {simple ? <>
        <fieldset className="space-y-2">
          <legend className={LABEL}>API format *</legend>
          {registry.limits.api_formats.map((format) => <div key={format.family} className="space-y-1">
            <label className="flex items-center gap-1.5 text-xs">
              <input type="radio" name={`${id}-format`} value={format.family} className="h-4 w-4 accent-primary"
                checked={draft.format === format.family} onChange={() => set({ format: format.family })} />
              {format.label}
            </label>
            <p className="text-[11px] text-muted-foreground">Spoken by {registry.runtime_types
              .filter((runtime) => runtime.protocols.some((protocol) => format.protocols.some((entry) => entry.protocol === protocol)))
              .map((runtime) => runtime.label).join(", ")}</p>
          </div>)}
        </fieldset>
        {draft.format === "openai" && <div className="space-y-1">
          <label htmlFor={`${id}-openai-apis`} className={LABEL}>OpenAI APIs it serves</label>
          <select id={`${id}-openai-apis`} className={SELECT} value={draft.openaiApis}
            onChange={(event) => set({ openaiApis: event.target.value as ProviderDraft["openaiApis"] })}>
            <option value="both">Responses and Chat Completions</option>
            <option value="openai-chat">Chat Completions only</option>
            <option value="openai-responses">Responses only</option>
          </select>
        </div>}
        <div className="space-y-1">
          <label htmlFor={`${id}-base-url`} className={LABEL}>Base URL *</label>
          <Input id={`${id}-base-url`} className="font-mono" spellCheck={false} autoCapitalize="none" value={draft.baseUrl}
            onChange={(event) => set({ baseUrl: event.target.value })}
            placeholder={draft.format === "anthropic" ? "https://api.example.com/anthropic" : "https://api.example.com/v1"} />
          {draft.format === "anthropic" && simpleUrl !== draft.baseUrl.trim() && <p className="break-words text-[11px] text-muted-foreground">
            Looper adds /v1/messages itself, so this is saved as {simpleUrl}.
          </p>}
          <p className="break-words text-[11px] text-muted-foreground">{draft.format === "anthropic"
            ? `Runs call ${displayUrl}/v1/messages; the model list is ${displayUrl}/v1/models.`
            : `Runs call ${openaiCalls}; the model list is ${displayUrl}/models.`}</p>
        </div>
        <Button type="button" variant="ghost" size="sm" className="text-primary underline" onClick={() => set({ perProtocol: true,
          endpoints: !draft.format && draft.baseUrl.trim()
            ? registry.limits.protocols.map((protocol) => ({ protocol: protocol as ProviderEndpoint["protocol"], base_url: draft.baseUrl, use_runtime_default: false }))
            : simpleEndpoints(draft, registry) })}>
          Set a URL per protocol instead
        </Button>
      </> : <fieldset className="space-y-2">
        <legend className={LABEL}>Endpoints</legend>
        {registry.limits.protocols.map((protocol) => {
          const endpoint = draft.endpoints.find((e) => e.protocol === protocol);
          return (
            <div key={protocol} className="space-y-1">
              <label className="flex items-center gap-1.5 font-mono text-xs">
                <input type="checkbox" className="h-4 w-4 accent-primary" checked={!!endpoint} disabled={login}
                  onChange={(e) => set({ endpoints: e.target.checked
                    ? [...draft.endpoints, { protocol: protocol as ProviderEndpoint["protocol"], base_url: "", use_runtime_default: false }]
                    : draft.endpoints.filter((entry) => entry.protocol !== protocol), urlsConfirmed: false })} />
                {protocol}
              </label>
              {endpoint && <div className="space-y-1">
                <label htmlFor={`${id}-${protocol}-url`} className={LABEL}>{protocol} URL</label>
                <Input id={`${id}-${protocol}-url`} value={endpoint.base_url} disabled={login}
                  onChange={(e) => set({ endpoints: draft.endpoints.map((entry) => entry.protocol === protocol
                    ? { ...entry, base_url: e.target.value } : entry), urlsConfirmed: false })}
                  placeholder={login ? "Runtime login" : endpoint.use_runtime_default ? "Migrated runtime default"
                    : protocol === "anthropic-messages" ? "https://api.example.com" : "https://api.example.com/v1"}
                  spellCheck={false} autoCapitalize="none" className="font-mono" />
              </div>}
            </div>
          );
        })}
      </fieldset>}
      {!provider && draft.vendorType === "custom" && draft.perProtocol && <Button type="button" variant="ghost" size="sm" className="text-primary underline" onClick={() => {
        const first = draft.endpoints[0];
        const format = registry.limits.api_formats.find((format) => format.protocols.some(({ protocol }) => protocol === first?.protocol));
        if (draft.endpoints.some((endpoint) => endpoint.base_url.trim() !== first?.base_url.trim()
          || !format?.protocols.some(({ protocol }) => protocol === endpoint.protocol))) {
          setValidationError("This provider uses different URLs per protocol; keep editing it per protocol.");
          return;
        }
        const openai = draft.endpoints.filter((endpoint) => format?.protocols.some(({ protocol }) => protocol === endpoint.protocol));
        setValidationError("");
        set({ perProtocol: false, baseUrl: first?.base_url ?? draft.baseUrl, format: format?.family ?? draft.format,
          openaiApis: !first ? draft.openaiApis : format?.family === "openai" && openai.length === 1 ? first.protocol as ProviderDraft["openaiApis"] : "both" });
      }}>Describe it by API format instead</Button>}
      {needsConfirmation && <label className="flex items-center gap-1.5 text-xs text-muted-foreground">
        <input type="checkbox" aria-label="Confirm endpoint URLs" className="h-4 w-4 accent-primary" checked={draft.urlsConfirmed}
          onChange={(e) => set({ urlsConfirmed: e.target.checked })} />
        I have checked these endpoint URLs for this account.
      </label>}
      {!login && <div className="space-y-1">
        <label htmlFor={`${id}-key`} className={LABEL}>API key</label>
        <div className="flex flex-wrap items-center gap-3">
          <Input id={`${id}-key`} type="password" autoComplete="new-password" value={draft.apiKey}
            onChange={(e) => set({ apiKey: e.target.value })} disabled={draft.removeKey}
            placeholder={keyStored ? "Stored; leave empty to keep it" : "Not set"} className="min-w-48 flex-1 font-mono" />
          {keyStored && <label className="flex items-center gap-1.5 text-xs text-muted-foreground">
            <input type="checkbox" className="h-4 w-4 accent-primary" checked={draft.removeKey} onChange={(e) => set({ removeKey: e.target.checked, apiKey: "" })} />
            Remove the stored key
          </label>}
        </div>
        <p className="text-[11px] text-muted-foreground">Stored in Looper&apos;s database and never shown again.</p>
      </div>}
      <div className="space-y-1">
        <label htmlFor={`${id}-env`} className={LABEL}>Extra environment</label>
        <textarea id={`${id}-env`} value={draft.env} onChange={(e) => set({ env: e.target.value })}
          rows={Math.max(2, draft.env.split("\n").length)} placeholder="API_TIMEOUT_MS=3000000" spellCheck={false}
          className="w-full rounded-md border border-input bg-transparent px-3 py-2 font-mono text-xs" />
        <p className="text-[11px] text-muted-foreground">
          One KEY=value per line, added to the runs that use this provider. Not for secrets or
          runtime-owned variables ({registry.limits.provider_env_owned.join(", ")}).
        </p>
      </div>
      {warnings.length > 0 ? <div role="status" className="space-y-1 rounded-md border border-warning/40 bg-warning/10 px-3 py-2 text-xs">
        <p className="font-medium">Saved, with a warning:</p>
        {warnings.map((warning) => <p key={warning}>{warning}</p>)}
        <Button type="button" size="sm" variant="outline" onClick={onDone}>OK</Button>
      </div> : <FormFooter error={validationError || action.error} busy={action.busy} submit={provider ? "Save" : "Add provider"} onCancel={onDone} />}
    </form>
  );
}

/** An OAuth provider's login as the server reads it; after a run found it forked, what to check and the way to
 * let runs use it again. */
function LoginLine({ provider, label }: { provider: Provider; label: string }) {
  const action = useAction();
  const login = provider.login;
  if (!login) return null;
  if (login.state === "checking") return <p role="status" className="text-xs text-muted-foreground">{login.detail}</p>;
  if (loginUsable(login.state)) return <p className="text-xs text-muted-foreground">Login: {login.detail}</p>;
  const help = LOGIN_HELP[provider.oauth_runtime_type ?? ""] ?? { signIn: "" };
  const reuse = () => {
    const check = help.check ? `\`${help.check}\`` : "the login";
    if (confirm(`Use the ${label} login again? Check ${check} on the server's machine first.`)) {
      action.run(() => clearLoginProblem(provider.provider_id));
    }
  };
  return (
    <div role="status" className="space-y-1 text-xs text-warning">
      <p className="flex items-start gap-1">
        <AlertTriangle className="mt-0.5 h-3.5 w-3.5 shrink-0" aria-hidden="true" />
        <span>
          {login.state === "unavailable" ? `Login unavailable ${login.detail}` : `Login not usable: ${login.detail}`}.{" "}
          {login.state === "unavailable" ? (
            <>Runs with it do not start. Check {help.check ? <code className="font-mono">{help.check}</code> : "the login"} on
              the server&apos;s machine (<code className="font-mono">{help.signIn}</code> if it is not logged in), then use it
              again.</>
          ) : (
            <>{provider.oauth_runtime_type === "coco" && help.check ? <>Check <code className="font-mono">{help.check}</code>; run </> : "Run "}<code className="font-mono">{help.signIn}</code>{help.how ? ` ${help.how}` : ""} on the server&apos;s
              machine.</>
          )}
        </span>
      </p>
      {login.state === "unavailable" && (
        <Button type="button" size="sm" variant="outline" disabled={action.busy} onClick={reuse}>
          Use the login again
        </Button>
      )}
      {action.error && <p role="alert" className="text-destructive">{action.error}</p>}
    </div>
  );
}

function ProviderRow({ provider, registry }: { provider: Provider; registry: Registry }) {
  const [editing, setEditing] = useState(false);
  const [expanded, setExpanded] = useState(false);
  const [warnings, setWarnings] = useState<string[]>([]);
  const toggle = useAction();
  const setEnabled = (enabled: boolean) => {
    if (!enabled && provider.profile_count > 0 && !confirm(
      `Disable provider "${provider.name}"? ${provider.profile_count} Agent Profile(s) use it. They keep their selections, but their runs will not start until you enable it again.`,
    )) return;
    toggle.run(async () => {
      const result = await setProviderEnabled(provider.provider_id, enabled);
      setWarnings(result.warnings);
    });
  };
  const [fetching, setFetching] = useState(false);
  // An import's "secrets to fill in" links here (#settings/providers/<id>) to open the form.
  const ref = useSettingsTarget<HTMLLIElement>("provider", provider.provider_id, () => setEditing(!provider.builtin));
  const action = useAction();
  const remove = () => {
    if (confirm(`Delete provider "${provider.name}"?`)) action.run(() => deleteProvider(provider.provider_id));
  };
  const oauthLabel = registry.runtime_types.find((t) => t.type === provider.oauth_runtime_type)?.label ?? provider.oauth_runtime_type;
  const details = [
    !provider.enabled ? "Disabled: its Agent Profiles do not run" : "",
    provider.auth === "oauth" ? `OAuth: the login of ${oauthLabel} runtimes` : `API key: ${provider.api_key ?? "not set"}`,
    provider.vendor_type,
    ...provider.endpoints.map((endpoint) => `${endpoint.protocol}: ${endpoint.base_url || (endpoint.use_runtime_default ? "runtime default" : "runtime login")}`),
    `${provider.model_count} ${provider.model_count === 1 ? "model" : "models"}`,
    provider.max_concurrency == null ? "no concurrency limit" : `at most ${provider.max_concurrency} runs at once`,
    provider.env.length ? `env ${provider.env.map((e) => e.key).join(", ")}` : "",
    usedBy(provider.profile_count),
  ].filter(Boolean);
  return (
    <Row
      ref={ref}
      title={<><RowName name={provider.name} builtin={provider.builtin} />
        {!provider.enabled && <Badge variant="warning">Disabled</Badge>}</>}
      actions={
        <>
          <Switch checked={provider.enabled} label={`Enable provider ${provider.name}`} disabled={toggle.busy} onChange={setEnabled} />
          {provider.auth === "api_key" && <Button type="button" size="sm" variant="ghost"
            aria-label={`Fetch the models of provider ${provider.name}`} aria-expanded={fetching}
            onClick={() => setFetching(!fetching)}>Fetch models</Button>}
          <Button type="button" size="sm" variant="ghost" aria-label={`Models for provider ${provider.name}`}
            aria-expanded={expanded} onClick={() => setExpanded(!expanded)}>Models</Button>
          {!provider.builtin && (
            <EditDelete what="provider" name={provider.name} editing={editing} busy={action.busy} onEdit={() => setEditing(!editing)} onDelete={remove} />
          )}
        </>
      }
      below={
        <>
          {action.error && <p role="alert" className="text-xs text-destructive">{action.error}</p>}
          {toggle.error && <p role="alert" className="text-xs text-destructive">{toggle.error}</p>}
          {warnings.length > 0 && (
            <div role="status" className="space-y-2 rounded-md border border-warning/40 bg-warning/5 p-3 text-xs text-warning">
              <p>Disabled, with these effects:</p>
              <ul className="list-disc space-y-1 pl-4">{warnings.map((warning, index) => <li key={index}>{warning}</li>)}</ul>
              <Button type="button" size="sm" variant="outline" onClick={() => setWarnings([])}>OK</Button>
            </div>
          )}
          {editing && (
            <div className={EDIT_BOX}>
              <ProviderForm provider={provider} registry={registry} onDone={() => setEditing(false)} />
            </div>
          )}
          {fetching && <div className={EDIT_BOX}><ProviderModelPicker provider={provider} registry={registry} onDone={() => setFetching(false)} /></div>}
          {expanded && <div className="pt-2"><ModelsSection provider={provider} registry={registry} /></div>}
        </>
      }
    >
      <p className="break-words text-xs text-muted-foreground">{details.join(" · ")}</p>
      {provider.login && <LoginLine provider={provider} label={oauthLabel ?? "runtime"} />}
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
