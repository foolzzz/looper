import { afterEach, describe, expect, it, vi } from "vitest";
import { fetchChatTurn, sessionProfileLabel, stopChatReply } from "@/lib/api";

function stubFetch(status: number, body: unknown) {
  const fn = vi.fn(async () => new Response(JSON.stringify(body), { status }));
  vi.stubGlobal("fetch", fn);
  return fn;
}

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
  it("stopChatReply returns the state of a 409", async () => {
    const fn = stubFetch(409, { state: "not_running", error: "No turn is running." });
    expect(await stopChatReply("m1")).toEqual({ state: "not_running", method: null });
    expect(fn).toHaveBeenCalledWith("/api/chat/stop", expect.objectContaining({ method: "POST" }));
  });

  it("stopChatReply returns the state and method of a 200", async () => {
    stubFetch(200, { state: "stopping", method: "control" });
    expect(await stopChatReply("m1")).toEqual({ state: "stopping", method: "control" });
  });

  it("stopChatReply throws with the server's error", async () => {
    stubFetch(503, { error: "the Manager is not running" });
    await expect(stopChatReply("m1")).rejects.toThrow("the Manager is not running");
  });

  it("fetchChatTurn returns null when no turn runs", async () => {
    stubFetch(200, { turn: null });
    expect(await fetchChatTurn("s1")).toBeNull();
  });
});
