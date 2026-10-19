import { afterEach, describe, expect, it, vi } from "vitest";
import { fetchLarkStatus, searchLarkPeople } from "@/lib/api";

afterEach(() => vi.unstubAllGlobals());

describe("Lark channel APIs", () => {
  it.each([undefined, "lark_bot"])("reads status for %s (default: lark)", async (channelType) => {
    const status = { bot: null, problem: null, allowlist_app_id: null, names: {}, rejected: [] };
    const fetch = vi.fn().mockResolvedValue(Response.json(status));
    vi.stubGlobal("fetch", fetch);
    expect(await fetchLarkStatus(channelType)).toEqual(status);
    expect(fetch.mock.calls[0][0]).toBe(`/api/channels/${channelType ?? "lark"}/status`);
  });

  it.each([
    [undefined, undefined, true],
    ["lark_bot", true, true],
    ["lark_bot", false, false],
  ] as const)("searches %s and preserves completeness %s", async (channelType, complete, expected) => {
    const people = [{ open_id: "ou_example123", name: "Example", email: "example@example.com", department: "" }];
    const fetch = vi.fn().mockResolvedValue(Response.json({ people, ...(complete === undefined ? {} : { complete }) }));
    vi.stubGlobal("fetch", fetch);
    expect(await searchLarkPeople("a+b@example.com", channelType)).toEqual({ people, complete: expected });
    expect(fetch.mock.calls[0][0]).toBe(`/api/channels/${channelType ?? "lark"}/people?q=a%2Bb%40example.com`);
  });
});
