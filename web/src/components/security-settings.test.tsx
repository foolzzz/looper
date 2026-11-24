// @vitest-environment happy-dom
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { SecuritySettingsSection } from "@/components/security-settings";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
let container: HTMLDivElement, root: Root;
const ok = (result: unknown) => Response.json({ status: "ok", result });
const off = { enabled: false, managers_without_hook: [] };
beforeEach(() => { container = document.createElement("div"); document.body.append(container); root = createRoot(container); });
afterEach(() => { act(() => root.unmount()); container.remove(); vi.unstubAllGlobals(); });
const click = async (text: string) => act(async () => [...container.querySelectorAll("button")].find((button) => button.textContent === text)!.click());
const toggle = async (name: string) => act(async () => container.querySelector<HTMLButtonElement>(`[role="switch"][aria-label="${name}"]`)!.click());
it("rendersOffWithoutSavingDefaults", async () => {
  const fetcher = vi.fn().mockImplementation(async () => ok(off)); vi.stubGlobal("fetch", fetcher);
  await act(async () => root.render(<SecuritySettingsSection />));
  expect(container.querySelectorAll('[role="switch"][aria-checked="false"]')).toHaveLength(2);
  expect(fetcher.mock.calls.map(([url]) => url).sort()).toEqual(["/api/v1/outbound_review/get", "/api/v1/untrusted_fence/get"]);
  expect(container.querySelector('a[href="#settings/roles/outbound_check"]')).not.toBeNull();
});
it("confirmsEachSwitchAndKeepsThemIndependent", async () => {
  const fetcher = vi.fn(async (url: string) => url.endsWith("/get") ? ok(off) : url.endsWith("/confirm") ? ok({ ...off, enabled: true })
    : Response.json({ status: "needs_confirmation", confirmation_id: "review-confirm", expires_in_seconds: 60, impact: "Server impact" }, { status: 202 }));
  vi.stubGlobal("fetch", fetcher); await act(async () => root.render(<SecuritySettingsSection />));
  await toggle("对外动作审查"); expect(fetcher).toHaveBeenCalledTimes(2);
  await click("取消"); expect(fetcher).toHaveBeenCalledTimes(2);
  expect(document.activeElement?.getAttribute("aria-label")).toBe("对外动作审查");
  await toggle("对外动作审查"); await click("确认更改");
  expect(fetcher).toHaveBeenCalledTimes(3); expect(container.textContent).toContain("Server impact");
  expect(container.querySelector<HTMLButtonElement>('[aria-label="不可信内容围栏"]')?.disabled).toBe(false);
  await click("确认执行");
  expect(fetcher.mock.calls.filter(([url]) => url === "/api/v1/confirm")).toHaveLength(1);
  expect(container.querySelector('[aria-label="对外动作审查"]')?.getAttribute("aria-checked")).toBe("true");
  await toggle("不可信内容围栏"); await click("取消");
});
it("showsUnsupportedManagersAndKeepsSavedValueOnConflict", async () => {
  const fetcher = vi.fn(async (url: string) => url.endsWith("/get") ? ok({ ...off, managers_without_hook: [{ manager_id: "manager-example", name: "Example", runtime: "sample-runtime" }] })
    : Response.json({ status: "error", error: { code: "conflict", message: "Example runtime unsupported" } }, { status: 409 }));
  vi.stubGlobal("fetch", fetcher); await act(async () => root.render(<SecuritySettingsSection />));
  expect(container.textContent).toContain("sample-runtime"); await toggle("对外动作审查"); await click("确认更改");
  expect(container.textContent).toContain("Example runtime unsupported");
  expect(container.querySelector('[aria-label="对外动作审查"]')?.getAttribute("aria-checked")).toBe("false");
  expect(container.querySelector('[role="dialog"]')).toBeNull();
});
it("rendersLoadingErrorAndForbiddenWithoutEditableFallback", async () => {
  let finish!: (value: Response) => void;
  vi.stubGlobal("fetch", vi.fn(() => new Promise<Response>((resolve) => { finish = resolve; })));
  await act(async () => root.render(<SecuritySettingsSection />));
  expect(container.querySelector('[role="status"]')?.textContent).toContain("读取");
  await act(async () => finish(Response.json({ status: "error", error: { code: "forbidden", message: "Operator only" } }, { status: 403 })));
  expect(container.textContent).toContain("仅操作者");
  expect(container.querySelectorAll('[role="switch"]')).toHaveLength(0);
});
it("keepsAnUnreadSettingDisabledUntilAReadSucceeds", async () => {
  const fetcher = vi.fn(async (url: string) => url.includes("untrusted_fence") ? ok({ enabled: false })
    : Response.json({ status: "error", error: { code: "unavailable", message: "Review settings unavailable" } }, { status: 503 }));
  vi.stubGlobal("fetch", fetcher); await act(async () => root.render(<SecuritySettingsSection />));
  expect(container.textContent).toContain("Review settings unavailable");
  expect(container.querySelector('[aria-label="对外动作审查"]')).toBeNull();
  expect(container.querySelector<HTMLButtonElement>('[aria-label="不可信内容围栏"]')?.disabled).toBe(false);
  fetcher.mockImplementation(async () => ok(off)); await click("重新读取");
  expect(container.querySelector<HTMLButtonElement>('[aria-label="对外动作审查"]')?.disabled).toBe(false);
});
