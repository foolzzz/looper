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
  const initial = (await fetchGates({ limit: 2 })).next_cursor!;
  const through = (await fetchGates({ limit: 2, through: initial })).next_cursor!;
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

it.each([false, true])("retains the first unread gate when the loaded prefix disappears (new prefix: %s)", async (insert) => {
  let rows = [row("z"), row("A"), row("a"), row("Z")];
  const fetcher = mockList(() => rows);
  const first = await fetchGates({ limit: 2 });
  expect(first).toMatchObject({ gates: rows.slice(0, 2), total: 4, limit: 2, offset: 0, has_more: true });
  const requests = fetcher.mock.calls.map(([, init]) => JSON.parse(String(init?.body)));
  expect(JSON.parse(first.next_cursor!)).toMatchObject({ end: 2 });
  rows = [...(insert ? [row("new")] : []), ...rows.slice(2)];
  const refresh = await fetchGates({ limit: 200, through: first.next_cursor! });
  expect(refresh.gates.map(r => r.gate.step)).toEqual(insert ? ["new"] : []);
  expect(refresh.has_more).toBe(true);
  expect(requests).toEqual([{ limit: 3, offset: 0 }]);
  for (const cursor of [first.next_cursor!, refresh.next_cursor!]) {
    expect(cursor).toBeTruthy();
    expect((await fetchGates({ limit: 2, cursor })).gates.map(r => r.gate.step)).toEqual(["a", "Z"]);
  }
});

it.each(["no successor", "all removed", "reduced total"])("refreshes a closed window with %s", async (change) => {
  let rows = [row("z"), row("A"), ...(change === "no successor" ? [] : [row("a"), row("Z")])];
  mockList(() => rows);
  const first = await fetchGates({ limit: 2 });
  rows = change === "all removed" ? [] : change === "reduced total" ? [row("new"), rows[2]] : [row("new")];
  const refresh = await fetchGates({ limit: 2, through: first.next_cursor! });
  expect(refresh.gates.map(r => r.gate.step)).toEqual(change === "all removed" ? [] : ["new"]);
  expect(refresh.total).toBe(rows.length);
  expect(refresh.has_more).toBe(change === "reduced total");
  if (refresh.has_more) {
    const more = await fetchGates({ limit: 2, cursor: refresh.next_cursor! });
    expect(more.gates.map(r => r.gate.step)).toEqual(["a"]);
    expect(more.has_more).toBe(false);
  }
});

it("reports more when the successor exhausts the server response", async () => {
  const rows = [row("z"), row("A"), row("a")];
  mockList(() => rows);
  const first = await fetchGates({ limit: 2 });
  expect(first).toMatchObject({ gates: rows.slice(0, 2), total: 3, counts: { open: 3 }, has_more: true });
  expect((await fetchGates({ cursor: first.next_cursor! })).gates).toEqual(rows.slice(2));
});

it.each(["cursor", "through"] as const)("fails closed when every saved identity disappears during %s", async (mode) => {
  let rows = [row("z"), row("A"), row("a"), row("Z")];
  mockList(() => rows);
  const first = await fetchGates({ limit: 2 });
  // The unobserved tail cannot identify where the old window ended.
  rows = [row("new"), rows[3]];
  await expect(fetchGates({ [mode]: first.next_cursor! })).rejects.toThrow("The gate list changed");
});

it("keeps a surviving loaded boundary ahead of insertions in the unread suffix", async () => {
  let rows = [row("z"), row("A"), row("a"), row("Z")];
  mockList(() => rows);
  const first = await fetchGates({ limit: 2 });
  rows.splice(2, 0, row("inserted"));
  const refresh = await fetchGates({ limit: 200, through: first.next_cursor! });
  expect(refresh.gates.map(r => r.gate.step)).toEqual(["z", "A"]);
  const more = await fetchGates({ limit: 200, cursor: refresh.next_cursor! });
  expect(more.gates.map(r => r.gate.step)).toEqual(["inserted", "a", "Z"]);
});

it("retains an unread boundary across the 200-row server page and simultaneous insertions", async () => {
  let rows = Array.from({ length: 405 }, (_, n) => row(String(n)));
  const fetcher = mockList(() => rows);
  const first = await fetchGates({ limit: 2, offset: 199 });
  expect(first.gates).toEqual(rows.slice(199, 201));
  expect(fetcher.mock.calls.map(([, init]) => JSON.parse(String(init?.body))))
    .toEqual([{ limit: 200, offset: 0 }, { limit: 2, offset: 200 }]);
  rows = [row("new"), row("newer"), ...rows.slice(201)];
  const refresh = await fetchGates({ limit: 200, through: first.next_cursor! });
  expect(refresh.gates.map(r => r.gate.step)).toEqual(["new", "newer"]);
  const more = await fetchGates({ limit: 200, cursor: refresh.next_cursor! });
  expect(more.gates).toEqual(rows.slice(2, 202));
  const last = await fetchGates({ limit: 200, cursor: more.next_cursor! });
  expect(last.gates).toEqual(rows.slice(202));
  expect(last.has_more).toBe(false);
  expect(new Set([...refresh.gates, ...more.gates, ...last.gates].map(r => r.gate.step)).size).toBe(rows.length);
});

it("reads multiple offset pages and retains the dashboard offset wrapper", async () => {
  const rows = Array.from({ length: 205 }, (_, n) => row(String(n)));
  const fetcher = mockList(() => rows);
  expect(await fetchGates({ limit: 3, offset: 199 })).toMatchObject({ gates: rows.slice(199, 202), total: 205, limit: 3, offset: 199, has_more: true });
  expect(fetcher.mock.calls.map(([, init]) => JSON.parse(String(init?.body)).offset)).toEqual([0, 200]);
});

it.each(["insert", "duplicate", "empty", "error"].flatMap(change =>
  ["offset", "through"].map(mode => ({ change, mode })),
))("rejects partial $mode paging after $change", async ({ change, mode }) => {
  const rows = Array.from({ length: 201 }, (_, n) => row(String(n)));
  const fetcher = mockList(() => rows), original = fetcher.getMockImplementation()!;
  const through = (await fetchGates({ limit: 2 })).next_cursor!;
  fetcher.mockClear();
  fetcher.mockImplementationOnce(original).mockImplementationOnce(async () => Response.json(change === "error"
    ? { status: "error", error: { code: "unavailable", message: "Read failed" } }
    : { status: "ok", result: { total: change === "insert" ? 202 : 201,
      gates: change === "empty" ? [] : [rows[change === "duplicate" ? 0 : 200]], has_more: false } },
    { status: change === "error" ? 503 : 200 }));
  // Offset 199 crosses the server's 200-row page; saved-boundary reads reconstruct all pages.
  await expect(fetchGates({ limit: 2, ...(mode === "offset" ? { offset: 199 } : { through }) }))
    .rejects.toThrow(change === "error" ? "Read failed" : "The gate list changed");
  expect(fetcher.mock.calls.map(([, init]) => JSON.parse(String(init?.body)).offset)).toEqual([0, 200]);
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
