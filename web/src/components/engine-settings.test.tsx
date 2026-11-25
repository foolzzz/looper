// @vitest-environment happy-dom
import { act } from "react";
import { createRoot } from "react-dom/client";
import { afterEach, expect, it, vi } from "vitest";
import { EngineSettingsSection } from "./engine-settings";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
afterEach(() => vi.unstubAllGlobals());
it("shows built-in off and keeps it until an explicit confirmed save succeeds", async () => {
  let writes = 0; let confirms = 0;
  const setting = (value: string, source: string) => ({ failure_typing: { value, source, built_in: "off" } });
  vi.stubGlobal("fetch", vi.fn(async (path: string) => {
    if (path.endsWith("/get")) return Response.json({ status: "ok", result: setting("off", "built-in") });
    if (path.endsWith("/confirm")) { confirms++; return Response.json({ status: "ok", result: setting("act", "setting") }); }
    writes++; return Response.json({ status: "needs_confirmation", confirmation_id: "engine", expires_in_seconds: 600, impact: "Enable bounded retries" }, { status: 202 });
  }));
  const host = document.createElement("div"); document.body.append(host); const root = createRoot(host);
  const button = (text: string) => [...host.querySelectorAll<HTMLButtonElement>("button")].find(b => b.textContent === text)!;
  try {
    await act(async () => root.render(<EngineSettingsSection />));
    expect(host.textContent).toContain("当前：off"); expect(host.textContent).toContain("来源：内置默认"); expect(writes).toBe(0);
    const select = host.querySelector("select")!;
    await act(async () => { select.value = "act"; select.dispatchEvent(new Event("change", { bubbles: true })); });
    await act(async () => button("保存模式").click());
    expect(host.textContent).toContain("当前：off"); expect(confirms).toBe(0);
    await act(async () => button("取消").click());
    expect(confirms).toBe(0);
    await act(async () => button("保存模式").click());
    await act(async () => button("确认保存").click());
    expect(confirms).toBe(1); expect(host.textContent).toContain("当前：act"); expect(host.textContent).toContain("来源：系统设置");
  } finally { act(() => root.unmount()); host.remove(); }
});
