// @vitest-environment happy-dom
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { AgentProfileForm, AgentProfilesSection } from "@/components/agent-profiles";
import type { AgentProfile, Registry } from "@/lib/api";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

const REFUSAL = "the Stand-in runtime cannot replace its default system prompt";
const PROFILE: AgentProfile = {
  profile_id: "p", name: "Profile", model: "model", effort: null, agent_teams: true,
  system_prompt_mode: "append", runtime_id: "rt-claude-code", runtime_type: "claude-code",
  runtime_name: "Claude Code", provider_id: "provider", provider_name: "Provider", builtin: false,
  roles: ["worker"], used_by: { layers: [], task_count: 0, allowed_in: [] },
  cost: { source: "runtime", equivalent: false, unpriced: [] },
};
const REGISTRY: Registry = {
  runtime_types: ["claude-code", "stand-in"].map<Registry["runtime_types"][number]>((type) => ({
    type, label: type === "stand-in" ? "Stand-in" : "Claude Code", default_executable: type,
    runtime_id: `rt-${type}`, protocols: ["anthropic"], capabilities: [], roles: ["worker"],
    auth_kinds: ["api_key"], can_replace_system_prompt: type === "claude-code",
    replace_system_prompt_refusal: type === "stand-in" ? REFUSAL : null,
  })),
  runtimes: ["claude-code", "stand-in"].map<Registry["runtimes"][number]>((type) => ({
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

describe("Agent Profile system prompt mode", () => {
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

  const show = async (profile: AgentProfile = PROFILE) => {
    await act(async () => root.render(<AgentProfileForm profile={profile} registry={REGISTRY} onDone={() => {}} />));
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

  it("renders the append and replace choices with append selected by default", async () => {
    await show();
    const field = select("System prompt");
    expect(field.value).toBe("append");
    expect(field.disabled).toBe(false);
    expect([...field.options].map((option) => option.text)).toEqual([
      "Append to the runtime's default", "Replace the runtime's default",
    ]);
  });

  it("disables system prompt selection with the runtime's refusal reason", async () => {
    await show({ ...PROFILE, runtime_id: "rt-stand-in", runtime_type: "stand-in", agent_teams: false });
    const field = select("System prompt");
    expect(field.disabled).toBe(true);
    expect(document.getElementById(field.getAttribute("aria-describedby")!)?.textContent).toContain(REFUSAL);
  });

  it("sends the selected system_prompt_mode in the profile save body", async () => {
    await show();
    await choose("System prompt", "replace");
    await act(async () => container.querySelector("form")!.dispatchEvent(new Event("submit", { bubbles: true, cancelable: true })));
    expect(requests).toEqual([expect.objectContaining({ url: "/api/agent-profiles/p", body: expect.objectContaining({ system_prompt_mode: "replace" }) })]);
  });

  it("resets a replace draft to append and explains why when the runtime cannot replace", async () => {
    const savedProfile = { ...PROFILE, system_prompt_mode: "replace" as const };
    await show(savedProfile);
    await choose("Runtime", "rt-stand-in");
    expect(select("System prompt").value).toBe("append");
    expect(container.textContent).toContain(REFUSAL);
    expect(container.textContent).toContain("The editor changed this draft to append.");
    expect(savedProfile.system_prompt_mode).toBe("replace");
  });

  it("shows replace mode in the read-only profile summary", async () => {
    const registry = { ...REGISTRY, profiles: [{ ...PROFILE, system_prompt_mode: "replace" as const }] };
    vi.stubGlobal("fetch", vi.fn(async () => new Response(JSON.stringify(registry), { status: 200 })));
    await act(async () => root.render(<AgentProfilesSection />));
    expect(container.textContent).toContain("agent teams on · replaces default system prompt");
  });
});
