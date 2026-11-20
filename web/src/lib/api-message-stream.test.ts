// @vitest-environment happy-dom
import { afterEach, expect, it, vi } from "vitest";
import { deleteMessageStream, previewMessageStreamDeletion } from "@/lib/api";

// These calls use the real v1 request path: confirmation tokens are bound to its session header.
afterEach(() => vi.unstubAllGlobals());

it("keeps each deletion preview's session for confirmation without sharing it across actions", async () => {
  const calls: { path: string; session: string | null; body: Record<string, unknown> }[] = [];
  vi.stubGlobal("fetch", vi.fn(async (path: string, init: RequestInit) => {
    calls.push({ path, session: new Headers(init.headers).get("X-Looper-Session"), body: JSON.parse(String(init.body)) });
    return Response.json({ status: "ok", result: path.endsWith("delete_preview") ? {
      source_id: "demo-source", scope: "message", events: 1, confirmation_token: `token-${calls.length}`,
      backups: { count: 0, earliest_at: null }, limits: [],
    } : { source_id: "demo-source", deleted: 1, deletion_id: 1 } });
  }));
  const target = { source_id: "demo-source", scope: "message" as const, message_id: "message-demo" };
  const first = await previewMessageStreamDeletion(target);
  const second = await previewMessageStreamDeletion(target);
  await deleteMessageStream({ ...target, confirmation_token: first.confirmation_token }, first.session);
  await deleteMessageStream({ ...target, confirmation_token: second.confirmation_token }, second.session);

  expect(calls[0].session).toBeTruthy();
  expect(calls[2].session).toBe(calls[0].session);
  expect(calls[3].session).toBe(calls[1].session);
  expect(calls[0].session).not.toBe(calls[1].session);
  expect(calls[2].body).toEqual({ ...target, confirmation_token: first.confirmation_token });
  expect(calls[2].body).not.toHaveProperty("session");
});
