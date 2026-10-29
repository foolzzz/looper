// @vitest-environment happy-dom
import { afterEach, expect, it, vi } from "vitest";
import { type LlmRecord, fetchTaskRuns, fetchLlmRecords, fetchLlmRecord, fetchLlmRecordEvents, fetchAgentUsage } from "@/lib/api";
import { runCostLabel } from "@/lib/usage";
import { installSessionGuard, resetSessionGuard, SessionEndedError } from "@/lib/session";

afterEach(() => { resetSessionGuard(); vi.unstubAllGlobals(); });

it("reads runs, records, incremental events and usage through v1 with cancellation", async () => {
  const row = { id: 3, status: "interrupted", metadata: { historical: true } };
  const events = [{ seq: 9, event: { type: "assistant", extra: [null, 1] } }];
  const usage = { agent_id: "planner-example", latest_context_tokens: null };
  const fetcher = vi.fn();
  for (const result of [{ runs: [row] }, { records: [row] }, { record: row }, { events }, usage]) {
    fetcher.mockResolvedValueOnce(Response.json({ status: "ok", result }));
  }
  vi.stubGlobal("fetch", fetcher);
  const { signal } = new AbortController();
  expect(await fetchTaskRuns("task-example", signal)).toEqual([row]);
  expect(await fetchLlmRecords({ task_id: "task-example", role: "worker", limit: 1, offset: 0 })).toEqual([row]);
  expect(await fetchLlmRecord(3)).toEqual(row);
  expect(await fetchLlmRecordEvents(3, 8)).toEqual(events);
  expect(await fetchAgentUsage("planner-example", 100, signal)).toEqual(usage);
  expect(fetcher.mock.calls.map(([url, init]) => [url, init.method, JSON.parse(init.body)])).toEqual([
    ["/api/v1/runs/for_task", "POST", { task_id: "task-example" }],
    ["/api/v1/runs/list", "POST", { task_id: "task-example", role: "worker", limit: 1, offset: 0 }],
    ["/api/v1/runs/get", "POST", { record_id: 3 }],
    ["/api/v1/runs/events", "POST", { record_id: 3, after: 8 }],
    ["/api/v1/runs/usage", "POST", { agent_id: "planner-example", turns: 100 }],
  ]);
  expect(fetcher.mock.calls[0][1].signal).toBe(signal);
  expect(fetcher.mock.calls[4][1].signal).toBe(signal);
});

it("preserves typed failures, aborts and ended-session handling", async () => {
  const fetcher = vi.fn().mockResolvedValueOnce(Response.json({ status: "error", error: {
    code: "unavailable", message: "Database unavailable" } }, { status: 503 }))
    .mockRejectedValueOnce(new DOMException("Aborted", "AbortError"));
  vi.stubGlobal("fetch", fetcher);
  await expect(fetchLlmRecords({})).rejects.toMatchObject({ code: "unavailable" });
  await expect(fetchAgentUsage("manager", 100, AbortSignal.abort())).rejects.toMatchObject({ name: "AbortError" });
  fetcher.mockResolvedValueOnce(new Response(null, { status: 401 }));
  installSessionGuard();
  await expect(fetchLlmRecordEvents(3)).rejects.toBeInstanceOf(SessionEndedError);
  await expect(fetchTaskRuns("task-example")).rejects.toBeInstanceOf(SessionEndedError);
  expect(fetcher).toHaveBeenCalledTimes(3);
});

it.each([
  [{}, ""], [{ accounting_basis: "subscription_equivalent" }, "equivalent"],
  [{ profile: { provider: { auth: "oauth" } } }, "equivalent"], [{ profile: { provider: { auth: "api_key" } } }, ""],
])("prices only explicit accounting evidence: %j", (metadata, expected) => {
  expect(runCostLabel({ metadata } as LlmRecord)).toBe(expected);
});
