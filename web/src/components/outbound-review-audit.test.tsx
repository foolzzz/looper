// @vitest-environment happy-dom
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { OutboundReviewAudit } from "@/components/outbound-review-audit";
import type { ReviewRow } from "@/lib/api-security";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
let container: HTMLDivElement, root: Root;
const row: ReviewRow = { id: "9007199254740993", created_at: "2030-01-01T00:00:00Z", agent_id: "manager-example", task_id: null,
  parent_record_id: null, record_id: null, source: "hook", tool: "Bash", action: null, level: "unknown", target_kind: "machine",
  target: "example target", summary: "<script>bad()</script>", verdict: "block", failure: "protected", reason: "Protected action",
  signals: ["outside_request"], profile_id: null, model: null, cost_usd: null, latency_ms: null };
const ok = (items: ReviewRow[], next_cursor: string | null = null) => Response.json({ status: "ok", result: { items, next_cursor } });
beforeEach(() => { container = document.createElement("div"); document.body.append(container); root = createRoot(container); });
afterEach(() => { act(() => root.unmount()); container.remove(); window.history.replaceState(null, ""); vi.unstubAllGlobals(); });
const click = async (text: string) => act(async () => [...container.querySelectorAll("button")].find((button) => button.textContent === text)!.click());
it("rendersReviewFactsAndUnknownCost", async () => {
  vi.stubGlobal("fetch", vi.fn().mockResolvedValue(ok([row]))); await act(async () => root.render(<OutboundReviewAudit />));
  expect(container.textContent).toContain("Protected action"); expect(container.textContent).toContain("未知");
  expect(container.textContent).toContain("未记录"); await click("展开详情");
  expect(container.textContent).toContain(row.summary); expect(container.querySelector("script")).toBeNull();
  expect(container.querySelector('a[href^="#history"]')).toBeNull(); expect(container.querySelector('a[href^="#tasks/"]')).toBeNull();
  expect(container.textContent).not.toContain("手动放行");
});
it("paginatesWithoutDuplicateRowsAndResetsCursorOnFilters", async () => {
  const fetcher = vi.fn().mockResolvedValueOnce(ok([row], "page-2")).mockResolvedValueOnce(ok([{ ...row, id: "2" }]))
    .mockResolvedValueOnce(ok([row], "page-2")).mockResolvedValueOnce(ok([])); vi.stubGlobal("fetch", fetcher);
  await act(async () => root.render(<OutboundReviewAudit />)); await click("更早");
  expect(JSON.parse(fetcher.mock.calls[1][1].body)).toEqual({ limit: 50, cursor: "page-2" });
  expect(container.querySelectorAll("article")).toHaveLength(1); await click("返回");
  expect(JSON.parse(fetcher.mock.calls[2][1].body)).toEqual({ limit: 50 });
  await act(async () => container.querySelector<HTMLInputElement>('[type="checkbox"]')!.click());
  expect(JSON.parse(fetcher.mock.calls[3][1].body)).toEqual({ limit: 50, verdict: "block" });
  expect(container.querySelector<HTMLButtonElement>('[aria-label="返回上一页"]')?.disabled).toBe(true);
});
it("ignoresStaleFilterResponse", async () => {
  let finish!: (value: Response) => void;
  const fetcher = vi.fn().mockImplementationOnce(() => new Promise<Response>((resolve) => { finish = resolve; })).mockResolvedValueOnce(ok([]));
  vi.stubGlobal("fetch", fetcher); await act(async () => root.render(<OutboundReviewAudit />));
  await act(async () => container.querySelector<HTMLInputElement>('[type="checkbox"]')!.click());
  await act(async () => finish(ok([row])));
  expect(container.textContent).toContain("没有匹配的审查记录"); expect(container.textContent).not.toContain(row.reason);
});
it.each([403, 503])("rendersEmptyErrorAndForbiddenDistinctly %s", async (status) => {
  vi.stubGlobal("fetch", vi.fn().mockResolvedValue(Response.json({ status: "error", error: { code: "unavailable", message: "Cannot read reviews" } }, { status })));
  await act(async () => root.render(<OutboundReviewAudit />));
  expect(container.textContent).toContain(status === 403 ? "仅操作者" : "Cannot read reviews");
  expect(container.textContent).not.toContain("没有匹配");
});
it("linksOnlyTheRecordedClassifierAndKeepsLargeIdentifiersExact", async () => {
  const fetcher = vi.fn(async (url: string) => url.endsWith("/get")
    ? Response.json({ status: "ok", result: { record: { agent_id: "outbound-check-example" } } })
    : ok([{ ...row, record_id: "42", parent_record_id: "11", task_id: "task-example" }]));
  vi.stubGlobal("fetch", fetcher); await act(async () => root.render(<OutboundReviewAudit />)); await click("展开详情");
  expect(container.querySelector('a[href="#history/outbound-check-example?task=task-example"]')).not.toBeNull();
  expect(container.querySelector('a[href="#history/manager-example?task=task-example"]')).not.toBeNull();
  await act(async () => root.unmount()); root = createRoot(container);
  fetcher.mockImplementation(async () => ok([{ ...row, record_id: "9223372036854775807" }]));
  await act(async () => root.render(<OutboundReviewAudit />)); await click("展开详情");
  expect(container.textContent).toContain("9223372036854775807（超出现有运行接口支持的编号范围）");
  expect(container.querySelector('a[href^="#history"]')).toBeNull();
});
it("marksRetainedRowsAsNotRefreshedWhenReloadFails", async () => {
  const fetcher = vi.fn().mockResolvedValueOnce(ok([row])).mockResolvedValueOnce(Response.json({ status: "error", error: {
    code: "unavailable", message: "Read failed" } }, { status: 503 }));
  vi.stubGlobal("fetch", fetcher); await act(async () => root.render(<OutboundReviewAudit />)); await click("刷新");
  expect(container.textContent).toContain("以下数据未刷新"); expect(container.textContent).toContain(row.reason);
});
it("restoresFiltersAndCursorWhenReturningToTheAuditEntry", async () => {
  window.history.replaceState({ outboundReviewAudit: { task: "task-example", blocked: true, cursors: [undefined, "older-page"] } }, "");
  const fetcher = vi.fn(async (_url: string, _init: RequestInit) => ok([row])); vi.stubGlobal("fetch", fetcher);
  await act(async () => root.render(<OutboundReviewAudit />));
  expect(JSON.parse(String(fetcher.mock.calls[0][1]?.body))).toEqual({ task_id: "task-example", verdict: "block", cursor: "older-page", limit: 50 });
  expect(container.querySelector<HTMLInputElement>('[aria-label="任务 ID"]')?.value).toBe("task-example");
  expect(container.querySelector<HTMLInputElement>('[type="checkbox"]')?.checked).toBe(true);
  expect(container.querySelector<HTMLButtonElement>('[aria-label="返回上一页"]')?.disabled).toBe(false);
});
