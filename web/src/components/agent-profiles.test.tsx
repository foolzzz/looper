// @vitest-environment happy-dom
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { AgentProfileForm, AgentProfilesSection } from "@/components/agent-profiles";
import { RuntimesSection } from "@/components/runtime-cards";
import { fetchPromptPreview, type AgentProfile, type Provider, type ProviderModel, type Registry, type Runtime, type RuntimeType } from "@/lib/api";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

const REFUSAL = "the Stand-in runtime cannot replace its default system prompt";
const catalogModel = (name: string): ProviderModel => ({
  name, context_window: null, input_usd_per_mtok: null, output_usd_per_mtok: null,
  cache_read_usd_per_mtok: null, cache_write_usd_per_mtok: null, efforts: null, background_model: null,
});
const PROFILE: AgentProfile = {
  profile_id: "p", name: "Profile", model: "model", effort: null, agent_teams: true,
  custom_prompt: "", runtime_id: "rt-claude-code", runtime_type: "claude-code",
  runtime_name: "Claude Code", provider_id: "provider", provider_name: "Provider", builtin: false,
  roles: ["worker"], used_by: { layers: [], task_count: 0, allowed_in: [] },
  cost: { source: "runtime", equivalent: false, unpriced: [] },
};
const REGISTRY: Registry = {
  runtime_types: ["claude-code", "codex", "stand-in"].map<Registry["runtime_types"][number]>((type) => ({
    type, label: type === "stand-in" ? "Stand-in" : "Claude Code", default_executable: type,
    runtime_id: `rt-${type}`, protocols: ["anthropic"], capabilities: [], roles: ["worker"],
    auth_kinds: ["api_key"], can_replace_system_prompt: type !== "stand-in",
    supported_models: null,
    replace_system_prompt_refusal: type === "stand-in" ? REFUSAL : null,
  })),
  runtimes: ["claude-code", "codex", "stand-in"].map<Registry["runtimes"][number]>((type) => ({
    runtime_id: `rt-${type}`, name: type, type, type_label: type, executable: "", default_executable: type,
    path: null, protocols: ["anthropic"], version: null, capabilities: [], roles: ["worker"],
    probed_at: null, probe_error: null, builtin: true, profile_count: 0,
  })),
  providers: [{
    provider_id: "provider", name: "Provider", protocols: ["anthropic"], auth: "api_key", oauth_runtime_type: null,
    base_url: "", models: [], suggested_models: [], login: null, env: [], max_concurrency: null,
    api_key: null, builtin: false, profile_count: 1,
  }],
  profiles: [PROFILE], provider_presets: [], prices: { as_of: "", sources: [], models: {} },
  limits: {
    name_max_chars: 100, models_max: 10, price_fields: [], protocols: ["anthropic"], auth_kinds: ["api_key"],
    effort_levels: [], role_capabilities: { manager: [], planner: [], worker: [], reviewer: [] },
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
      if (init?.body) requests.push({ url, body: JSON.parse(String(init.body)) });
      const answer = url.endsWith("/registry") ? registry : url.endsWith("/status") ? { checks: [] }
        : { profile: PROFILE, warnings: [] };
      return new Response(JSON.stringify(answer), { status: 200 });
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

  const show = async (profile: AgentProfile = PROFILE) => {
    await act(async () => root.render(<AgentProfileForm profile={profile} registry={registry} onDone={() => {}} />));
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

  const CODEX: AgentProfile = { ...PROFILE, runtime_id: "rt-codex", runtime_type: "codex", agent_teams: false };

  const supported = ["deepseek-flash", "deepseek-flash[1m]", "deepseek-v4-pro", "deepseek-v4-pro[1m]"];
  const dsh = () => {
    registry.runtime_types.push({
      ...registry.runtime_types[0], type: "dsh", label: "DSH", runtime_id: "rt-dsh",
      default_executable: "dsh", protocols: ["anthropic-messages"], supported_models: supported,
      can_replace_system_prompt: false,
      replace_system_prompt_refusal: "the DSH runtime cannot replace its default system prompt",
    });
    registry.runtimes.push({ ...registry.runtimes[0], type: "dsh", runtime_id: "rt-dsh", type_label: "DSH" });
    registry.providers[0] = { ...registry.providers[0], protocols: ["anthropic-messages"] };
  };
  const modelOptions = () => [...select("Model *").options].map((o) => o.value);

  it("shows the DSH runtime card and pinned install command using the existing cards", async () => {
    dsh();
    await act(async () => root.render(<RuntimesSection />));
    expect([...container.querySelectorAll("h3")].map((h) => h.textContent)).toContain("DSH CLI");
    expect(container.textContent).toContain("npm install -g @deepseek-ai/dsh@0.2.1-rc.2");
    expect(container.textContent).toContain("Claude Code CLI");
    expect(container.textContent).toContain("Codex CLI");
  });

  it("pairs Messages API key providers by the server's exact supported models and saves [1m]", async () => {
    dsh();
    registry.providers[0].models = ["unrelated", ...supported].map(catalogModel);
    await show();
    await choose("Runtime", "rt-dsh");
    expect(select("Provider").value).toBe("provider");
    expect(modelOptions()).toEqual(supported);
    expect(select("Model *").value).toBe(supported[0]);
    await choose("Model *", "deepseek-v4-pro[1m]");
    await act(async () => container.querySelector("form")!.dispatchEvent(new Event("submit", { bubbles: true, cancelable: true })));
    expect(requests[0].body).toMatchObject({ runtime_id: "rt-dsh", provider_id: "provider", model: "deepseek-v4-pro[1m]", agent_teams: false });
    expect(container.textContent).toContain("the DSH runtime cannot replace its default system prompt");
  });

  it.each(["chat", "oauth", "no intersection"])("offers no valid DSH pairing for %s providers", async (kind) => {
    dsh();
    const provider = registry.providers[0];
    provider.models = [catalogModel(kind === "no intersection" ? "deepseek-flash-extra" : supported[0])];
    if (kind === "chat") provider.protocols = ["openai-chat"];
    if (kind === "oauth") Object.assign(provider, { auth: "oauth", oauth_runtime_type: "dsh" });
    await show();
    await choose("Runtime", "rt-dsh");
    expect([...select("Provider").options].map((o) => o.value)).not.toContain("provider");
    expect(select("Provider").value).toBe("");
  });

  it("keeps empty catalogs editable with exact server suggestions and lets the server reject invalid input", async () => {
    dsh();
    registry.providers[0].suggested_models = ["unrelated"];
    await show();
    await choose("Runtime", "rt-dsh");
    const field = select("Model *") as unknown as HTMLInputElement;
    expect(field.tagName).toBe("INPUT");
    expect([...container.querySelectorAll("datalist option")].map((o) => o.getAttribute("value"))).toEqual(supported);
    await act(async () => {
      Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value")!.set!.call(field, "deepseek-flash[1m]");
      field.dispatchEvent(new Event("input", { bubbles: true }));
    });
    await act(async () => container.querySelector("form")!.dispatchEvent(new Event("submit", { bubbles: true, cancelable: true })));
    expect(requests[0].body.model).toBe("deepseek-flash[1m]");
    await act(async () => {
      Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value")!.set!.call(field, "unsupported");
      field.dispatchEvent(new Event("input", { bubbles: true }));
    });
    vi.stubGlobal("fetch", vi.fn(async () => new Response(JSON.stringify({ error: "DSH supports only exact models" }), { status: 400 })));
    await act(async () => container.querySelector("form")!.dispatchEvent(new Event("submit", { bubbles: true, cancelable: true })));
    expect(container.textContent).toContain("DSH supports only exact models");
  });

  it("uses supported_models for any runtime and preserves unrestricted runtime catalogs including pi", async () => {
    dsh();
    registry.runtime_types.at(-1)!.supported_models = ["server-only-model"];
    registry.providers[0].models = ["server-only-model", supported[0]].map(catalogModel);
    await show();
    await choose("Runtime", "rt-dsh");
    expect(modelOptions()).toEqual(["server-only-model"]);
    registry.runtime_types.push({ ...registry.runtime_types[0], type: "pi", label: "pi", runtime_id: "rt-pi", supported_models: null });
    registry.runtimes.push({ ...registry.runtimes[0], type: "pi", runtime_id: "rt-pi" });
    registry.providers[0].protocols.push("anthropic");
    await show();
    await choose("Runtime", "rt-pi");
    expect(modelOptions()).toEqual(["server-only-model", supported[0]]);
  });

  it("refits the model when switching providers and retains an eligible selection", async () => {
    dsh();
    registry.providers[0].models = ["unrelated", supported[1]].map(catalogModel);
    registry.providers.push({ ...registry.providers[0], provider_id: "second", models: [supported[2]].map(catalogModel) });
    registry.providers.push({ ...registry.providers[0], provider_id: "third", models: [supported[1], supported[2]].map(catalogModel) });
    await show();
    await choose("Runtime", "rt-dsh");
    expect(select("Model *").value).toBe(supported[1]);
    await choose("Provider", "second");
    expect(modelOptions()).toEqual([supported[2]]);
    expect(select("Model *").value).toBe(supported[2]);
    await choose("Provider", "third");
    expect(select("Model *").value).toBe(supported[2]);
    await choose("Runtime", "rt-claude-code");
    await choose("Runtime", "rt-dsh");
    expect(select("Model *").value).toBe(supported[1]);
  });

  it("opens a saved DSH profile with its exact [1m] model selected", async () => {
    dsh();
    registry.providers[0].models = supported.map(catalogModel);
    await show({ ...PROFILE, runtime_id: "rt-dsh", runtime_type: "dsh", model: supported[3], agent_teams: false });
    expect(select("Runtime").value).toBe("rt-dsh");
    expect(select("Provider").value).toBe("provider");
    expect(select("Model *").value).toBe(supported[3]);
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
      return new Response(JSON.stringify(url.includes("prompt-preview") ? {
        profile: { id: "p", name: "Profile" }, role: "reviewer", runtime: "claude-code", system_prompt_mode: "append",
        custom_prompt_set: true, looper_prompt_sha256: "hash", prompt_layers: { custom_prompt: { sha256: "hash" } },
        blocks: [
          { name: "Default prompt", owner: "runtime", text: null, note: "Claude Code's default prompt (not shown)" },
          { name: "Looper prompt", owner: "looper", text: "Speak briefly.\n\nHarness", note: "Appended to the main block" },
        ], notes: ["the Task section is filled in at launch"],
      } : { ...REGISTRY, profiles: [{ ...PROFILE, roles: ["worker", "reviewer"] }] }), { status: 200 });
    });
    vi.stubGlobal("fetch", fetcher);
    await act(async () => root.render(<AgentProfilesSection />));
    const details = container.querySelector("details")!;
    expect(details.open).toBe(false);
    expect(details.querySelector("summary")!.textContent).toBe("Preview system prompt");
    expect(fetcher.mock.calls.some(([url]) => String(url).includes("prompt-preview"))).toBe(false);
    details.open = true;
    await choose("Role", "reviewer");
    const load = [...container.querySelectorAll("button")].find((button) => button.textContent === "Load")!;
    await act(async () => load.click());
    expect(fetcher.mock.calls.at(-1)![0]).toBe("/api/agent-profiles/p/prompt-preview?role=reviewer");
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
    vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL) => String(input).includes("prompt-preview")
      ? new Response(JSON.stringify({ error: "Profile cannot run this role" }), { status: 400 })
      : new Response(JSON.stringify(REGISTRY), { status: 200 })));
    await act(async () => root.render(<AgentProfilesSection />));
    const load = [...container.querySelectorAll("button")].find((button) => button.textContent === "Load")!;
    await act(async () => load.click());
    expect(container.querySelector('[role="alert"]')!.textContent).toContain("Profile cannot run this role");
    expect(container.querySelector("details textarea")).toBeNull();
  });

  it("includes and encodes the optional project in preview requests", async () => {
    await fetchPromptPreview("p/1", "planner", "project /?");
    expect(fetch).toHaveBeenCalledWith("/api/agent-profiles/p%2F1/prompt-preview?role=planner&project_id=project+%2F%3F");
  });
});

const PI_ROLES = ["manager", "planner", "worker", "reviewer"] as const;

function piProvider(fields: Pick<Provider, "provider_id" | "name" | "protocols" | "auth"> & Partial<Provider>): Provider {
  return {
    oauth_runtime_type: null, base_url: "https://example.invalid/v1", models: [], suggested_models: [], login: null,
    env: [], max_concurrency: null, api_key: null, builtin: false, profile_count: 0, ...fields,
  };
}

const PI_RUNTIME: Runtime = {
  runtime_id: "rt-pi", name: "pi", type: "pi", type_label: "pi", executable: "", default_executable: "pi",
  path: "/usr/local/bin/pi", protocols: ["openai-chat", "openai-responses"], version: "1.3.0",
  capabilities: ["run", "resident_session", "resume"], roles: [...PI_ROLES],
  probed_at: null, probe_error: null, builtin: true, profile_count: 1,
};

const PI_TYPE: RuntimeType = {
  type: "pi", label: "pi", default_executable: "pi", runtime_id: "rt-pi",
  supported_models: null,
  protocols: ["openai-chat", "openai-responses"], capabilities: ["run", "resident_session", "resume"],
  roles: [...PI_ROLES], auth_kinds: ["api_key"], can_replace_system_prompt: true, replace_system_prompt_refusal: null,
};

const PI_PROFILE: AgentProfile = {
  ...PROFILE, profile_id: "pi-profile", name: "pi/default", model: "selected-model", agent_teams: false,
  runtime_id: "rt-pi", runtime_type: "pi", runtime_name: "pi", provider_id: "responses", provider_name: "Responses",
  roles: [...PI_ROLES],
};

const PI_REGISTRY: Registry = {
  ...REGISTRY,
  runtime_types: [PI_TYPE],
  runtimes: [PI_RUNTIME],
  providers: [
    piProvider({ provider_id: "responses", name: "Responses", protocols: ["openai-responses"], auth: "api_key" }),
    piProvider({ provider_id: "chat", name: "Chat", protocols: ["openai-chat"], auth: "api_key" }),
    piProvider({ provider_id: "anthropic", name: "Anthropic", protocols: ["anthropic-messages"], auth: "api_key" }),
    piProvider({
      provider_id: "claude-login", name: "Claude login", protocols: ["anthropic-messages"], auth: "oauth",
      oauth_runtime_type: "claude-code",
    }),
    piProvider({
      provider_id: "pi-login", name: "Pi login", protocols: ["openai-responses"], auth: "oauth",
      oauth_runtime_type: "pi",
    }),
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
      if (init?.body) requests.push({ url, body: JSON.parse(String(init.body)) });
      const answer = url.endsWith("/registry") ? PI_REGISTRY : { profile: PI_PROFILE, warnings: [] };
      return new Response(JSON.stringify(answer), { status: 200 });
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
  const choose = async (label: string, value: string) => {
    await act(async () => {
      const field = select(label);
      field.value = value;
      field.dispatchEvent(new Event("change", { bubbles: true }));
    });
  };

  it("offers both compatible API-key providers and not OAuth or Anthropic", async () => {
    await act(async () => root.render(<AgentProfileForm profile={PI_PROFILE} registry={PI_REGISTRY} onDone={() => {}} />));
    const provider = select("Provider");
    expect([...provider.querySelectorAll("optgroup")].map((group) => group.label)).toEqual(["API key"]);
    expect([...provider.querySelectorAll("option")].map((option) => option.value)).toEqual(["responses", "chat", "__new_provider__"]);
    expect([...provider.options].every((option) => !option.disabled || option.value === "")).toBe(true);
    await choose("Provider", "chat");
    expect(select("Provider").value).toBe("chat");
    await choose("Provider", "responses");
    expect(select("Provider").value).toBe("responses");
    expect(container.textContent).not.toMatch(/login \(OAuth\)|Pi login|Claude login|Anthropic/);
    expect([...select("Runtime").querySelectorAll("option")].map((option) => option.textContent)).toContain("Pi CLI");
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
    expect(requests[0].body.provider_id).toBe("responses");
    expect(requests[0].body.runtime_id).toBe("rt-pi");
  });
});
