// @vitest-environment happy-dom
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { ConfigTransferCard } from "@/components/config-transfer";
import { type ConfigImportPlan, type ConfigImportUpload } from "@/lib/api";
import { useRegistry } from "@/lib/use-registry";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

const counts = (added = 0, changed = 0, unchanged = 0, skipped = 0) => ({ added, changed, unchanged, skipped });
const preview = (): ConfigImportPlan => ({
  applied: false,
  counts: { providers: counts(1), models: counts(1, 1, 2), profiles: counts(0, 0, 0, 1) },
  changes: [
    { section: "providers", key: "Account", action: "added" },
    { section: "models", key: "Account / exact-model / openai-chat", action: "added" },
    { section: "models", key: "Account / exact-model / anthropic-messages", action: "changed", fields: ["context_window"] },
    { section: "profiles", key: "Unsupported profile", action: "skipped", reason: "no valid model here or in the file", code: "model_outside_catalog" },
  ],
  need_secret: [{ section: "providers", key: "Account", names: ["api_key"] }],
  notes: [],
});

function RegistryReader() {
  const { registry } = useRegistry();
  return <output>{registry?.models.map((model) => model.name).join(", ")}</output>;
}

describe("configuration import", () => {
  let container: HTMLDivElement;
  let root: Root;
  let plan: ConfigImportPlan;
  let applied: ConfigImportPlan;
  let fetcher: ReturnType<typeof vi.fn<typeof fetch>>;
  let onImported: ReturnType<typeof vi.fn<() => void>>;
  let imported: boolean;

  beforeEach(() => {
    plan = preview();
    applied = { ...preview(), applied: true };
    imported = false;
    onImported = vi.fn();
    vi.stubGlobal("confirm", vi.fn(() => true));
    fetcher = vi.fn<typeof fetch>(async (input, init) => {
      const url = String(input);
      if (url === "/api/v1/registry/view") {
        return Response.json({ status: "ok", result: { models: [{ name: imported ? "Imported model" : "Existing model" }] } });
      }
      if (url === "/api/config/imports") {
        return Response.json({
          import_id: "pending-import", bytes: 512, expires_at: "2030-01-01T12:00:00Z",
          file: { format: "looper-config/v3", secrets: "included" }, plan,
        } satisfies ConfigImportUpload);
      }
      if (url === "/api/config/imports/pending-import/apply") {
        imported = true;
        return Response.json({ import_id: "pending-import", plan: applied });
      }
      if (url === "/api/config/imports/pending-import" && init?.method === "DELETE") return Response.json({});
      throw new Error(`Unexpected request: ${init?.method ?? "GET"} ${url}`);
    });
    vi.stubGlobal("fetch", fetcher);
    container = document.createElement("div");
    document.body.append(container);
    root = createRoot(container);
  });

  afterEach(() => {
    act(() => root.unmount());
    container.remove();
    vi.unstubAllGlobals();
  });

  const show = async (registry = false, readers = 2) => {
    await act(async () => root.render(<>
      <ConfigTransferCard onImported={onImported} />
      {registry && <>{Array.from({ length: readers }, (_, index) => <RegistryReader key={index} />)}</>}
    </>));
  };
  const upload = async () => {
    const file = new File(['{"api_key":"secret-never-rendered"}'], "configuration.json", { type: "application/json" });
    await act(async () => {
      const input = container.querySelector<HTMLInputElement>('input[type="file"]')!;
      Object.defineProperty(input, "files", { configurable: true, value: [file] });
      input.dispatchEvent(new Event("change", { bubbles: true }));
    });
    return file;
  };
  const button = (text: string) => {
    const result = [...container.querySelectorAll("button")].find((item) => item.textContent?.includes(text));
    expect(result, text).toBeTruthy();
    return result!;
  };
  const click = async (text: string) => act(async () => button(text).click());

  it("shows the server's Models row, exact model keys and skipped profile reasons without secret values", async () => {
    await show();
    const file = await upload();
    const row = [...container.querySelectorAll("tbody tr")].find((item) => item.querySelector("th")?.textContent === "Models");
    expect(row).toBeTruthy();
    expect([...row!.querySelectorAll("td")].map((cell) => cell.textContent)).toEqual(["1", "1", "2", "0"]);
    expect(container.textContent).toContain("Account / exact-model / anthropic-messages");
    expect(container.textContent).toContain("context_window");
    const skipped = [...container.querySelectorAll("li")].find((item) => item.textContent?.includes("Unsupported profile"))!;
    expect(skipped.textContent).toContain("no valid model here or in the file");
    expect(skipped.textContent).toContain("model_outside_catalog");
    expect(skipped.closest("div")?.querySelector("h4")?.className).toContain("text-warning");
    expect(button("Apply 3 changes").disabled).toBe(false);
    expect(container.querySelector('[role="status"]')).toBeNull();
    expect(container.textContent).not.toContain("secret-never-rendered");
    expect(fetcher).toHaveBeenCalledWith("/api/config/imports", expect.objectContaining({ method: "POST", body: file }));
  });

  it("keeps a skipped-only plan unapplied", async () => {
    plan.counts = { profiles: counts(0, 0, 0, 1) };
    plan.changes = plan.changes.filter((change) => change.action === "skipped");
    await show();
    await upload();
    expect(button("Nothing to apply").disabled).toBe(true);
    expect(container.textContent).toContain("no valid model here or in the file");
    expect(container.querySelector(".text-success")).toBeNull();
    expect(onImported).not.toHaveBeenCalled();
  });

  it("links missing secrets only to server-supplied stored targets and reads back applied results", async () => {
    applied.counts = { models: counts(1), profiles: counts(0, 0, 0, 2) };
    applied.need_secret = [
      { section: "providers", key: "Canonical account", names: ["api_key"], target: { kind: "provider", id: "provider/merged" } },
      { section: "catalog", key: "MCP", names: ["header Authorization"], target: { kind: "mcp", id: "mcp-one" } },
      { section: "channels", key: "lark", names: ["app_secret"], target: { kind: "channel", id: "channel-one" } },
    ];
    await show();
    await upload();
    expect(container.querySelector('a[href^="#settings/"]')).toBeNull();
    await click("Apply 3 changes");
    expect([...container.querySelectorAll("a")].map((link) => link.getAttribute("href"))).toEqual([
      "#settings/providers/provider%2Fmerged", "#settings/catalog/mcp/mcp-one", "#settings/channels/channel-one",
    ]);
    expect(container.textContent).toContain("Secrets to fill in · 3");
    expect(container.textContent).toContain("Canonical account");
    expect(container.textContent).not.toContain("secret-never-rendered");
    const status = container.querySelector('[role="status"]')!;
    expect(status.textContent).toContain("1 entry written");
    expect(status.textContent).toContain("2 skipped");
    expect(status.className).toContain("text-warning");
    expect(status.className).not.toContain("text-success");
  });

  it("shows success only for applied results without skipped entries", async () => {
    applied.counts = { models: counts(2) };
    applied.changes = [{ section: "models", key: "New model", action: "added" }];
    await show();
    await upload();
    await click("Apply 3 changes");
    const status = container.querySelector('[role="status"]')!;
    expect(status.textContent).toContain("2 entries written");
    expect(status.className).toContain("text-success");
    expect(container.textContent).not.toContain("Unsupported profile");
  });

  it("refreshes every mounted registry consumer after apply, then exposes the new cache to later consumers", async () => {
    await show(true);
    expect([...container.querySelectorAll("output")].map((item) => item.textContent)).toEqual(["Existing model", "Existing model"]);
    await upload();
    expect(fetcher.mock.calls.filter(([url]) => url === "/api/v1/registry/view")).toHaveLength(1);
    await click("Apply 3 changes");
    expect(onImported).toHaveBeenCalledTimes(1);
    expect(fetcher.mock.calls.filter(([url]) => url === "/api/v1/registry/view")).toHaveLength(2);
    expect([...container.querySelectorAll("output")].map((item) => item.textContent)).toEqual(["Imported model", "Imported model"]);
    await show(true, 3);
    expect([...container.querySelectorAll("output")].every((item) => item.textContent === "Imported model")).toBe(true);
    expect(fetcher.mock.calls.filter(([url]) => url === "/api/v1/registry/view")).toHaveLength(2);
    expect(fetcher.mock.calls.some(([url, init]) => String(url).endsWith("/apply") && init?.body !== undefined)).toBe(false);
  });

  it("reads back upload errors without showing a successful preview", async () => {
    fetcher.mockResolvedValueOnce(Response.json({ error: "unsupported configuration format" }, { status: 400 }));
    await show();
    await upload();
    expect(container.querySelector('[role="alert"]')?.textContent).toContain("unsupported configuration format");
    expect(container.querySelector("table")).toBeNull();
    expect(onImported).not.toHaveBeenCalled();
  });

  it("keeps a failed apply retryable and refreshes only after a successful retry", async () => {
    await show(true);
    await upload();
    fetcher.mockResolvedValueOnce(Response.json({ error: "provider endpoint conflicts with an existing profile" }, { status: 409 }));
    await click("Apply 3 changes");
    expect(container.querySelector('[role="alert"]')?.textContent).toContain("provider endpoint conflicts");
    expect(container.querySelector('[role="status"]')).toBeNull();
    expect(button("Apply 3 changes").disabled).toBe(false);
    expect(onImported).not.toHaveBeenCalled();
    expect(fetcher.mock.calls.filter(([url]) => url === "/api/v1/registry/view")).toHaveLength(1);
    await click("Apply 3 changes");
    expect(container.querySelector('[role="alert"]')).toBeNull();
    expect(onImported).toHaveBeenCalledTimes(1);
    expect(fetcher.mock.calls.filter(([url]) => url === "/api/v1/registry/view")).toHaveLength(2);
  });

  it("leaves the preview pending when confirmation is declined and cancels it on request", async () => {
    await show();
    await upload();
    vi.stubGlobal("confirm", vi.fn(() => false));
    await click("Apply 3 changes");
    expect(fetcher.mock.calls.some(([url]) => String(url).endsWith("/apply"))).toBe(false);
    expect(onImported).not.toHaveBeenCalled();
    expect(container.querySelector("table")).not.toBeNull();
    await click("Cancel");
    expect(fetcher).toHaveBeenCalledWith("/api/config/imports/pending-import", { method: "DELETE" });
    expect(container.querySelector("table")).toBeNull();
    expect(container.querySelector('[role="status"]')).toBeNull();
  });
});
