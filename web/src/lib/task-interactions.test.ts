// @vitest-environment happy-dom
import { afterEach, expect, it, vi } from "vitest";
import { fetchGates, fetchTaskRoom, resumeWait, type GateList, type GateListRow, type UserGate } from "@/lib/api";

afterEach(() => vi.unstubAllGlobals());

const row = (step: string): GateListRow => ({
  task: { task_id: `task-${step}`, name: step, status: "waiting_user", group: "needs_you", project_id: "project", project_name: "Project" },
  gate: { step, step_id: step, state: "open", topic: step, opened_at: "2026-10-20T10:00:00Z" } as UserGate,
  agenda_open: 1, last_message: null,
});
function mockList(current: () => GateListRow[]) {
  const fetcher = vi.fn(async (_path: string, init?: RequestInit) => {
    const { limit, offset } = JSON.parse(String(init?.body)), rows = current();
    return Response.json({ status: "ok", result: { gates: rows.slice(offset, offset + limit), total: rows.length,
      counts: { open: rows.length }, limit, offset, has_more: offset + limit < rows.length } as GateList });
  });
  vi.stubGlobal("fetch", fetcher);
  return fetcher;
}

it.each(["anchor", "prefix", "suffix", "unchanged"])("preserves server order and the old %s refresh boundary", async (removed) => {
  // Identical timestamps and non-lexical keys: the server owns the collation order.
  let rows = [row("z"), row("A"), row("a"), row("Z")];
  mockList(() => rows);
  const through = (await fetchGates({ limit: 2 })).next_cursor!;
  if (removed === "anchor") rows.splice(1, 1);
  if (removed === "prefix") rows.splice(0, 2);
  if (removed === "suffix") rows.splice(2, 2);
  rows.unshift(row("new"));
  const refresh = await fetchGates({ limit: 200, through });
  expect(refresh.gates.map(r => r.gate.step)).toEqual(removed === "prefix" ? ["new"] : removed === "anchor" ? ["new", "z"] : ["new", "z", "A"]);
  expect((await fetchGates({ limit: 2, cursor: through })).gates.map(r => r.gate.step)).toEqual(removed === "suffix" ? [] : ["a", "Z"]);
  rows = [row("replacement")];
  await expect(fetchGates({ cursor: through })).rejects.toThrow("The gate list changed");
});

it("reads multiple offset pages and retains the dashboard offset wrapper", async () => {
  const rows = Array.from({ length: 205 }, (_, n) => row(String(n)));
  const fetcher = mockList(() => rows);
  expect(await fetchGates({ limit: 3, offset: 199 })).toMatchObject({ gates: rows.slice(199, 202), total: 205, limit: 3, offset: 199, has_more: true });
  expect(fetcher.mock.calls.map(([, init]) => JSON.parse(String(init?.body)).offset)).toEqual([0, 200]);
});

it.each(["insert", "duplicate", "empty", "error"])("rejects partial paging after %s", async change => {
  const rows = Array.from({ length: 201 }, (_, n) => row(String(n)));
  const fetcher = mockList(() => rows), original = fetcher.getMockImplementation()!;
  fetcher.mockImplementationOnce(original).mockImplementationOnce(async () => Response.json(change === "error"
    ? { status: "error", error: { code: "unavailable", message: "Read failed" } }
    : { status: "ok", result: { total: change === "insert" ? 202 : 201, gates: change === "empty" ? [] : [rows[0]], has_more: false } },
    { status: change === "error" ? 503 : 200 }));
  await expect(fetchGates({ limit: 2 })).rejects.toThrow(change === "error" ? "Read failed" : "The gate list changed");
});

it("keeps complete room bodies, consults, filters and the cancellation signal", async () => {
  const result = { enabled: true, messages: [{ body: "Complete message\n".repeat(500) }], consults: { list: [{ state: "answered" }] } };
  const fetcher = vi.fn(async () => Response.json({ status: "ok", result }));
  vi.stubGlobal("fetch", fetcher);
  const signal = new AbortController().signal;
  expect(await fetchTaskRoom("task", { kind: "question", from: "worker", unanswered: true, after: 2, limit: 500, signal })).toEqual(result);
  expect(fetcher).toHaveBeenCalledWith("/api/v1/tasks/room_view", expect.objectContaining({ signal,
    body: JSON.stringify({ task_id: "task", kind: "question", from: "worker", unanswered: true, after: 2, limit: 500 }) }));
});

it("does not complete or confirm a pending dashboard action", async () => {
  const fetcher = vi.fn(async () => Response.json({ status: "needs_confirmation", confirmation_id: "pending",
    expires_in_seconds: 60, impact: "Resume the wait" }, { status: 202 }));
  vi.stubGlobal("fetch", fetcher);
  await expect(resumeWait("task", "wait")).rejects.toMatchObject({ status: 202, message: "Resume the wait" });
  expect(fetcher).toHaveBeenCalledOnce();
});
