import { afterEach, describe, expect, it, vi } from "vitest";
import { fetchGateSettings, fetchLarkStatus, searchLarkPeople, updateGateSettings } from "@/lib/api";
import { V1Error } from "@/lib/v1";

afterEach(() => vi.unstubAllGlobals());

const ok = (result: unknown) => Response.json({ status: "ok", result });
const requestBody = (fetch: ReturnType<typeof vi.fn>) => JSON.parse(fetch.mock.calls[0][1].body);

describe("Lark channel APIs", () => {
  it.each([
    [undefined, undefined], ["lark", "channel-lark"], ["lark", "channel-lark-5"],
    ["lark_bot", undefined], ["lark_bot", "channel-lark_bot"], ["lark_bot", "channel-lark_bot-5"],
  ])("reads %s status preserving channel id %s", async (channelType, channelId) => {
    const status = { bot: null, problem: "Bot unavailable", allowlist_app_id: null, names: {}, rejected: [] };
    const fetch = vi.fn().mockResolvedValue(ok(status));
    vi.stubGlobal("fetch", fetch);
    expect(await fetchLarkStatus(channelType, channelId)).toEqual(status);
    expect(fetch.mock.calls[0][0]).toBe("/api/v1/channels/status");
    expect(fetch.mock.calls[0][1].method).toBe("POST");
    expect(requestBody(fetch)).toEqual({ channel_type: channelType ?? "lark", ...(channelId ? { channel_id: channelId } : {}) });
  });

  it.each([true, false])("keeps the query and completeness %s", async (complete) => {
    const people = [{ open_id: "ou_example123", name: "Example", email: "example@example.com", department: "Support" }];
    const fetch = vi.fn().mockResolvedValue(ok({ people, complete }));
    vi.stubGlobal("fetch", fetch);
    expect(await searchLarkPeople("a+b@example.com", "lark_bot", "channel-lark_bot-5")).toEqual({ people, complete });
    expect(fetch.mock.calls[0][0]).toBe("/api/v1/channels/people");
    expect(requestBody(fetch)).toEqual({ channel_type: "lark_bot", channel_id: "channel-lark_bot-5", q: "a+b@example.com" });
  });

  it("surfaces typed lookup failures", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(Response.json({ status: "error", error: {
      code: "unavailable", message: "Directory lookup unavailable",
    } }, { status: 503 })));
    await expect(searchLarkPeople("Example")).rejects.toMatchObject({ name: "V1Error", code: "unavailable", status: 503 });
  });
});

describe("Gate notification settings APIs", () => {
  const settings = { dashboard_target: null, dashboard_target_problem: null, targets: [] };

  it("reads settings using the ordinary v1 operation", async () => {
    const fetch = vi.fn().mockResolvedValue(ok(settings));
    vi.stubGlobal("fetch", fetch);
    expect(await fetchGateSettings()).toEqual(settings);
    expect(fetch.mock.calls[0][0]).toBe("/api/v1/gate_settings/get");
    expect(requestBody(fetch)).toEqual({});
  });

  it.each([null, { channel: "lark_bot" as const, chat: "ou_example123" }])("saves target %s in one operator request", async (target) => {
    const saved = { ...settings, dashboard_target: target };
    const fetch = vi.fn().mockResolvedValue(ok(saved));
    vi.stubGlobal("fetch", fetch);
    expect(await updateGateSettings(target)).toEqual(saved);
    expect(fetch).toHaveBeenCalledTimes(1);
    expect(fetch.mock.calls[0][0]).toBe("/api/v1/gate_settings/update");
    expect(requestBody(fetch)).toEqual({ dashboard_target: target });
  });

  it("does not treat a pending confirmation as saved or auto-confirm it", async () => {
    const fetch = vi.fn().mockResolvedValue(Response.json({ status: "needs_confirmation", confirmation_id: "confirmation-example", expires_in_seconds: 60, impact: "Change target" }, { status: 202 }));
    vi.stubGlobal("fetch", fetch);
    await expect(updateGateSettings(null)).rejects.toBeInstanceOf(V1Error);
    expect(fetch).toHaveBeenCalledTimes(1);
  });
});
