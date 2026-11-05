// @vitest-environment happy-dom
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { ProviderModelPicker } from "@/components/provider-model-picker";
import { addModels, fetchProviderModels, type ListedModel, type ModelListing, type Provider, type Registry } from "@/lib/api";

vi.mock("@/lib/api", async (original) => ({ ...await original<typeof import("@/lib/api")>(),
  addModels: vi.fn(), fetchProviderModels: vi.fn() }));
(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
const provider = { provider_id: "saved-account", name: "Gateway" } as Provider;
const registry = { limits: { api_formats: [{ family: "openai", label: "OpenAI", protocols: [
  { protocol: "openai-chat", label: "OpenAI Chat Completions" }, { protocol: "openai-responses", label: "OpenAI Responses" },
] }] } } as Registry;
const model = (name: string, changes: Partial<ListedModel> = {}): ListedModel => ({ name, context_window: null,
  registered: [], builtin_price: false, addable: true, problem: null, ...changes });
const listing = (changes: Partial<ModelListing> = {}): ModelListing => ({ family: "openai", base_url: "https://example.invalid/v1",
  protocols: ["openai-chat", "openai-responses"], state: "listed", problem: null, truncated: false, models: [model("alpha")], ...changes });
let container: HTMLDivElement;
let root: Root;
const done = vi.fn();
beforeEach(() => {
  vi.clearAllMocks();
  vi.mocked(fetchProviderModels).mockResolvedValue({ provider_id: provider.provider_id, listings: [listing()] });
  vi.mocked(addModels).mockResolvedValue({ models: [], skipped: [], warnings: [] });
  container = document.createElement("div"); document.body.append(container); root = createRoot(container);
});
afterEach(() => { act(() => root.unmount()); container.remove(); });
const render = async () => { await act(async () => root.render(<ProviderModelPicker provider={provider} registry={registry} onDone={done} />)); };
const checkbox = (name: string) => container.querySelector<HTMLInputElement>(`input[aria-label="${name}"]`)!;
const button = (text: string) => [...container.querySelectorAll("button")].find((item) => item.textContent === text)!;
const click = async (element: HTMLElement) => { await act(async () => element.click()); };
const results = (value: ModelListing) => vi.mocked(fetchProviderModels).mockResolvedValue({ provider_id: provider.provider_id, listings: [value] });

it("shows accessible loading while fetching only a saved provider id", async () => {
  vi.mocked(fetchProviderModels).mockReturnValue(new Promise(() => {}));
  await render();
  expect(container.querySelector('[role="status"]')?.textContent).toBe("Asking Gateway for its models…");
  expect(fetchProviderModels).toHaveBeenCalledWith("saved-account");
  expect(button("Add 0 models").disabled).toBe(true);
  await click(button("Done")); expect(done).toHaveBeenCalledOnce();
});

it("explains endpoint failure and fetches again", async () => {
  results(listing({ state: "failed", problem: "the provider refused the request (HTTP 401): check the provider's API key", models: [] }));
  await render();
  expect(container.textContent).toContain("OpenAI at https://example.invalid/v1: could not list models: the provider refused");
  expect(container.textContent).toContain("The provider is saved; add models by hand under Models, or fetch again.");
  results(listing()); await click(button("Fetch again"));
  expect(fetchProviderModels).toHaveBeenCalledTimes(2); expect(checkbox("alpha")).toBeTruthy();
});

it("starts unticked, adds only missing protocols, and marks added rows", async () => {
  results(listing({ models: [model("alpha", { registered: ["openai-chat"], context_window: 8192, builtin_price: true }), model("beta")] }));
  vi.mocked(addModels).mockResolvedValue({ models: [{ name: "alpha", protocol: "openai-responses" }] as never[], skipped: [], warnings: [] });
  await render(); expect(checkbox("alpha").checked).toBe(false); expect(checkbox("beta").checked).toBe(false);
  expect(container.textContent).toContain("OpenAI Chat Completions + OpenAI Responses · context 8192 · priced by Looper");
  expect(container.textContent).toContain("price 0 until you set one");
  await click(checkbox("alpha")); await click(button("Add 1 model"));
  expect(addModels).toHaveBeenCalledWith("saved-account", [{ name: "alpha", protocol: "openai-responses", context_window: 8192 }]);
  expect(checkbox("alpha").checked).toBe(true); expect(checkbox("alpha").disabled).toBe(true);
  expect(container.textContent).toContain("Added 1 models.");
});

it("expands a selection into every missing protocol and reads back skipped duplicates", async () => {
  vi.mocked(addModels).mockResolvedValue({ models: [], skipped: [
    { name: "alpha", protocol: "openai-chat", reason: "already registered" },
    { name: "alpha", protocol: "openai-responses", reason: "already registered" },
  ], warnings: [] });
  await render(); await click(checkbox("alpha")); await click(button("Add 2 models"));
  expect(addModels).toHaveBeenCalledWith("saved-account", [
    { name: "alpha", protocol: "openai-chat" }, { name: "alpha", protocol: "openai-responses" },
  ]);
  expect(container.textContent).toContain("alpha (openai-chat): already registered");
  expect(checkbox("alpha").disabled).toBe(true);
});

it("disables registered and invalid rows and never preselects a new row", async () => {
  results(listing({ models: [model("added", { registered: ["openai-chat", "openai-responses"] }),
    model("invalid@name", { addable: false, problem: "model name is invalid" }), model("new")] }));
  await render(); expect(checkbox("added").checked).toBe(true); expect(checkbox("added").disabled).toBe(true);
  expect(checkbox("invalid@name").disabled).toBe(true); expect(checkbox("new").checked).toBe(false);
  expect(container.textContent).toContain("model name is invalid"); expect(button("Add 0 models").disabled).toBe(true);
});

it("filters a large list and selects only shown addable models", async () => {
  results(listing({ models: Array.from({ length: 15 }, (_, index) => model(`model-${index}`)) })); await render();
  const input = container.querySelector<HTMLInputElement>('input[aria-label="Filter models"]')!;
  await act(async () => { Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value")!.set!.call(input, "MODEL-14");
    input.dispatchEvent(new Event("input", { bubbles: true })); });
  expect(checkbox("model-0")).toBeNull();
  const all = [...container.querySelectorAll("label")].find((item) => item.textContent === "Select all shown")!.querySelector("input")!;
  await click(all); expect(checkbox("model-14").checked).toBe(true); expect(button("Add 2 models").disabled).toBe(false);
});

it("caps actual model entries at 200, including multiple protocols", async () => {
  results(listing({ truncated: true, models: Array.from({ length: 101 }, (_, index) => model(`model-${index}`)) })); await render();
  const all = [...container.querySelectorAll("label")].find((item) => item.textContent === "Select all shown")!.querySelector("input")!;
  await click(all); expect(button("Add 202 models").disabled).toBe(true);
  expect(container.textContent).toContain("Choose at most 200 models at a time.");
  expect(container.textContent).toContain("Showing the first 1000 models.");
  await click(checkbox("model-0")); expect(button("Add 200 models").disabled).toBe(false);
});

it("preserves selection across registry refreshes and reports add failures without marking rows", async () => {
  vi.mocked(addModels).mockRejectedValue(new Error("entry is invalid"));
  await render(); await click(checkbox("alpha"));
  await act(async () => root.render(<ProviderModelPicker provider={{ ...provider }} registry={{ ...registry }} onDone={done} />));
  expect(fetchProviderModels).toHaveBeenCalledOnce(); expect(checkbox("alpha").checked).toBe(true);
  await click(button("Add 2 models"));
  expect(container.querySelector('[role="alert"]')?.textContent).toBe("entry is invalid");
  expect(checkbox("alpha").disabled).toBe(false); expect(checkbox("alpha").checked).toBe(true);
});
