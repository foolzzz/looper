// @vitest-environment happy-dom
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { ManagerInstalls } from "@/components/manager-installs";
import * as api from "@/lib/api";
import * as installsApi from "@/lib/manager-installs-api";
import type { ManagerInstall, ManagerInstallsSnapshot } from "@/lib/manager-installs-api";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

vi.mock("@/lib/api", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/lib/api")>()),
  fetchCatalog: vi.fn(), fetchRegistry: vi.fn(), fetchAgentSettings: vi.fn(),
}));
vi.mock("@/lib/manager-installs-api", () => ({
  fetchManagerInstalls: vi.fn(), putManagerInstall: vi.fn(), deleteManagerInstall: vi.fn(),
}));

const m = vi.mocked(api);
const installs = vi.mocked(installsApi);
const manager = (id: string, profileId: string | null = "profile"): api.ManagerInfo => ({
  id, name: id, description: "", builtin: false, enabled: true, tools: "tasks", task_scope: "own",
  task_policy: "free", idle_timeout_seconds: 1800, system_prompt: "", profile_id: profileId, profile_name: null,
  templates: [], revision: 0, created_at: "", updated_at: "",
  state: { running: false, busy: false, healthy: true, sync: "ok" },
  spend: { today_usd: 0, last_7d_usd: 0, runs_today: 0 },
});
const row = (name = "Confirmed skill"): ManagerInstall => ({
  item_id: "installed", name, kind: "skill", enabled: true, inherited: false, updated_at: null,
});
const snapshot = (id: string, rows: ManagerInstall[] = []): ManagerInstallsSnapshot => ({ manager_id: id, installs: rows });
const CANDIDATE: api.CatalogItem = {
  item_id: "candidate", name: "Candidate skill", qualified_name: "personal:candidate", kind: "skill",
  origin: "upload", owner_project_id: null, owner_project_name: null, description: "", source: "",
  version: "", sha256: "", size_bytes: 0, file_count: 1, modified: false, imported_at: "", updated_at: "",
  mcp: null, installs: [],
};
const PROFILE: api.AgentProfile = {
  profile_id: "profile", name: "Profile", model_id: "model", effort: null, agent_teams: false, custom_prompt: "",
  model: { model_id: "model", name: "Model", protocol: "anthropic-messages", provider_id: "provider" },
  runtime_id: "runtime", runtime_name: "Claude Code", runtime_type: "claude-code", builtin: false,
  provider: { provider_id: "provider", name: "Provider", vendor_type: "custom", auth: "api_key" },
  roles: ["manager"], used_by: { layers: [], task_count: 0, schedule_count: 0, allowed_in: [] },
  warnings: [], cost: { source: "runtime", equivalent: false, unpriced: [] },
};
const registry = (runtimeType = "claude-code", capabilities: string[] = ["claude_plugins"]): api.Registry => ({
  profiles: [{ ...PROFILE, runtime_type: runtimeType }],
  runtimes: [{ runtime_id: "runtime", name: "Runtime", type: runtimeType, type_label: "Runtime", in_process: false,
    executable: "", default_executable: null, path: null, protocols: [], version: null, capabilities,
    roles: ["manager"], probed_at: null, probe_error: null, builtin: false, profile_count: 1 }],
  runtime_types: [], providers: [], models: [], vendor_types: [], prices: { as_of: "", sources: [], models: {} },
  limits: { model_fields: [], name_max_chars: 100, models_max: 10, price_fields: [], protocols: [], auth_kinds: [],
    effort_levels: [], role_capabilities: { manager: [], planner: [], worker: [], reviewer: [] },
    secret_mask: "", provider_env_owned: [] },
});
function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((done) => { resolve = done; });
  return { promise, resolve };
}

describe("Manager install local state", () => {
  let container: HTMLDivElement;
  let root: Root;
  const render = async (target = manager("A")) => {
    await act(async () => root.render(<ManagerInstalls manager={target} hasScenarios />));
  };
  const button = (text: string) => [...container.querySelectorAll("button")].find((el) => el.textContent === text)!;
  const click = (element: HTMLElement) => act(async () => element.click());
  const open = () => click(button("Plugins and skills"));
  const picker = () => container.querySelector<HTMLSelectElement>("select")!;
  const choose = async () => {
    await act(async () => {
      picker().value = "candidate";
      picker().dispatchEvent(new Event("change", { bubbles: true }));
    });
  };

  beforeEach(() => {
    vi.resetAllMocks();
    installs.fetchManagerInstalls.mockImplementation(async (id) => snapshot(id));
    m.fetchCatalog.mockResolvedValue({ items: [CANDIDATE], installed_count: 0, total_bytes: 0,
      last_import: null, last_mcp_import: null, roles: [], task_roles: [],
      caps: { file_max_bytes: 1000, item_max_bytes: 1000, item_max_files: 10, library_max_bytes: 1000, upload_max_bytes: 1000 },
      mcp_caps: { servers_max: 10, variables_max: 10, value_max: 100, args_max: 10, mask: "" } });
    m.fetchRegistry.mockResolvedValue(registry());
    container = document.createElement("div");
    document.body.append(container);
    root = createRoot(container);
  });
  afterEach(() => {
    act(() => root.unmount());
    container.remove();
  });

  it("ignores an old Manager list even when its request does not honor cancellation", async () => {
    const late = deferred<ManagerInstallsSnapshot>();
    installs.fetchManagerInstalls.mockImplementation(async (id) => id === "A" ? late.promise : snapshot(id, [row("B skill")]));
    await render();
    await render(manager("B"));
    await open();
    await act(async () => late.resolve(snapshot("A", [row("A skill")])));
    expect(container.textContent).toContain("B skill");
    expect(container.textContent).not.toContain("A skill");
    expect(picker().getAttribute("id")).toBe(container.querySelector("label")!.htmlFor);
    expect(container.querySelector("section")!.getAttribute("aria-label")).toContain("B");
  });

  it.each(["mutation", "readback"] as const)("keeps a late A %s response off B and prevents duplicate in-flight submissions", async (stage) => {
    const late = deferred<ManagerInstallsSnapshot>();
    let aReads = 0;
    installs.fetchManagerInstalls.mockImplementation(async (id) => {
      if (id === "B") return snapshot(id, [row("B skill")]);
      return ++aReads === 1 ? snapshot(id, [row()]) : stage === "readback" ? late.promise : snapshot(id, [row("A saved skill")]);
    });
    installs.putManagerInstall.mockImplementation(async () => stage === "mutation" ? late.promise : snapshot("A"));
    await render();
    await open();
    await choose();
    await click(button("Add install"));
    expect(picker().disabled).toBe(true);
    expect(container.querySelector<HTMLButtonElement>('[role="switch"]')!.disabled).toBe(true);
    expect(button("Remove").disabled).toBe(true);
    expect(button("Add install").disabled).toBe(true);
    await act(async () => { container.querySelector("form")!.dispatchEvent(new Event("submit", { bubbles: true, cancelable: true })); });
    expect(installs.putManagerInstall).toHaveBeenCalledTimes(1);
    await render(manager("B"));
    await open();
    await act(async () => late.resolve(snapshot("A", [row("A saved skill")])));
    expect(container.textContent).toContain("B skill");
    expect(container.textContent).not.toContain("A saved skill");
    expect(container.textContent).not.toContain("Saved and read back");
    expect(picker().value).toBe("");
    expect(installs.putManagerInstall).toHaveBeenCalledWith("A", "candidate");
  });

  it.each(["missing target", "readback"] as const)("keeps the confirmed state and draft after a failed %s, without carrying the draft to B", async (failure) => {
    installs.fetchManagerInstalls.mockResolvedValueOnce(snapshot("A", [row()]));
    if (failure === "missing target") {
      installs.putManagerInstall.mockRejectedValue(new Error("Manager no longer exists (404)"));
    } else {
      installs.putManagerInstall.mockResolvedValue(snapshot("A", [{ ...row(), enabled: false }]));
      installs.fetchManagerInstalls.mockRejectedValueOnce(new Error("Readback unavailable"));
    }
    await render();
    await open();
    await choose();
    await click(button("Add install"));
    expect(container.querySelector('[role="alert"]')!.textContent).toContain(failure === "missing target" ? "404" : "Readback unavailable");
    expect(picker().value).toBe("candidate");
    expect(container.querySelector('[role="switch"]')!.getAttribute("aria-checked")).toBe("true");
    expect(container.textContent).not.toContain("Saved and read back");
    expect(button("Add install").disabled).toBe(false);
    await render(manager("B"));
    await open();
    expect(picker().value).toBe("");
    expect(button("Add install").disabled).toBe(true);
    expect(container.querySelector('[role="alert"]')).toBeNull();
    expect(installs.putManagerInstall).toHaveBeenCalledTimes(1);
  });

  it.each([
    { profileId: null, runtimeType: "claude-code", capabilities: ["claude_plugins"], expected: "supports Claude plugin loading." },
    { profileId: "profile", runtimeType: "claude-code", capabilities: [], expected: "only runtime-supported contents load." },
    { profileId: "profile", runtimeType: "pi", capabilities: ["claude_plugins"], expected: "Pi does not load catalog plugins or skills; they are recorded as skipped." },
  ])("uses resolved registry capabilities for shared or own $runtimeType profiles ($profileId)", async ({ profileId, runtimeType, capabilities, expected }) => {
    m.fetchRegistry.mockResolvedValue(registry(runtimeType, capabilities));
    // Only this API field participates in resolving the shared Manager profile.
    m.fetchAgentSettings.mockResolvedValue({ roles: { manager: { effective: { profile: { value: { profile_id: "profile" } } } } } } as api.AgentSettings);
    await render(manager("A", profileId));
    await open();
    expect(container.textContent).toContain(expected);
    expect(container.querySelector('a[href="#settings/profiles"]')).not.toBeNull();
    if (runtimeType === "pi" || !capabilities.length) expect(container.textContent).not.toContain("supports Claude plugin loading.");
    expect(m.fetchAgentSettings).toHaveBeenCalledTimes(profileId === null ? 1 : 0);
  });

  it("clears a shared-profile lookup error when the same Manager chooses its own profile", async () => {
    m.fetchAgentSettings.mockRejectedValue(new Error("Shared profile unavailable"));
    await render(manager("A", null));
    await open();
    expect(container.textContent).toContain("Shared profile unavailable");
    await render(manager("A", "profile"));
    expect(button("Plugins and skills").getAttribute("aria-expanded")).toBe("true");
    expect(container.textContent).toContain("supports Claude plugin loading.");
    expect(container.textContent).not.toContain("Shared profile unavailable");
    expect(m.fetchAgentSettings).toHaveBeenCalledTimes(1);
  });
});
