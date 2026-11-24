import { afterEach, expect, it, vi } from "vitest";
import { ledgerAction, ledgerAdd, ledgerGet, ledgerPage } from "@/lib/api-ledger";
import { V1Error } from "@/lib/v1";
afterEach(() => vi.unstubAllGlobals());
it("ledger writes use closed dashboard DTOs and preserve V1Error", async () => {
  const fetcher = vi.fn().mockImplementation(async () => new Response(JSON.stringify({ status: "ok", result: { item: { id: "L-0001" } } })));
  vi.stubGlobal("fetch", fetcher);
  expect(await ledgerAdd({ kind: "decision", title: "选择方案", due: "2026-11-30", quote: "确定" })).toEqual({ id: "L-0001" });
  expect(fetcher.mock.calls[0][0]).toBe("/api/v1/ledger/add");
  expect(JSON.parse(fetcher.mock.calls[0][1].body)).toEqual({ kind: "decision", title: "选择方案", due: "2026-11-30", quote: "确定" });
  await ledgerAction({ item: "L-0001", action: "amend", fields: { due: null, owner: null, project_id: null, detail: null } });
  expect(fetcher.mock.calls[1][0]).toBe("/api/v1/ledger/action");
  expect(JSON.parse(fetcher.mock.calls[1][1].body).fields).toEqual({ due: null, owner: null, project_id: null, detail: null });
  for (const status of [400, 403, 409]) {
    fetcher.mockResolvedValueOnce(new Response(JSON.stringify({ status: "error", error: { code: "conflict", message: "保留输入", field: "title" } }), { status }));
    await expect(ledgerAction({ item: "L-0001", action: "note", note: "说明" })).rejects.toMatchObject({ status, field: "title" });
  }
  fetcher.mockResolvedValueOnce(new Response(JSON.stringify({ status: "needs_confirmation", confirmation_id: "c", expires_in_seconds: 60, impact: "待确认" }), { status: 202 }));
  await expect(ledgerAdd({ kind: "pending", title: "事项" })).rejects.toBeInstanceOf(V1Error);
  fetcher.mockResolvedValueOnce(new Response(JSON.stringify({ status: "ok", result: { item: {}, events: [] } })));
  await ledgerGet("L-0001");
  expect(fetcher.mock.calls.at(-1)?.[0]).toBe("/api/v1/ledger/get");
  fetcher.mockResolvedValueOnce(new Response(JSON.stringify({ status: "ok", result: { items: [], next_cursor: null } })));
  await ledgerPage({ section: "all", status: "cancelled", cursor: "next", limit: 10 });
  expect(fetcher.mock.calls.at(-1)?.[0]).toBe("/api/v1/ledger/page");
  expect(JSON.parse(fetcher.mock.calls.at(-1)?.[1].body)).toEqual({ section: "all", status: "cancelled", cursor: "next", limit: 10 });
});
