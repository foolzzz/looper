// @vitest-environment happy-dom
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { AgentProfileForm, AgentProfilesSection } from "@/components/agent-profiles";
import { fetchPromptPreview, type AgentProfile, type Registry } from "@/lib/api";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

const REFUSAL = "the Stand-in runtime cannot replace its default system prompt";
const PROFILE: AgentProfile = {
  profile_id: "p", name: "Profile", model: "model", effort: null, agent_teams: true,
  system_prompt_mode: "append", custom_prompt: "", runtime_id: "rt-claude-code", runtime_type: "claude-code",
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

describe("Agent Profile prompts", () => {
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
