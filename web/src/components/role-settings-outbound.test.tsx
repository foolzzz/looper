// @vitest-environment happy-dom
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { RolesSection } from "@/components/role-settings";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
const values = { harness_prompt: null, boundaries: null, profile_id: null, env: [], run_budget_usd: null, max_concurrency: null };
const effective = { harness_prompt: { value: "", source: "builtin" }, env: [],
  profile: { value: { profile_id: "builtin", name: "Built-in classifier", model: "m", effort: null, runtime: "r", provider: "v" }, source: "builtin" },
  run_budget_usd: { value: null, source: "builtin" }, system_prompt_mode: "replace" };
const limits = { harness_max_chars: 20000, env_max_entries: 20, builtin_harness_prompt: "", builtin_boundaries: "",
  builtin_model: "m", builtin_profile: { profile_id: "shared", name: "Shared" }, budget_max_usd: 100, concurrency_max: 64 };
const config = { role: "outbound_check", display_name: "Outbound check", enabled: true, system_prompt: "Review the action.",
  default_prompt: "Review the action.", timeout_seconds: 30, max_turns: 1 };
const state = { values, effective };
const settings = { limits, system: state, roles: { outbound_check: state } };
const profile = { profile_id: "classifier", name: "Classifier", runtime_name: "Runtime", runtime_type: "r", effort: null,
  provider: { name: "Account", enabled: true }, model: { name: "Model", protocol: "p" }, roles: ["outbound_check"] };
let container: HTMLDivElement;
let root: Root;
let fail = false;
let fetcher: ReturnType<typeof vi.fn>;
beforeEach(() => {
  fail = false;
  fetcher = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input);
    if (fail && (url.endsWith("/roles/update") || url.endsWith("/agent_settings/update_role"))) {
      return Response.json({ status: "error", error: { code: "unavailable", message: "Save unavailable" } }, { status: 503 });
    }
    const body = JSON.parse(String(init?.body ?? "{}"));
    const result = url.endsWith("/registry/view") ? { providers: [], profiles: [profile, { ...profile, profile_id: "unsupported", name: "Tools only", roles: ["worker"] }] }
      : url.endsWith("/roles/list") ? { configs: [config] }
      : url.endsWith("/roles/update") ? { ...config, ...body.fields }
      : settings;
    return Response.json({ status: "ok", result });
  });
  vi.stubGlobal("fetch", fetcher);
  Element.prototype.scrollIntoView = vi.fn();
  container = document.createElement("div");
  document.body.append(container);
  root = createRoot(container);
});
afterEach(() => { act(() => root.unmount()); container.remove(); vi.unstubAllGlobals(); });
async function render() { await act(async () => root.render(<RolesSection sub="outbound_check" />)); }
async function edit(selector: string, value: string) {
  await act(async () => {
    const field = container.querySelector<HTMLInputElement | HTMLTextAreaElement | HTMLSelectElement>(selector)!;
    const prototype = field instanceof HTMLTextAreaElement ? HTMLTextAreaElement.prototype
      : field instanceof HTMLSelectElement ? HTMLSelectElement.prototype : HTMLInputElement.prototype;
    Object.getOwnPropertyDescriptor(prototype, "value")!.set!.call(field, value);
    field.dispatchEvent(new Event(field instanceof HTMLSelectElement ? "change" : "input", { bubbles: true }));
  });
}
async function savePrompt() { await act(async () => Array.from(container.querySelectorAll("button"))
  .find((b) => b.textContent?.includes("Save prompt and limits"))!.click()); }

it("opensOutboundRoleAndUsesOnlyItsOwnProfile", async () => {
  await render();
  expect(container.querySelector("#roles-outbound_check-button")?.getAttribute("aria-expanded")).toBe("true");
  expect(container.querySelector("#role-outbound_check-profile")).toBeTruthy();
  expect(container.textContent).toContain("Built-in classifier");
  expect(container.textContent).toContain("只使用自身设置或内置设置");
  expect(container.querySelector("#roles-shared-button")?.textContent).not.toContain("Applies to all 1 role");
  expect(fetcher.mock.calls.filter(([p]) => String(p).includes("update"))).toHaveLength(0);
});
it("hidesHarnessAndUsesActionContextHelp", async () => {
  await render();
  expect(container.querySelector("#role-outbound_check-harness")).toBeNull();
  expect(container.querySelector("#role-outbound_check-prompt-mode")).toBeNull();
  expect(container.textContent).toContain("只读动作及触发上下文，不带工具");
  expect(container.textContent).toContain("实际最多30秒");
  expect(container.textContent).not.toContain("project turns consults on");
  expect(container.textContent).toContain("判定不可用会拦截");
});
it("savesPromptTimeoutAndProfileViaExistingOperations", async () => {
  await render();
  await edit("textarea", "New classifier prompt");
  await edit('input[id$="-timeout"]', "15");
  await savePrompt();
  expect(fetcher.mock.calls.some(([p, i]) => String(p).endsWith("/roles/update") &&
    JSON.parse(String(i?.body)).role === "outbound_check" && JSON.parse(String(i?.body)).fields.timeout_seconds === 15 &&
    JSON.parse(String(i?.body)).fields.system_prompt === "New classifier prompt")).toBe(true);
  await edit("#role-outbound_check-profile", "classifier");
  await act(async () => container.querySelector("#role-outbound_check-profile")!.closest("form")!
    .dispatchEvent(new Event("submit", { bubbles: true, cancelable: true })));
  expect(fetcher.mock.calls.some(([p, i]) => String(p).endsWith("/agent_settings/update_role") &&
    JSON.stringify(JSON.parse(String(i?.body))) === JSON.stringify({ role: "outbound_check", values: { profile_id: "classifier" } }))).toBe(true);
});
it("retainsDraftAfterFailureAndShowsUnavailableProfiles", async () => {
  await render();
  expect(container.querySelector<HTMLOptionElement>('option[value="unsupported"]')?.disabled).toBe(true);
  fail = true;
  await edit("textarea", "Unsaved classifier prompt");
  await savePrompt();
  expect(container.querySelector("textarea")!.value).toBe("Unsaved classifier prompt");
  expect(container.textContent).toContain("Save unavailable");
});
