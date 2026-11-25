// @vitest-environment happy-dom
import { act } from "react";
import { createRoot } from "react-dom/client";
import { afterEach, expect, it, vi } from "vitest";
import { MorningBriefSettingsSection } from "./morning-brief-settings";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
afterEach(() => vi.unstubAllGlobals());

async function mount(fail = false) {
  const requests: { path: string; body: Record<string, unknown>; session: string }[] = [];
  const settings = { enabled: false, time: "09:15", cutoff: "12:00" };
  vi.stubGlobal("fetch", vi.fn(async (path: string, init: RequestInit) => {
    requests.push({ path, body: JSON.parse(String(init.body)),
      session: (init.headers as Record<string, string>)["X-Looper-Session"] });
    if (fail) return Response.json({ status: "error", error: { code: "unavailable", message: "Settings unavailable" } }, { status: 503 });
    if (path.endsWith("/update")) return Response.json({ status: "needs_confirmation", confirmation_id: "brief-confirmation",
      expires_in_seconds: 300, impact: "更新晨报发送时间与补发截止时间。" }, { status: 202 });
    return Response.json({ status: "ok", result: settings });
  }));
  const container = document.createElement("div"); document.body.append(container);
  const root = createRoot(container);
  await act(async () => root.render(<MorningBriefSettingsSection />));
  return { container, requests, close: () => { act(() => root.unmount()); container.remove(); } };
}

async function enter(input: HTMLInputElement, value: string) {
  await act(async () => {
    Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value")!.set!.call(input, value);
    input.dispatchEvent(new Event("input", { bubbles: true }));
  });
}

async function submit(container: HTMLElement) {
  await act(async () => container.querySelector("form")!.dispatchEvent(new Event("submit", { bubbles: true, cancelable: true })));
}

it("loads both local times and saves only after the operator confirms", async () => {
  const page = await mount();
  try {
    expect(page.container.querySelector<HTMLInputElement>('[id$="-time"]')!.value).toBe("09:15");
    expect(page.container.querySelector<HTMLInputElement>('[id$="-cutoff"]')!.value).toBe("12:00");
    await enter(page.container.querySelector('[id$="-cutoff"]')!, "11:30");
    await act(async () => page.container.querySelector<HTMLButtonElement>('[role="switch"]')!.click());
    await submit(page.container);
    expect(page.requests.map(({ path }) => path)).toEqual(["/api/v1/morning_brief/get", "/api/v1/morning_brief/update"]);
    expect(page.requests[1].body).toEqual({ enabled: true, time: "09:15", cutoff: "11:30" });
    expect(page.container.querySelector('[role="dialog"]')?.textContent).toContain("更新晨报");
    const confirm = [...page.container.querySelectorAll<HTMLButtonElement>('[role="dialog"] button')]
      .find((button) => button.textContent === "确认保存")!;
    await act(async () => confirm.click());
    expect(page.requests[2]).toMatchObject({ path: "/api/v1/confirm", body: { confirmation_id: "brief-confirmation" },
      session: page.requests[1].session });
    expect(page.container.querySelector('[role="dialog"]')).toBeNull();
    expect(page.container.textContent).toContain("已保存");
  } finally { page.close(); }
});

it("sends null for blank times to restore defaults and cancellation does not confirm", async () => {
  const page = await mount();
  try {
    await enter(page.container.querySelector('[id$="-time"]')!, "");
    await enter(page.container.querySelector('[id$="-cutoff"]')!, "");
    await submit(page.container);
    expect(page.requests[1].body).toEqual({ enabled: false, time: null, cutoff: null });
    const cancel = [...page.container.querySelectorAll<HTMLButtonElement>('[role="dialog"] button')]
      .find((button) => button.textContent === "取消")!;
    await act(async () => cancel.click());
    expect(page.requests).toHaveLength(2);
    expect(page.container.querySelector('[role="dialog"]')).toBeNull();
  } finally { page.close(); }
});

it("refuses a cutoff at or before the send time without a request", async () => {
  const page = await mount();
  try {
    await enter(page.container.querySelector('[id$="-cutoff"]')!, "09:15");
    await submit(page.container);
    expect(page.requests).toHaveLength(1);
    expect(page.container.querySelector('[role="alert"]')?.textContent).toContain("晚于");
  } finally { page.close(); }
});

it("keeps failed settings unavailable and offers a retry", async () => {
  const page = await mount(true);
  try {
    expect(page.container.querySelector('[role="alert"]')?.textContent).toContain("Settings unavailable");
    expect(page.container.querySelector("form")).toBeNull();
    await act(async () => page.container.querySelector<HTMLButtonElement>("button")!.click());
    expect(page.requests).toHaveLength(2);
    expect(page.requests[1].path).toBe("/api/v1/morning_brief/get");
  } finally { page.close(); }
});
