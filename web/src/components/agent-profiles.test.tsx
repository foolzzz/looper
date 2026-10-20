// @vitest-environment happy-dom
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { AgentProfileForm, AgentProfilesSection, AgentProfileSelect } from "@/components/agent-profiles";
import { fetchPromptPreview, REGISTRY_CHANGED_EVENT, type AgentProfileV3 as AgentProfile, type RegistryV3 as Registry, type ModelV3, type ProviderV3 } from "@/lib/api";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

const REFUSAL = "the Stand-in runtime cannot replace its default system prompt";
const PROFILE: AgentProfile = {
  profile_id: "p", name: "Profile", model_id: "model-messages",
  model: { model_id: "model-messages", name: "model", protocol: "anthropic-messages", provider_id: "provider" }, effort: null, agent_teams: true,
  custom_prompt: "", runtime_id: "rt-claude-code", runtime_type: "claude-code",
  runtime_name: "Claude Code", provider_id: "provider", provider_name: "Provider", builtin: false,
  provider: { provider_id: "provider", name: "Provider", vendor_type: "custom", auth: "api_key" },
  roles: ["worker"], used_by: { layers: [], task_count: 0, schedule_count: 0, allowed_in: [] }, warnings: [],
  cost: { source: "runtime", equivalent: false, unpriced: [] },
};
const provider = (id: string, extra: Partial<ProviderV3> = {}): ProviderV3 => ({
  provider_id: id, name: id === "provider" ? "Provider" : id, vendor_type: "custom", auth: "api_key",
  oauth_runtime_type: null, endpoints: ["anthropic-messages", "openai-chat", "openai-responses"].map((protocol) => ({
    protocol: protocol as ModelV3["protocol"], base_url: "https://example.com", use_runtime_default: false,
  })), suggested_models: [], login: null, env: [], max_concurrency: null, api_key: null, builtin: false,
  endpoint_count: 3, model_count: 0, profile_count: 1, created_at: "", updated_at: "", ...extra,
});
const model = (id: string, protocol: ModelV3["protocol"], providerId = "provider"): ModelV3 => ({
  model_id: id, name: "model", protocol, provider_id: providerId, provider_name: providerId === "provider" ? "Provider" : providerId,
  context_window: null, input_usd_per_mtok: null, output_usd_per_mtok: null, cache_read_usd_per_mtok: null,
  cache_write_usd_per_mtok: null, efforts: null, background_model: null, catalog_order: 0, builtin: false,
  profile_count: 0, endpoint: { protocol, base_url: "https://example.com", use_runtime_default: false },
  prices: {} as ModelV3["prices"], warnings: [], created_at: "", updated_at: "",
});
const REGISTRY: Registry = {
  runtime_types: ["claude-code", "codex", "stand-in"].map<Registry["runtime_types"][number]>((type) => ({
    type, label: type === "stand-in" ? "Stand-in" : "Claude Code", default_executable: type,
    runtime_id: `rt-${type}`, protocols: type === "codex" ? ["openai-responses"] : type === "stand-in" ? ["anthropic-messages", "openai-chat"] : ["anthropic-messages"], capabilities: [], roles: ["worker"],
    auth_kinds: ["api_key", "oauth"], can_replace_system_prompt: type !== "stand-in",
    replace_system_prompt_refusal: type === "stand-in" ? REFUSAL : null,
  })),
  runtimes: ["claude-code", "codex", "stand-in"].map<Registry["runtimes"][number]>((type) => ({
    runtime_id: `rt-${type}`, name: type, type, type_label: type, executable: "", default_executable: type,
    path: null, protocols: type === "codex" ? ["openai-responses"] : type === "stand-in" ? ["anthropic-messages", "openai-chat"] : ["anthropic-messages"], version: null, capabilities: [], roles: ["worker"],
    probed_at: null, probe_error: null, builtin: true, profile_count: 0,
  })),
  providers: [provider("provider"), provider("other"), provider("codex-login", { auth: "oauth", oauth_runtime_type: "codex" }),
    provider("foreign-login", { auth: "oauth", oauth_runtime_type: "grok" })],
  models: [model("model-messages", "anthropic-messages"), model("model-chat", "openai-chat"),
    model("model-responses", "openai-responses"), model("other-messages", "anthropic-messages", "other"),
    model("codex-model", "openai-responses", "codex-login"), model("foreign-model", "openai-responses", "foreign-login")],
  profiles: [PROFILE], vendor_types: [], prices: { as_of: "", sources: [], models: {} },
  limits: {
    model_fields: [], name_max_chars: 100, models_max: 10, price_fields: [], protocols: ["anthropic-messages", "openai-chat", "openai-responses"], auth_kinds: ["api_key"],
    effort_levels: ["high", "xhigh"], role_capabilities: { manager: [], planner: [], worker: [], reviewer: [] },
    secret_mask: "", provider_env_owned: [],
  },
};

describe("Agent Profiles", () => {
  let container: HTMLDivElement;
  let root: Root;
  let requests: { url: string; body: Record<string, unknown> }[];

  beforeEach(() => {
    requests = [];
    vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      if (init?.body) requests.push({ url, body: JSON.parse(String(init.body)) });
      const answer = url.endsWith("/registry") ? REGISTRY : url.endsWith("/status") ? { checks: [] }
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

  const show = async (profile: AgentProfile | null = PROFILE, registry = REGISTRY) => {
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

  const CODEX: AgentProfile = { ...PROFILE, model_id: "model-responses", model: { ...PROFILE.model, model_id: "model-responses", protocol: "openai-responses" }, runtime_id: "rt-codex", runtime_type: "codex", agent_teams: false };

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
  const options = () => [...select("Model *").options].filter((item) => item.value).map((item) => item.value);
  const input = async (label: string, value: string) => {
    const field = select(label) as unknown as HTMLInputElement;
    await act(async () => {
      Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value")!.set!.call(field, value);
      field.dispatchEvent(new Event("input", { bubbles: true }));
    });
  };

  it("requires an explicit model for a new profile and saves only its identity", async () => {
    await show(null);
    expect(select("Model *").tagName).toBe("SELECT");
    expect(select("Model *").value).toBe("");
    await input("Name *", "New profile");
    await submit();
    expect(requests).toEqual([]);
    expect(container.querySelector('[role="alert"]')!.textContent).toMatch(/choose.*model/i);
    await choose("Model *", "model-messages");
    await choose("Effort", "high");
    await submit();
    expect(requests[0].body).toEqual({ name: "New profile", runtime_id: "rt-claude-code", model_id: "model-messages",
      effort: "high", agent_teams: true, custom_prompt: "" });
  });

  it("filters models by exact runtime protocol, auth kind and OAuth ownership", async () => {
    await show();
    expect(options()).toEqual(["model-messages", "other-messages"]);
    await choose("Runtime", "rt-codex");
    expect(options()).toEqual(["model-responses", "codex-model"]);
    expect(select("Model *").value).toBe("");
    await choose("Model *", "codex-model");
    await submit();
    expect(requests[0].body.model_id).toBe("codex-model");
    expect(requests[0].body.agent_teams).toBe(false);
  });

  it("does not offer OAuth models when the runtime only supports API keys", async () => {
    await show(CODEX, { ...REGISTRY, runtime_types: REGISTRY.runtime_types.map((item) => ({ ...item, auth_kinds: ["api_key"] })) });
    expect(options()).toEqual(["model-responses"]);
  });

  it("keeps same-name models with distinct protocols selectable by id", async () => {
    await show(null);
    await choose("Runtime", "rt-stand-in");
    const labels = [...select("Model *").options].map((item) => item.textContent);
    expect(labels).toContain("Provider / model / anthropic-messages");
    expect(labels).toContain("Provider / model / openai-chat");
    await input("Name *", "Chat profile");
    await choose("Model *", "model-chat");
    await submit();
    expect(requests[0].body.model_id).toBe("model-chat");
    expect(requests[0].body).not.toHaveProperty("provider_id");
    expect(requests[0].body).not.toHaveProperty("model");
  });

  it("uses provider only as a model filter, without choosing a model", async () => {
    await show(null);
    await choose("Provider", "other");
    expect(options()).toEqual(["other-messages"]);
    expect(select("Model *").value).toBe("");
    await choose("Model *", "other-messages");
    await input("Name *", "Other profile");
    await submit();
    expect(requests[0].body.model_id).toBe("other-messages");
    expect(requests[0].body).not.toHaveProperty("provider_id");
  });

  it("clears incompatible choices without selecting the first model when changing runtime", async () => {
    await show();
    await choose("Runtime", "rt-codex");
    await submit();
    expect(requests).toEqual([]);
    await choose("Runtime", "rt-claude-code");
    expect(select("Model *").value).toBe("");
  });

  it("keeps a compatible selection and the saved teams preference across runtime changes", async () => {
    await show({ ...PROFILE, agent_teams: false });
    await choose("Runtime", "rt-stand-in");
    expect(select("Model *").value).toBe("model-messages");
    await choose("Runtime", "rt-claude-code");
    expect(container.querySelector<HTMLInputElement>('input[type="checkbox"]')!.checked).toBe(false);
  });

  it("blocks saving a model removed by a registry refresh", async () => {
    await show();
    await show(PROFILE, { ...REGISTRY, models: [] });
    expect(select("Model *").value).toBe("");
    await submit();
    expect(requests).toEqual([]);
    expect(container.querySelector('[role="alert"]')!.textContent).toMatch(/model/i);
  });

  it("creates inline, refreshes all selectors and leaves the role selection unchanged", async () => {
    const saved: AgentProfile = { ...PROFILE, profile_id: "new", name: "New profile" };
    let registry = REGISTRY;
    vi.stubGlobal("fetch", vi.fn(async (request: RequestInfo | URL, init?: RequestInit) => {
      if (init?.method === "POST") {
        requests.push({ url: String(request), body: JSON.parse(String(init.body)) });
        registry = { ...REGISTRY, profiles: [...REGISTRY.profiles, saved] };
        return new Response(JSON.stringify({ profile: saved, warnings: [] }));
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
    await choose("Model *", "model-messages");
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
    vi.stubGlobal("fetch", vi.fn(async () => new Response(JSON.stringify(registry))));
    await act(async () => root.render(<AgentProfilesSection />));
    await act(async () => [...container.querySelectorAll("button")].find((item) => item.textContent?.includes("Add Agent Profile"))!.click());
    registry = { ...REGISTRY, models: [...REGISTRY.models, { ...model("fresh", "anthropic-messages"), name: "fresh" }] };
    await act(async () => window.dispatchEvent(new Event(REGISTRY_CHANGED_EVENT)));
    expect(options()).toContain("fresh");
    expect(select("Model *").value).toBe("");
  });

  it("keeps the draft and displays the server error when saving fails", async () => {
    const done = vi.fn();
    vi.stubGlobal("fetch", vi.fn(async (url: RequestInfo | URL, init?: RequestInit) => init?.body
      ? Response.json({ error: "Model is no longer available" }, { status: 409 })
      : Response.json(String(url).endsWith("/status") ? { checks: [] } : REGISTRY)));
    await act(async () => root.render(<AgentProfileForm profile={PROFILE} registry={REGISTRY} onDone={done} />));
    await submit();
    expect(container.querySelector('[role="alert"]')!.textContent).toContain("Model is no longer available");
    expect(select("Model *").value).toBe("model-messages");
    expect(done).not.toHaveBeenCalled();
  });

  it("keeps a saved warning visible until acknowledged", async () => {
    const done = vi.fn();
    vi.stubGlobal("fetch", vi.fn(async (_url: RequestInfo | URL, init?: RequestInit) => Response.json(init?.body
      ? { profile: PROFILE, warnings: ["No price for model"] } : { checks: [] })));
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
    vi.stubGlobal("fetch", vi.fn(async () => Response.json({ ...REGISTRY, profiles: [profile] })));
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
