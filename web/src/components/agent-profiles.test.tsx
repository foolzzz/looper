// @vitest-environment happy-dom
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { RuntimesSection } from "@/components/runtime-cards";
import { ClaudeCheckBanner } from "@/components/claude-check-banner";
import { TaskAgentsSection } from "@/components/task-agents";
import { profileLabel, useRegistry } from "@/lib/use-registry";
import { AgentProfileForm, AgentProfilesSection, AgentProfileSelect } from "@/components/agent-profiles";
import { fetchPromptPreview, REGISTRY_CHANGED_EVENT, type AgentProfile, type Registry, type Model, type Provider, type TaskAgents, type Runtime, type RuntimeType } from "@/lib/api";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

const REFUSAL = "the Stand-in runtime cannot replace its default system prompt";
const PROFILE: AgentProfile = {
  model_path: "direct", profile_id: "p", name: "Profile", model_id: "model-messages",
  model: { model_id: "model-messages", name: "model", protocol: "anthropic-messages", provider_id: "provider" }, effort: null, agent_teams: true,
  custom_prompt: "", runtime_id: "rt-claude-code", runtime_type: "claude-code",
  runtime_name: "Claude Code", builtin: false,
  provider: { provider_id: "provider", name: "Provider", vendor_type: "custom", auth: "api_key", enabled: true },
  roles: ["worker"], used_by: { layers: [], task_count: 0, schedule_count: 0, allowed_in: [] }, warnings: [],
  cost: { source: "runtime", equivalent: false, unpriced: [] },
};
const provider = (id: string, extra: Partial<Provider> = {}): Provider => ({
  provider_id: id, name: id === "provider" ? "Provider" : id, vendor_type: "custom", auth: "api_key",
  oauth_runtime_type: null, endpoints: ["anthropic-messages", "openai-chat", "openai-responses"].map((protocol) => ({
    protocol: protocol as Model["protocol"], base_url: "https://example.com", use_runtime_default: false,
  })), suggested_models: [], login: null, env: [], max_concurrency: null, api_key: null, builtin: false, enabled: true,
  endpoint_count: 3, model_count: 0, profile_count: 1, created_at: "", updated_at: "", ...extra,
});
const model = (id: string, protocol: Model["protocol"], providerId = "provider"): Model => ({
  model_id: id, name: "model", protocol, provider_id: providerId, provider_name: providerId === "provider" ? "Provider" : providerId, provider_enabled: true,
  chat_dialect: null, effective_chat_dialect: null, context_window: null, input_usd_per_mtok: null, output_usd_per_mtok: null, cache_read_usd_per_mtok: null,
  cache_write_usd_per_mtok: null, efforts: null, background_model: null, catalog_order: 0, builtin: false,
  profile_count: 0, endpoint: { protocol, base_url: "https://example.com", use_runtime_default: false },
  prices: {} as Model["prices"], warnings: [], created_at: "", updated_at: "",
});
const REGISTRY: Registry = {
  runtime_types: ["claude-code", "codex", "stand-in"].map<Registry["runtime_types"][number]>((type) => ({
    translated_protocols: [], type, in_process: false, label: type === "stand-in" ? "Stand-in" : "Claude Code", default_executable: type,
    runtime_id: `rt-${type}`, protocols: type === "codex" ? ["openai-responses"] : type === "stand-in" ? ["anthropic-messages", "openai-chat"] : ["anthropic-messages"], capabilities: [], roles: ["worker"],
    auth_kinds: ["api_key", "oauth"], can_replace_system_prompt: type !== "stand-in", can_interrupt: type === "claude-code",
    supported_models: null,
    loads_operator_config: false,
    replace_system_prompt_refusal: type === "stand-in" ? REFUSAL : null,
  })),
  runtimes: ["claude-code", "codex", "stand-in"].map<Registry["runtimes"][number]>((type) => ({
    runtime_id: `rt-${type}`, name: type, type, type_label: type, executable: "", default_executable: type,
    path: null, in_process: false, protocols: type === "codex" ? ["openai-responses"] : type === "stand-in" ? ["anthropic-messages", "openai-chat"] : ["anthropic-messages"], version: null, capabilities: [], roles: ["worker"],
    probed_at: null, probe_error: null, builtin: true, profile_count: 0,
    auth_kinds: ["api_key", "oauth"],
  })),
  providers: [provider("provider"), provider("other"), provider("codex-login", { auth: "oauth", oauth_runtime_type: "codex" }),
    provider("foreign-login", { auth: "oauth", oauth_runtime_type: "grok" })],
  models: [model("model-messages", "anthropic-messages"), model("model-chat", "openai-chat"),
    model("model-responses", "openai-responses"), model("other-messages", "anthropic-messages", "other"),
    model("codex-model", "openai-responses", "codex-login"), model("foreign-model", "openai-responses", "foreign-login")],
  profiles: [PROFILE], vendor_types: [], prices: { as_of: "", sources: [], models: {} },
  limits: { api_formats: [{ family: "anthropic", label: "Anthropic", protocols: [{ protocol: "anthropic-messages", label: "Anthropic Messages" }] }, { family: "openai", label: "OpenAI", protocols: [{ protocol: "openai-responses", label: "OpenAI Responses" }, { protocol: "openai-chat", label: "OpenAI Chat Completions" }] }],
    model_fields: [], name_max_chars: 100, models_max: 10, price_fields: [], protocols: ["anthropic-messages", "openai-chat", "openai-responses"], auth_kinds: ["api_key"],
    effort_levels: ["high", "xhigh"], role_capabilities: { manager: [], planner: [], worker: [], reviewer: [] },
    secret_mask: "", provider_env_owned: [],
  },
};

describe("Agent Profiles", () => {
  let container: HTMLDivElement;
  let root: Root;
  let requests: { url: string; body: Record<string, unknown> }[];
  let registry: Registry;

  beforeEach(() => {
    requests = [];
    registry = structuredClone(REGISTRY);
    vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      if (url === "/api/v1/system/status") {
        return Response.json({ status: "ok", result: { checks: [] } });
      }
      if (url === "/api/v1/registry/view") return Response.json({ status: "ok", result: registry });
      if (init?.body) requests.push({ url, body: (JSON.parse(String(init.body)).fields ?? JSON.parse(String(init.body))) });
      const answer = { profile: PROFILE, warnings: [] };
      return new Response(JSON.stringify({ status: "ok", result: answer }), { status: 200 });
    }));
    container = document.createElement("div");
    document.body.append(container);
    root = createRoot(container);
  });
  afterEach(() => {
    act(() => root.unmount());
    container.remove();
    vi.useRealTimers();
    vi.unstubAllGlobals();
  });

  const show = async (profile: AgentProfile | null = PROFILE, data = registry) => {
    await act(async () => root.render(<AgentProfileForm profile={profile} registry={data} onDone={() => {}} />));
  };
  const select = (label: string) => {
    const field = [...container.querySelectorAll("label")].find((item) => item.textContent === label);
    expect(field, label).toBeTruthy();
    return document.getElementById(field!.htmlFor) as HTMLSelectElement;
  };
  const choose = async (label: string, value: string) => {
    await act(async () => {
      const field = select(label);
      field.value = value;
      field.dispatchEvent(new Event("change", { bubbles: true }));
    });
  };

  const CODEX: AgentProfile = { ...PROFILE, model_id: "model-responses", model: { ...PROFILE.model, model_id: "model-responses", protocol: "openai-responses" }, runtime_id: "rt-codex", runtime_type: "codex", agent_teams: false };

  it("keeps Claude Code selectable for chat models and labels their gateway path", async () => {
    registry.runtime_types[0].translated_protocols = ["openai-chat"];
    await show();
    await choose("Protocol *", "openai-chat");
    const option = select("Runtime").querySelector<HTMLOptionElement>('option[value="rt-claude-code"]')!;
    expect(option.disabled).toBe(false);
    expect(option.textContent).toContain("via local gateway");
    expect(select("Runtime").value).toBe("rt-claude-code");
    expect(container.textContent).toContain("Runtimes that reach it through the local gateway: Claude Code");
    expect(container.textContent).toContain("Runs are refused when the gateway is unavailable.");
    await submit();
    expect(requests[0].body).toMatchObject({ runtime_id: "rt-claude-code", model_id: "model-chat" });
    registry.profiles = [{ ...PROFILE, model_path: "translated" }];
    await act(async () => root.render(<AgentProfilesSection />));
    expect(container.querySelector('[data-slot="badge"]')?.textContent ?? container.textContent).toContain("via local gateway");
  });

  it.each([
    ["oauth", "native", "login providers cannot be translated"],
    ["api_key", "ttadk", "TTADK manages its own provider; use the native launcher"],
  ] as const)("explains the translated pairing refusal for %s/%s", async (auth, launcher, reason) => {
    registry.runtime_types[0].translated_protocols = ["openai-chat"];
    registry.providers[0].auth = auth;
    registry.runtimes[0].launcher = launcher;
    await show();
    await choose("Protocol *", "openai-chat");
    const option = select("Runtime").querySelector<HTMLOptionElement>('option[value="rt-claude-code"]')!;
    expect(option.disabled).toBe(true);
    expect(option.textContent).toContain(reason);
  });

  it("re-reads pending startup checks and shows failures without a reload, then stops", async () => {
    vi.useFakeTimers();
    let checks = [{ id: "claude_cli", label: "Claude Code CLI", status: "pending", detail: "Checking…", fix: "Install Claude Code" }];
    const fetcher = vi.fn(async () => Response.json({ status: "ok", result: { checks } }));
    vi.stubGlobal("fetch", fetcher);
    await act(async () => root.render(<ClaudeCheckBanner />));
    expect(container.querySelector('[role="alert"]')).toBeNull();
    await act(async () => vi.advanceTimersByTimeAsync(2999));
    expect(fetcher).toHaveBeenCalledTimes(1);
    checks = [{ ...checks[0], status: "fail", detail: "CLI missing" },
      { id: "git", label: "git", status: "pending", detail: "Checking…", fix: "" }];
    await act(async () => vi.advanceTimersByTimeAsync(1));
    expect(container.querySelector('[role="alert"]')?.textContent).toContain("CLI missing");
    expect(fetcher).toHaveBeenCalledTimes(2);
    checks = [checks[0], { ...checks[1], status: "ok" }];
    await act(async () => vi.advanceTimersByTimeAsync(3000));
    expect(fetcher).toHaveBeenCalledTimes(3);
    await act(async () => vi.advanceTimersByTimeAsync(9000));
    expect(fetcher).toHaveBeenCalledTimes(3);
  });

  it("keeps polling pending checks after a transient read error and cancels on unmount", async () => {
    vi.useFakeTimers();
    const pending = () => Response.json({ status: "ok", result: { checks: [
      { id: "git", label: "git", status: "pending", detail: "Checking…", fix: "" },
    ] } });
    const fetcher = vi.fn().mockResolvedValueOnce(pending()).mockRejectedValueOnce(new Error("offline"))
      .mockResolvedValue(pending());
    vi.stubGlobal("fetch", fetcher);
    await act(async () => root.render(<ClaudeCheckBanner />));
    await act(async () => vi.advanceTimersByTimeAsync(6000));
    expect(fetcher).toHaveBeenCalledTimes(3);
    await act(async () => root.render(null));
    await act(async () => vi.advanceTimersByTimeAsync(6000));
    expect(fetcher).toHaveBeenCalledTimes(3);
  });

  it("refreshes the Agent Profile login while any startup check is pending", async () => {
    vi.useFakeTimers();
    registry.providers.push(provider("subscription", { auth: "oauth", oauth_runtime_type: "claude-code" }));
    const loginModel = model("subscription-model", "anthropic-messages", "subscription");
    registry.models.push(loginModel);
    const profile = { ...PROFILE, model_id: loginModel.model_id, model: { ...PROFILE.model, model_id: loginModel.model_id },
      provider: { ...PROFILE.provider, provider_id: "subscription", name: "subscription", auth: "oauth" as const } };
    let checks = [{ id: "claude_login", label: "Claude login", status: "pending", detail: "Checking…", fix: "" }];
    const fetcher = vi.fn(async (input: RequestInfo | URL) => Response.json({ status: "ok", result:
      String(input) === "/api/v1/system/status" ? { checks } : registry }));
    vi.stubGlobal("fetch", fetcher);
    await show(profile);
    expect(container.textContent).toContain("checking the login");
    checks = [{ ...checks[0], status: "fail", detail: "Login missing" }];
    await act(async () => vi.advanceTimersByTimeAsync(3000));
    expect(container.textContent).toContain("not logged in: run claude auth login");
    const reads = () => fetcher.mock.calls.filter(([input]) => String(input) === "/api/v1/system/status").length;
    expect(reads()).toBe(2);
    await act(async () => vi.advanceTimersByTimeAsync(9000));
    expect(reads()).toBe(2);
  });

  it("distinguishes TTADK from native Codex in the runtime dropdown and saved profile", async () => {
    const native = registry.runtimes.find((runtime) => runtime.runtime_id === "rt-codex")!;
    registry.runtimes.push({ ...native, runtime_id: "rt-ttadk-codex", name: "ttadk-codex", launcher: "ttadk" });
    registry.profiles = [{ ...CODEX, runtime_id: "rt-ttadk-codex", runtime_name: "ttadk-codex" }];
    await show(registry.profiles[0]);
    const runtimes = select("Runtime");
    expect(runtimes.querySelector('option[value="rt-codex"]')!.textContent).toBe("Codex CLI");
    expect(runtimes.selectedOptions[0].textContent).toBe("ttadk-codex (launcher: ttadk)");
    await act(async () => root.render(<AgentProfilesSection />));
    expect(container.textContent).toContain("ttadk-codex (launcher: ttadk) · Provider · model");
  });

  it("disables a TTADK runtime for an API key with the login reason", async () => {
    const native = registry.runtimes.find((runtime) => runtime.runtime_id === "rt-codex")!;
    registry.runtimes.push({ ...native, runtime_id: "rt-ttadk-codex", name: "ttadk-codex",
      launcher: "ttadk", auth_kinds: ["oauth"] });
    await show(CODEX);
    const option = select("Runtime").querySelector<HTMLOptionElement>('option[value="rt-ttadk-codex"]')!;
    expect(option.disabled).toBe(true);
    expect(option.textContent).toContain("takes only its own login");
  });

  it("shows failed Cursor and TTADK checks with their own fixes", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => Response.json({ status: "ok", result: { checks: [
      { id: "cursor_login", label: "Cursor login", status: "fail", detail: "Not signed in", fix: "Run cursor-agent login" },
      { id: "ttadk", label: "TTADK", status: "fail", detail: "Not available", fix: "Install ttadk" },
    ] } })));
    await act(async () => root.render(<ClaudeCheckBanner />));
    const problems = [...container.querySelectorAll('[role="alert"] p')].map((node) => node.textContent);
    expect(problems).toHaveLength(2);
    expect(problems[0]).toContain("Cursor login: Not signed in");
    expect(problems[0]).toContain("To fix: Run cursor-agent login");
    expect(problems[1]).toContain("TTADK: Not available");
    expect(problems[1]).toContain("To fix: Install ttadk");
    expect(container.textContent).not.toContain("then restart");
  });

  it.each([
    [null, "Profile"], ["high", "Profile · high"], ["xhigh", "Profile · xhigh"],
  ])("labels profiles with their derived provider, exact model and protocol (%s)", (effort, name) => {
    expect(profileLabel({ ...PROFILE, effort })).toBe(
      `${name} — Claude Code · Provider / model / anthropic-messages`,
    );
    expect(profileLabel({ ...PROFILE, name, effort })).toBe(
      `${name} — Claude Code · Provider / model / anthropic-messages`,
    );
  });

  it("marks disabled providers in every profile selector label", () => {
    expect(profileLabel({ ...PROFILE, provider: { ...PROFILE.provider, enabled: false } })).toBe(
      "Profile — Claude Code · Provider / model / anthropic-messages · provider disabled",
    );
  });

  it("explains disabled provider refusal on the affected profile row with a settings link", async () => {
    registry.profiles = [{ ...PROFILE, provider: { ...PROFILE.provider, enabled: false } }];
    await act(async () => root.render(<AgentProfilesSection />));
    expect(container.textContent).toContain("Provider Provider is disabled: this profile's runs do not start.");
    const link = [...container.querySelectorAll("a")].find((item) => item.textContent === "Enable it in LLM Providers")!;
    expect(link.getAttribute("href")).toBe("#settings/providers/provider");
  });

  it("offers enabled accounts and preserves only the edited profile's disabled model and identity", async () => {
    registry.providers[0].enabled = false;
    registry.models = registry.models.map((item) => ({ ...item, provider_enabled: item.provider_id !== "provider" }));
    await show(null);
    await input("Model *", "model");
    await choose("Protocol *", "anthropic-messages");
    expect([...select("Provider *").options].map((o) => o.value)).toEqual(["", "other"]);
    expect(select("Provider *").value).toBe("other");
    await act(async () => root.unmount()); root = createRoot(container);
    await show({ ...PROFILE, provider: { ...PROFILE.provider, enabled: false } });
    expect(select("Provider *").selectedOptions[0].textContent).toBe("Provider (disabled)");
    expect(container.textContent).toContain("Provider Provider is disabled: this profile's runs do not start.");
    expect([...select("Protocol *").options].map((o) => o.value)).not.toContain("openai-chat");
    await submit();
    expect(requests[0].body.model_id).toBe(PROFILE.model_id);
  });

  function RegistryReader({ id }: { id: string }) {
    const { registry, error, reload } = useRegistry();
    return <div id={id}><output>{registry?.profiles.map(profileLabel).join("; ")}</output>
      <span role="alert">{error}</span><button onClick={reload}>Reload</button></div>;
  }

  it("shares one registry fetch and refresh across mounted consumers and later selectors", async () => {
    let registry = REGISTRY;
    const fetcher = vi.fn(async () => Response.json({ status: "ok", result: registry }));
    vi.stubGlobal("fetch", fetcher);
    await act(async () => root.render(<><RegistryReader id="first" /><RegistryReader id="second" /></>));
    expect(fetcher).toHaveBeenCalledTimes(1);
    await act(async () => root.render(<><RegistryReader id="first" /><RegistryReader id="second" />
      <RegistryReader id="later" /></>));
    expect(fetcher).toHaveBeenCalledTimes(1);
    registry = { ...REGISTRY, profiles: [{ ...PROFILE, model: { ...PROFILE.model, protocol: "openai-chat" } }] };
    await act(async () => window.dispatchEvent(new Event(REGISTRY_CHANGED_EVENT)));
    expect(fetcher).toHaveBeenCalledTimes(2);
    for (const output of container.querySelectorAll("output")) {
      expect(output.textContent).toContain("Provider / model / openai-chat");
    }
  });

  it("retains the last registry after refresh failure and clears the shared error on retry", async () => {
    const fetcher = vi.fn().mockResolvedValueOnce(Response.json({ status: "ok", result: REGISTRY }))
      .mockRejectedValueOnce(new Error("Registry unavailable"))
      .mockResolvedValueOnce(Response.json({ status: "ok", result: { ...REGISTRY, profiles: [{ ...PROFILE, name: "Renamed" }] } }));
    vi.stubGlobal("fetch", fetcher);
    await act(async () => root.render(<RegistryReader id="reader" />));
    await act(async () => window.dispatchEvent(new Event(REGISTRY_CHANGED_EVENT)));
    expect(container.querySelector("output")!.textContent).toContain("Profile —");
    expect(container.querySelector('[role="alert"]')!.textContent).toBe("Registry unavailable");
    await act(async () => container.querySelector("button")!.click());
    expect(container.querySelector("output")!.textContent).toContain("Renamed —");
    expect(container.querySelector('[role="alert"]')!.textContent).toBe("");
  });

  it("ignores an older registry answer after a newer refresh completes", async () => {
    let resolveFirst!: (response: Response) => void;
    vi.stubGlobal("fetch", vi.fn().mockImplementationOnce(() => new Promise<Response>((resolve) => { resolveFirst = resolve; }))
      .mockResolvedValueOnce(Response.json({ status: "ok", result: { ...REGISTRY, profiles: [{ ...PROFILE, name: "Latest" }] } })));
    await act(async () => root.render(<RegistryReader id="reader" />));
    await act(async () => window.dispatchEvent(new Event(REGISTRY_CHANGED_EVENT)));
    await act(async () => resolveFirst(Response.json({ status: "ok", result: REGISTRY })));
    expect(container.querySelector("output")!.textContent).toContain("Latest —");
  });

  it.each([true, false])("shows the derived model in task summaries (editable %s)", async (editable) => {
    const role: TaskAgents["roles"]["worker"] = {
      profile: { value: { profile_id: "p", name: "Profile", model: "model", effort: null,
        runtime: "Claude Code", provider: "Provider" }, source: "system" },
      runtime_type: "claude-code", system_prompt_mode: { value: "append", source: "builtin" },
      concurrency: { value: 1, source: "builtin", task: null, default: { value: 1, source: "builtin" } },
    };
    const agents: TaskAgents = { concurrency_max: 64, editable, roles: { planner: role, worker: role, reviewer: role } };
    await act(async () => root.render(<TaskAgentsSection taskId="task" agents={agents} onSaved={() => {}} />));
    const summaries = [...container.querySelectorAll('p[aria-live="polite"]')].filter((item) => item.textContent?.includes("Profile"));
    expect(summaries).toHaveLength(3);
    for (const summary of summaries) expect(summary.textContent).toContain("Provider / model / anthropic-messages");
    expect(container.textContent).not.toContain("[object Object]");
  });

  it("selects an existing profile by profile_id", async () => {
    const change = vi.fn();
    await act(async () => root.render(<AgentProfileSelect id="pick" value="" onChange={change} emptyLabel="Default" />));
    await act(async () => {
      const field = container.querySelector<HTMLSelectElement>("#pick")!;
      field.value = "p"; field.dispatchEvent(new Event("change", { bubbles: true }));
    });
    expect(change).toHaveBeenCalledExactlyOnceWith("p");
  });

  const supported = ["deepseek-flash", "deepseek-flash[1m]", "deepseek-v4-pro", "deepseek-v4-pro[1m]"];
  const dsh = () => {
    registry.runtime_types.push({
      ...registry.runtime_types[0], type: "dsh", label: "DSH", runtime_id: "rt-dsh",
      default_executable: "dsh", protocols: ["anthropic-messages"], supported_models: supported,
      auth_kinds: ["api_key"], can_replace_system_prompt: false,
      replace_system_prompt_refusal: "the DSH runtime cannot replace its default system prompt",
    });
    registry.runtimes.push({ ...registry.runtimes[0], type: "dsh", runtime_id: "rt-dsh", type_label: "DSH", auth_kinds: ["api_key"] });

  };
  const registeredModels = (names: string[], providerId = "provider", protocol: Model["protocol"] = "anthropic-messages") =>
    names.map((name) => ({ ...model(`${providerId}-${name}`, protocol, providerId), name }));
  const modelOptions = () => [...container.querySelectorAll<HTMLDataListElement>("datalist option")].map((o) => o.getAttribute("value"));

  const apiRuntime = () => {
    registry.runtime_types.push({ ...registry.runtime_types[0], type: "api", label: "Model API", runtime_id: "rt-api",
      in_process: true, protocols: ["openai-responses"], roles: ["manager"], auth_kinds: ["api_key"], supported_models: null });
    registry.runtimes.push({ ...registry.runtimes[0], type: "api", runtime_id: "rt-api", type_label: "Model API",
      in_process: true, path: null, protocols: ["openai-responses"], roles: ["manager"], auth_kinds: ["api_key"] });
  };

  it("shows the package probe error for an unavailable in-process runtime", async () => {
    apiRuntime();
    const error = "the openai Python package is not installed with this Looper; reinstall Looper";
    registry.runtime_types.at(-1)!.probe_error = error;
    registry.runtimes = registry.runtimes.filter((runtime) => runtime.type !== "api");
    await show(null);
    const option = [...select("Runtime").options].find((item) => item.value === "rt-api")!;
    expect(option.disabled).toBe(true);
    expect(option.textContent).toContain(error);
    expect(option.textContent).not.toContain("PATH");
    expect(option.title).toBe(error);
  });

  it("keeps the PATH hint for an unavailable CLI runtime", async () => {
    registry.runtimes = registry.runtimes.filter((runtime) => runtime.type !== "codex");
    registry.runtime_types.find((type) => type.type === "codex")!.probe_error = "CLI probe failed";
    await show(null);
    const option = [...select("Runtime").options].find((item) => item.value === "rt-codex")!;
    expect(option.disabled).toBe(true);
    expect(option.textContent).toContain("not on the server's PATH");
    expect(option.textContent).not.toContain("CLI probe failed");
  });

  it("lets a detected API runtime be selected", async () => {
    apiRuntime();
    await show(CODEX);
    expect([...select("Runtime").options].find((option) => option.value === "rt-api")?.disabled).toBe(false);
    await choose("Runtime", "rt-api");
    expect(select("Runtime").value).toBe("rt-api");
  });

  it("disables an API runtime for protocols it does not speak", async () => {
    apiRuntime();
    await show();
    const option = select("Runtime").querySelector<HTMLOptionElement>('option[value="rt-api"]')!;
    expect(option.disabled).toBe(true);
    expect(option.textContent).toContain("speaks OpenAI Responses");
  });

  it("uses the shared effort list for the API runtime", async () => {
    apiRuntime();
    await show(CODEX);
    await choose("Runtime", "rt-api");
    expect([...select("Effort").options].map((option) => option.value)).toEqual(["", ...registry.limits.effort_levels]);
  });

  it("hides agent teams for the API runtime", async () => {
    apiRuntime();
    await show();
    expect(container.querySelector('input[type="checkbox"]')).not.toBeNull();
    await choose("Protocol *", "openai-responses");
    await choose("Provider *", "provider");
    await choose("Runtime", "rt-api");
    expect(container.querySelector('input[type="checkbox"]')).toBeNull();
    expect(container.textContent).not.toContain("Agent teams");
  });

  it("shows the DSH runtime card and pinned install command using the existing cards", async () => {
    dsh();
    await act(async () => root.render(<RuntimesSection />));
    expect([...container.querySelectorAll("h3")].map((h) => h.textContent)).toContain("DSH CLI");
    expect(container.textContent).toContain("npm install -g @deepseek-ai/dsh@0.2.1-rc.2");
    expect(container.textContent).toContain("Claude Code CLI");
    expect(container.textContent).toContain("Codex CLI");
  });

  it("enables restricted runtimes for their exact supported names and saves [1m]", async () => {
    dsh();
    registry.models = registeredModels(["unrelated", ...supported]);
    await show(null);
    await input("Model *", "deepseek-v4-pro[1m]");
    expect(select("Protocol *").value).toBe("anthropic-messages");
    expect(select("Provider *").value).toBe("provider");
    await choose("Runtime", "rt-dsh");
    await submit();
    expect(requests[0].body).toMatchObject({ runtime_id: "rt-dsh", model_id: "provider-deepseek-v4-pro[1m]", agent_teams: false });
    expect(container.textContent).toContain("the DSH runtime cannot replace its default system prompt");
  });

  it.each([
    ["openai-chat", "api_key", "deepseek-flash", "speaks Anthropic Messages"],
    ["anthropic-messages", "oauth", "deepseek-flash", "cannot use another runtime's login"],
    ["anthropic-messages", "api_key", "unrelated", `runs only ${supported.join(", ")}`],
  ] as const)("explains restricted runtime refusal for %s/%s/%s", async (protocol, auth, name, reason) => {
    dsh();
    registry.providers[0].auth = auth;
    registry.models = registeredModels([name], "provider", protocol);
    await show(null);
    await input("Model *", name);
    const option = select("Runtime").querySelector<HTMLOptionElement>('option[value="rt-dsh"]')!;
    expect(option.disabled).toBe(true);
    expect(option.textContent).toContain(reason);
  });

  it("does not offer unregistered suggestions and explains an empty catalog", async () => {
    dsh();
    registry.models = [];
    registry.providers[0].suggested_models = supported;
    await show(null);
    expect(modelOptions()).toEqual([]);
    expect(select("Protocol *").disabled).toBe(true);
    expect(container.textContent).toContain("No compatible models. Add a model in LLM Providers.");
    await submit();
    expect(requests).toEqual([]);
  });

  it("reads supported names from the runtime contract", async () => {
    dsh();
    registry.runtime_types.at(-1)!.supported_models = ["server-only-model"];
    registry.models = registeredModels(["server-only-model", supported[0]]);
    await show(null);
    await input("Model *", supported[0]);
    expect(select("Runtime").querySelector<HTMLOptionElement>('option[value="rt-dsh"]')!.textContent)
      .toContain("runs only server-only-model");
    await input("Model *", "server-only-model");
    expect(select("Runtime").querySelector<HTMLOptionElement>('option[value="rt-dsh"]')!.disabled).toBe(false);
  });

  it("opens a saved DSH profile with its exact [1m] model selected", async () => {
    dsh();
    registry.models = registeredModels(supported);
    await show({ ...PROFILE, runtime_id: "rt-dsh", runtime_type: "dsh", model_id: registry.models[3].model_id, model: registry.models[3], agent_teams: false });
    expect(select("Runtime").value).toBe("rt-dsh");
    expect(select("Model *").value).toBe(registry.models[3].name);
    expect(select("Provider *").value).toBe("provider");
  });

  it.each([PROFILE, CODEX])("has no system prompt choice on $runtime_type, which is set per role", async (profile) => {
    await show(profile);
    expect([...container.querySelectorAll("label")].some((l) => l.textContent === "System prompt")).toBe(false);
    expect(container.querySelector('[data-testid="prompt-mode-per-role"]')!.textContent)
      .toContain("set per role in Settings → Roles");
  });

  it("does not send a system_prompt_mode in the profile save body", async () => {
    await show(CODEX);
    await act(async () => container.querySelector("form")!.dispatchEvent(new Event("submit", { bubbles: true, cancelable: true })));
    expect(requests).toHaveLength(1);
    expect(requests[0].body).not.toHaveProperty("system_prompt_mode");
  });

  const submit = () => act(async () => container.querySelector("form")!.dispatchEvent(
    new Event("submit", { bubbles: true, cancelable: true })));

  const input = async (label: string, value: string) => {
    const field = select(label) as unknown as HTMLInputElement;
    await act(async () => {
      Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value")!.set!.call(field, value);
      field.dispatchEvent(new Event("input", { bubbles: true }));
    });
  };

  it("requires a model, protocol and provider and saves exactly the existing fields", async () => {
    await show(null);
    expect(select("Model *").tagName).toBe("INPUT");
    expect(select("Model *").value).toBe("");
    expect([...container.querySelectorAll("label")].slice(0, 5).map((l) => l.textContent))
      .toEqual(["Name *", "Model *", "Protocol *", "Provider *", "Runtime"]);
    await input("Name *", "New profile");
    await submit();
    expect(requests).toEqual([]);
    expect(container.querySelector('[role="alert"]')!.textContent)
      .toBe("Choose a model, its protocol and a provider before saving.");
    await input("Model *", "model");
    await choose("Protocol *", "anthropic-messages");
    await submit();
    expect(requests).toEqual([]);
    await choose("Provider *", "provider");
    await choose("Effort", "high");
    await submit();
    expect(requests[0].body).toEqual({ name: "New profile", runtime_id: "rt-claude-code", model_id: "model-messages",
      effort: "high", agent_teams: true, custom_prompt: "" });
  });

  it("switches an incompatible runtime with the reason and applies its effort and teams defaults", async () => {
    registry.runtime_types[1].effort_levels = ["high"];
    await show({ ...PROFILE, effort: "xhigh" });
    await choose("Protocol *", "openai-responses");
    await choose("Provider *", "provider");
    expect(select("Runtime").value).toBe("rt-codex");
    expect(select("Effort").value).toBe("");
    expect(container.textContent).toContain("Runtime changed to Codex CLI: Claude Code CLI speaks Anthropic Messages.");
    await submit();
    expect(requests[0].body.agent_teams).toBe(false);
    await choose("Protocol *", "anthropic-messages");
    await choose("Provider *", "provider");
    expect(container.querySelector<HTMLInputElement>('input[type="checkbox"]')!.checked).toBe(true);
    await choose("Runtime", "rt-stand-in");
    expect(container.textContent).not.toContain("Runtime changed to");
  });

  it("offers a login model but enables only its owning runtime", async () => {
    registry.models = [model("login-model", "anthropic-messages", "subscription")];
    registry.providers.push(provider("subscription", { auth: "oauth", oauth_runtime_type: "claude-code" }));
    registry.runtimes.reverse();
    await show(null);
    await input("Model *", "model");
    expect(select("Runtime").value).toBe("rt-claude-code");
    const standIn = select("Runtime").querySelector<HTMLOptionElement>('option[value="rt-stand-in"]')!;
    expect(standIn.disabled).toBe(true);
    expect(standIn.textContent).toContain("subscription is the login of another runtime");
    expect([...select("Runtime").options].filter((o) => !o.disabled).map((o) => o.value)).toEqual(["rt-claude-code"]);
    expect(container.textContent).toContain("Runtime changed to Claude Code CLI");
    expect(select("Provider *").parentElement!.textContent).toContain("login (OAuth)");
  });

  it("lists names once in case-insensitive order and requires a choice for multiple labelled protocols", async () => {
    registry.models.push({ ...model("z", "openai-chat"), name: "Zulu" }, { ...model("a", "openai-chat"), name: "alpha" });
    await show(null);
    expect(modelOptions()).toEqual(["alpha", "model", "Zulu"]);
    await input("Model *", "model");
    expect(select("Protocol *").value).toBe("");
    expect([...select("Protocol *").options].slice(1).map((o) => o.textContent)).toEqual([
      "Anthropic Messages (anthropic-messages)", "OpenAI Responses (openai-responses)", "OpenAI Chat Completions (openai-chat)",
    ]);
    await choose("Protocol *", "openai-chat");
    expect(select("Provider *").value).toBe("provider");
    expect(container.textContent).toContain("Runtimes that speak it: Stand-in");
    expect(select("Runtime").value).toBe("rt-stand-in");
    await submit();
    expect(requests[0].body.model_id).toBe("model-chat");
  });

  it("automatically chooses the only protocol and offering and retains valid protocol choices", async () => {
    registry.models = [{ ...model("unique", "openai-chat"), name: "single" }, ...registry.models];
    await show(null);
    await input("Model *", "single");
    expect(select("Protocol *").value).toBe("openai-chat");
    expect(select("Provider *").value).toBe("provider");
    await input("Model *", "model");
    expect(select("Protocol *").value).toBe("openai-chat");
    await submit();
    expect(requests[0].body.model_id).toBe("model-chat");
  });

  it("explains an unknown name without creating a model or sending a save request", async () => {
    await show(null);
    await input("Model *", "unknown");
    expect(container.textContent).toContain("No enabled provider offers unknown. Add it under LLM Providers: Models, or Fetch models.");
    expect(container.querySelector('a[href="#settings/providers"]')).not.toBeNull();
    expect(select("Protocol *").disabled).toBe(true);
    expect(select("Provider *").value).toBe("");
    await submit();
    expect(requests).toEqual([]);
    expect(container.querySelector('[role="alert"]')!.textContent).toBe("Choose a model, its protocol and a provider before saving.");
  });

  it("reports when no runtime can run a selected model and blocks saving", async () => {
    registry.models = [model("foreign", "openai-responses", "foreign-login")];
    await show(null);
    await input("Model *", "model");
    expect(container.textContent).toContain("No runtime here can run this model.");
    await submit();
    expect(requests).toEqual([]);
    expect(container.textContent).toContain("Choose a runtime that can run this model.");
  });

  it("keeps a compatible selection and the saved teams preference across runtime changes", async () => {
    await show({ ...PROFILE, agent_teams: false });
    await choose("Runtime", "rt-stand-in");
    expect(select("Model *").value).toBe("model");
    await choose("Runtime", "rt-claude-code");
    expect(container.querySelector<HTMLInputElement>('input[type="checkbox"]')!.checked).toBe(false);
  });

  it("blocks saving a model removed by a registry refresh", async () => {
    await show();
    await show(PROFILE, { ...REGISTRY, models: [] });
    expect(select("Provider *").value).toBe("");
    await submit();
    expect(requests).toEqual([]);
    expect(container.querySelector('[role="alert"]')!.textContent).toMatch(/model/i);
  });

  it("creates inline, refreshes all selectors and leaves the role selection unchanged", async () => {
    const saved: AgentProfile = { ...PROFILE, profile_id: "new", name: "New profile" };
    let registry = REGISTRY;
    vi.stubGlobal("fetch", vi.fn(async (request: RequestInfo | URL, init?: RequestInit) => {
      if (String(request) === "/api/v1/registry/view") return Response.json({ status: "ok", result: registry });
      if (String(request) === "/api/v1/system/status") return Response.json({ status: "ok", result: { checks: [] } });
      if (init?.method === "POST") {
        requests.push({ url: String(request), body: (JSON.parse(String(init.body)).fields ?? JSON.parse(String(init.body))) });
        registry = { ...REGISTRY, profiles: [...REGISTRY.profiles, saved] };
        return new Response(JSON.stringify({ status: "ok", result: { profile: saved, warnings: [] } }));
      }
      return new Response(JSON.stringify(String(request).endsWith("/status") ? { checks: [] } : registry));
    }));
    const change = vi.fn();
    const parentSubmit = vi.fn((event: React.FormEvent) => event.preventDefault());
    await act(async () => root.render(<form onSubmit={parentSubmit}>
      <AgentProfileSelect id="role-profile" value="p" onChange={change} emptyLabel="Inherited" roles={["worker"]} />
      <AgentProfileSelect id="task-profile" value="" onChange={change} emptyLabel="Default" roles={["worker"]} />
    </form>));
    const role = container.querySelector<HTMLSelectElement>("#role-profile")!;
    expect(role.selectedOptions[0].textContent).toContain("Provider / model / anthropic-messages");
    await act(async () => {
      role.value = "__new_profile__";
      role.dispatchEvent(new Event("change", { bubbles: true }));
    });
    expect(container.querySelectorAll("form")).toHaveLength(1);
    await input("Name *", "New profile");
    await input("Model *", "model");
    await choose("Protocol *", "anthropic-messages");
    await choose("Provider *", "provider");
    const add = [...container.querySelectorAll("button")].find((item) => item.textContent === "Add Agent Profile")!;
    await act(async () => add.click());
    expect(parentSubmit).not.toHaveBeenCalled();
    expect(change).not.toHaveBeenCalled();
    expect(container.querySelector('[role="group"]')).toBeNull();
    expect(role.value).toBe("p");
    for (const id of ["role-profile", "task-profile"]) {
      expect([...container.querySelector<HTMLSelectElement>(`#${id}`)!.options].some((item) => item.value === "new")).toBe(true);
    }
    expect(requests[0].body.model_id).toBe("model-messages");
  });

  it("refreshes model options after a registry mutation event", async () => {
    let registry = REGISTRY;
    vi.stubGlobal("fetch", vi.fn(async () => Response.json({ status: "ok", result: registry })));
    await act(async () => root.render(<AgentProfilesSection />));
    await act(async () => [...container.querySelectorAll("button")].find((item) => item.textContent?.includes("Add Agent Profile"))!.click());
    registry = { ...REGISTRY, models: [...REGISTRY.models, { ...model("fresh", "anthropic-messages"), name: "fresh" }] };
    await act(async () => window.dispatchEvent(new Event(REGISTRY_CHANGED_EVENT)));
    expect(modelOptions()).toContain("fresh");
    expect(select("Model *").value).toBe("");
  });

  it("keeps the draft and displays the server error when saving fails", async () => {
    const done = vi.fn();
    vi.stubGlobal("fetch", vi.fn(async (url: RequestInfo | URL, init?: RequestInit) => init?.body
      ? Response.json({ status: "error", error: { code: "conflict", message: "Model is no longer available" } }, { status: 409 })
      : Response.json(String(url).endsWith("/status") ? { checks: [] } : REGISTRY)));
    await act(async () => root.render(<AgentProfileForm profile={PROFILE} registry={REGISTRY} onDone={done} />));
    await submit();
    expect(container.querySelector('[role="alert"]')!.textContent).toContain("Model is no longer available");
    expect(select("Model *").value).toBe("model");
    expect(done).not.toHaveBeenCalled();
  });

  it("keeps a saved warning visible until acknowledged", async () => {
    const done = vi.fn();
    vi.stubGlobal("fetch", vi.fn(async (_url: RequestInfo | URL, init?: RequestInit) => Response.json(init?.body
      ? { status: "ok", result: { profile: PROFILE, warnings: ["No price for model"] } } : { checks: [] })));
    await act(async () => root.render(<AgentProfileForm profile={PROFILE} registry={REGISTRY} onDone={done} />));
    await submit();
    expect(container.querySelector('[role="status"]')!.textContent).toContain("No price for model");
    expect(done).not.toHaveBeenCalled();
    await act(async () => [...container.querySelectorAll("button")].find((item) => item.textContent === "OK")!.click());
    expect(done).toHaveBeenCalledWith(PROFILE);
  });

  it("preserves links to the layers that select a profile", async () => {
    const profile: AgentProfile = { ...PROFILE, used_by: { ...PROFILE.used_by, layers: [
      { scope: "system", role: null, project_id: null, project_name: null },
      { scope: "role", role: "worker", project_id: null, project_name: null },
      { scope: "project", role: null, project_id: "project", project_name: "Project" },
      { scope: "role_project", role: "reviewer", project_id: "project", project_name: "Project" },
    ] } };
    vi.stubGlobal("fetch", vi.fn(async () => Response.json({ status: "ok", result: { ...REGISTRY, profiles: [profile] } })));
    await act(async () => root.render(<AgentProfilesSection />));
    expect([...container.querySelectorAll("a")].map((item) => item.getAttribute("href"))).toEqual([
      "#settings/roles/shared", "#settings/roles/worker", "#projects/project/profiles/project", "#projects/project/profiles/reviewer",
    ]);
    expect(container.textContent).not.toContain("[object Object]");
  });

  it("edits and saves custom prompt text", async () => {
    await show({ ...PROFILE, custom_prompt: "Use plain language." });
    const label = [...container.querySelectorAll("label")].find((item) => item.textContent === "Custom prompt");
    const field = document.getElementById(label!.htmlFor) as HTMLTextAreaElement;
    expect(field.value).toBe("Use plain language.");
    expect(container.textContent).toContain("Text placed first in this profile's system prompt, before the harness. Empty adds nothing.");
    await act(async () => {
      Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype, "value")!.set!.call(field, "Speak briefly.");
      field.dispatchEvent(new Event("input", { bubbles: true }));
    });
    await act(async () => container.querySelector("form")!.dispatchEvent(new Event("submit", { bubbles: true, cancelable: true })));
    expect(requests[0].body.custom_prompt).toBe("Speak briefly.");
  });

  it("defaults a new profile's custom prompt to empty", async () => {
    await act(async () => root.render(<AgentProfileForm profile={null} registry={REGISTRY} onDone={() => {}} />));
    expect(container.querySelector("textarea")!.value).toBe("");
  });

  it("loads a selected role's preview and renders blocks in order with ownership and notes", async () => {
    const fetcher = vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input);
      return new Response(JSON.stringify(url.includes("prompt_preview") ? { status: "ok", result: {
        profile: { id: "p", name: "Profile" }, role: "reviewer", runtime: "claude-code", system_prompt_mode: "append",
        custom_prompt_set: true, looper_prompt_sha256: "hash", prompt_layers: { custom_prompt: { sha256: "hash" } },
        blocks: [
          { name: "Default prompt", owner: "runtime", text: null, note: "Claude Code's default prompt (not shown)" },
          { name: "Looper prompt", owner: "looper", text: "Speak briefly.\n\nHarness", note: "Appended to the main block" },
        ], notes: ["the Task section is filled in at launch"],
      } } : { status: "ok", result: { ...REGISTRY, profiles: [{ ...PROFILE, roles: ["worker", "reviewer"] }] } }), { status: 200 });
    });
    vi.stubGlobal("fetch", fetcher);
    await act(async () => root.render(<AgentProfilesSection />));
    const details = container.querySelector("details")!;
    expect(details.open).toBe(false);
    expect(details.querySelector("summary")!.textContent).toBe("Preview system prompt");
    expect(fetcher.mock.calls.some(([url]) => String(url).includes("prompt_preview"))).toBe(false);
    details.open = true;
    await choose("Role", "reviewer");
    const load = [...container.querySelectorAll("button")].find((button) => button.textContent === "Load")!;
    await act(async () => load.click());
    expect(fetcher.mock.calls.at(-1)![0]).toBe("/api/v1/profiles/prompt_preview");
    expect(details.textContent!.indexOf("Default prompt")).toBeLessThan(details.textContent!.indexOf("Looper prompt"));
    expect(details.textContent).toContain("Runtime");
    expect(details.textContent).toContain("Looper");
    expect(details.textContent).toContain("Claude Code's default prompt (not shown)");
    expect(details.textContent).toContain("the Task section is filled in at launch");
    const boxes = details.querySelectorAll("textarea");
    expect(boxes).toHaveLength(1);
    expect(boxes[0].readOnly).toBe(true);
    expect(boxes[0].value).toBe("Speak briefly.\n\nHarness");
    await choose("Role", "worker");
    expect(details.querySelector("textarea")).toBeNull();
  });

  it("shows preview errors without claiming the prompt loaded", async () => {
    vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL) => String(input).includes("prompt_preview")
      ? new Response(JSON.stringify({ status: "error", error: { code: "invalid", message: "Profile cannot run this role" } }), { status: 400 })
      : Response.json({ status: "ok", result: REGISTRY })));
    await act(async () => root.render(<AgentProfilesSection />));
    const load = [...container.querySelectorAll("button")].find((button) => button.textContent === "Load")!;
    await act(async () => load.click());
    expect(container.querySelector('[role="alert"]')!.textContent).toContain("Profile cannot run this role");
    expect(container.querySelector("details textarea")).toBeNull();
  });

  it("preserves special characters in profile and project preview parameters", async () => {
    await fetchPromptPreview("p/1", "planner", "project /?");
    expect(fetch).toHaveBeenCalledWith("/api/v1/profiles/prompt_preview", expect.objectContaining({
      method: "POST", body: JSON.stringify({ profile_id: "p/1", role: "planner", project_id: "project /?" }),
    }));
  });
});

const PI_ROLES = ["manager", "planner", "worker", "reviewer"] as const;

const PI_RUNTIME: Runtime = {
  runtime_id: "rt-pi", name: "pi", type: "pi", type_label: "pi", executable: "", default_executable: "pi",
  path: "/usr/local/bin/pi", in_process: false, protocols: ["openai-chat", "openai-responses"], version: "1.3.0",
  capabilities: ["run", "resident_session", "resume"], roles: [...PI_ROLES],
  probed_at: null, probe_error: null, builtin: true, profile_count: 1,
  auth_kinds: ["api_key"],
};

const PI_TYPE: RuntimeType = {
  translated_protocols: [], type: "pi", in_process: false, label: "pi", default_executable: "pi", runtime_id: "rt-pi",
  supported_models: null,
  loads_operator_config: false,
  protocols: ["openai-chat", "openai-responses"], capabilities: ["run", "resident_session", "resume"],
  roles: [...PI_ROLES], auth_kinds: ["api_key"], can_replace_system_prompt: true, replace_system_prompt_refusal: null, can_interrupt: false,
};

const PI_PROFILE: AgentProfile = {
  ...PROFILE, profile_id: "pi-profile", name: "pi/default", model_id: "pi-model",
  model: { model_id: "pi-model", name: "selected-model", protocol: "openai-responses", provider_id: "responses" },
  agent_teams: false, runtime_id: "rt-pi", runtime_type: "pi", runtime_name: "pi",
  provider: { ...PROFILE.provider, provider_id: "responses", name: "Responses" },
  roles: [...PI_ROLES],
};

const PI_REGISTRY: Registry = {
  ...REGISTRY,
  runtime_types: [PI_TYPE],
  runtimes: [PI_RUNTIME],
  providers: [
    provider("responses", { name: "Responses" }),
    provider("chat", { name: "Chat" }),
    provider("anthropic", { name: "Anthropic" }),
    provider("claude-login", {
      name: "Claude login", auth: "oauth",
      oauth_runtime_type: "claude-code",
    }),
    provider("pi-login", {
      name: "Pi login", auth: "oauth",
      oauth_runtime_type: "pi",
    }),
  ],
  models: [
    { ...model("pi-model", "openai-responses", "responses"), name: "selected-model" },
    model("chat-model", "openai-chat", "chat"), model("anthropic-model", "anthropic-messages", "anthropic"),
    model("claude-oauth-model", "anthropic-messages", "claude-login"),
    model("pi-oauth-model", "openai-responses", "pi-login"),
  ],
  profiles: [PI_PROFILE],
};

describe("pi Agent Profiles", () => {
  let container: HTMLDivElement;
  let root: Root;
  let requests: { url: string; body: Record<string, unknown> }[];

  beforeEach(() => {
    requests = [];
    vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      if (url === "/api/v1/registry/view") return Response.json({ status: "ok", result: PI_REGISTRY });
      if (url === "/api/v1/system/status") return Response.json({ status: "ok", result: { checks: [] } });
      if (init?.body) requests.push({ url, body: (JSON.parse(String(init.body)).fields ?? JSON.parse(String(init.body))) });
      const answer = { profile: PI_PROFILE, warnings: [] };
      return new Response(JSON.stringify({ status: "ok", result: answer }), { status: 200 });
    }));
    container = document.createElement("div");
    document.body.append(container);
    root = createRoot(container);
  });

  afterEach(() => {
    act(() => root.unmount());
    container.remove();
    vi.unstubAllGlobals();
  });

  const select = (label: string) => {
    const field = [...container.querySelectorAll("label")].find((item) => item.textContent === label);
    expect(field, label).toBeTruthy();
    return document.getElementById(field!.htmlFor) as HTMLSelectElement;
  };

  it("offers only providers for the chosen name and protocol on a saved Pi profile", async () => {
    await act(async () => root.render(<AgentProfileForm profile={PI_PROFILE} registry={PI_REGISTRY} onDone={() => {}} />));
    expect([...select("Provider *").options].map((option) => option.value)).toEqual(["", "responses"]);
    expect(select("Provider *").value).toBe("responses");
    expect(select("Model *").value).toBe("selected-model");
    expect(select("Protocol *").value).toBe("openai-responses");
    expect([...select("Runtime").options].map((option) => option.textContent)).toContain("Pi CLI");
  });

  it("lists four roles and leaves the system prompt mode on the role", async () => {
    await act(async () => root.render(<AgentProfilesSection />));
    expect(container.textContent).toContain("Pi CLI · Responses · model selected-model");
    expect(container.textContent).toContain("Can run Manager, Planner, Worker, Reviewer");
    const role = select("Role");
    expect([...role.querySelectorAll("option")].map((option) => option.textContent)).toEqual([
      "Manager", "Planner", "Worker", "Reviewer",
    ]);
    const edit = container.querySelector('button[aria-label="Edit Agent Profile pi/default"]') as HTMLButtonElement;
    expect(edit).toBeTruthy();
    await act(async () => edit.click());
    expect([...container.querySelectorAll("label")].some((label) => label.textContent === "System prompt")).toBe(false);
    expect(container.querySelector('[data-testid="prompt-mode-per-role"]')!.textContent)
      .toContain("set per role in Settings → Roles");
    expect([...container.querySelectorAll("option")].map((option) => option.value)).not.toContain("append");
    expect([...container.querySelectorAll("option")].map((option) => option.value)).not.toContain("replace");
    await act(async () => container.querySelector("form")!.dispatchEvent(new Event("submit", { bubbles: true, cancelable: true })));
    expect(requests).toHaveLength(1);
    expect(requests[0].body).not.toHaveProperty("system_prompt_mode");
    expect(requests[0].body).not.toHaveProperty("api_key");
    expect(requests[0].body.model_id).toBe("pi-model");
    expect(requests[0].body).not.toHaveProperty("provider_id");
    expect(requests[0].body.runtime_id).toBe("rt-pi");
  });
});
