import { afterEach, describe, expect, it, vi } from "vitest";
import {
  fetchChatSettings, fetchChatTurn, fetchManagerSession, sessionProfileLabel, setManagerSessionProfile,
  stopChatReply, updateChatSettings,
} from "@/lib/api";

function stubFetch(status: number, body: unknown) {
  const fn = vi.fn(async () => Response.json(body, { status }));
  vi.stubGlobal("fetch", fn);
  return fn;
}
const ok = (result: unknown) => ({ status: "ok", result });

afterEach(() => vi.unstubAllGlobals());

it.each([
  ["high", "Fast — codex · OpenAI / gpt · high"],
  [null, "Fast — codex · OpenAI / gpt"],
])("shows the session profile with effort %s", (effort, label) => {
  expect(sessionProfileLabel({
    profile_id: "fast", name: "Fast", runtime: "codex", provider: "OpenAI", model: "gpt", effort,
  })).toBe(label);
});

describe("chat stop and turn", () => {
  it.each(["disabled", "not_running", "other_message", "cannot_interrupt"])(
    "resolves expected stop conflict %s from typed details", async (state) => {
      const fn = stubFetch(409, { status: "error", error: { code: "conflict", message: "A localized reason.", details: { state } } });
      expect(await stopChatReply("m1")).toEqual({ state, method: null });
      expect(fn).toHaveBeenCalledWith("/api/v1/operator_chat/stop", expect.objectContaining({ method: "POST", body: '{"message_id":"m1"}' }));
    });

  it.each(["stopping", "already_stopping"])("returns stop result %s", async (state) => {
    stubFetch(200, ok({ state, phase: "running", method: "control" }));
    expect(await stopChatReply("m1")).toEqual({ state, method: "control" });
  });

  it.each([
    [503, { code: "unavailable", message: "the Manager is not running" }],
    [409, { code: "conflict", message: "not_running" }],
  ])("throws errors without expected conflict details (%s)", async (status, error) => {
    stubFetch(status, { status: "error", error });
    await expect(stopChatReply("m1")).rejects.toThrow(error.message);
  });

  it.each([null, { message_id: "m1", client_message_id: "c1", channel: "web", started_at: "now", state: "running",
    method: null, stopping: false, can_interrupt: true }])("returns the exact turn snapshot %j", async (turn) => {
    const fn = stubFetch(200, ok({ turn }));
    expect(await fetchChatTurn("s1")).toEqual(turn);
    expect(fn).toHaveBeenCalledWith("/api/v1/operator_chat/turn", expect.objectContaining({ method: "POST", body: '{"session_id":"s1"}' }));
  });
});

it.each([null, true, false])("updates stop setting %s through v1", async (stop_enabled) => {
  const fn = stubFetch(200, ok({ stop_enabled: stop_enabled ?? false }));
  expect(await updateChatSettings({ stop_enabled })).toEqual({ stop_enabled: stop_enabled ?? false });
  expect(fn).toHaveBeenCalledWith("/api/v1/chat_settings/update", expect.objectContaining({ method: "POST", body: JSON.stringify({ stop_enabled }) }));
});

it("reads chat settings through v1", async () => {
  const fn = stubFetch(200, ok({ stop_enabled: false }));
  expect(await fetchChatSettings()).toEqual({ stop_enabled: false });
  expect(fn).toHaveBeenCalledWith("/api/v1/chat_settings/get", expect.objectContaining({ body: "{}" }));
});

it("forwards the manager and abort signal for a session read", async () => {
  const view = { session: { active: true, runtime: "codex", profile_id: "p1" }, choices: [] };
  const fn = stubFetch(200, ok(view));
  const signal = new AbortController().signal;
  expect(await fetchManagerSession("manager-scenario", signal)).toEqual(view);
  expect(fn).toHaveBeenCalledWith("/api/v1/manager_session/get", expect.objectContaining({ body: '{"manager":"manager-scenario"}', signal }));
});

it("selects a profile for the requested manager", async () => {
  const selected = { selected: { profile_id: "p1", name: "Fast" }, effect: "resumes", text: "selected" };
  const fn = stubFetch(200, ok(selected));
  expect(await setManagerSessionProfile("p1", "manager-scenario")).toEqual(selected);
  expect(fn).toHaveBeenCalledWith("/api/v1/manager_session/select_profile", expect.objectContaining({ body: '{"profile_id":"p1","manager":"manager-scenario"}' }));
});

it("does not report a pending chat setting update as completed or auto-confirm it", async () => {
  const fn = stubFetch(202, { status: "needs_confirmation", confirmation_id: "pending", impact: "Enable Stop for all chats.", expires_in_seconds: 60 });
  await expect(updateChatSettings({ stop_enabled: true })).rejects.toThrow("Enable Stop for all chats.");
  expect(fn).toHaveBeenCalledTimes(1);
});
