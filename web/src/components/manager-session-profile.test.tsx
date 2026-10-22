import { renderToStaticMarkup } from "react-dom/server";
import { afterEach, describe, expect, it, vi } from "vitest";
import {
  ManagerSessionProfileView,
  confirmText,
  sourceText,
} from "@/components/manager-session-profile";
import { setManagerSessionProfile, type ManagerProfileChoice, type ManagerSessionView } from "@/lib/api";

afterEach(() => vi.unstubAllGlobals());

const choice = (over: Partial<ManagerProfileChoice> = {}): ManagerProfileChoice => ({
  number: 1, profile_id: "p1", name: "Alpha", runtime: "claude-code", provider: "anthropic", model: "m",
  effect: "current", problem: null, ...over,
});

const view = (over: Partial<ManagerSessionView> = {}): ManagerSessionView => ({
  session: { active: true, runtime: "claude-code", profile_id: "p1" },
  selected: null,
  effective: {
    profile: {
      profile_id: "p1", name: "Effective Agent", effort: null, runtime: "claude-code", provider: "anthropic", model: "opus",
    },
    source: "session",
  },
  problem: null,
  follow_settings: { name: "Settings Agent", effect: "new_session" },
  choices: [choice(), choice({ number: 2, profile_id: "p2", name: "Beta", problem: "runtime missing" })],
  ...over,
});

describe("manager session profile", () => {
  it.each([["session", "this session"], ["role", "Settings"], ["system", "Settings"], ["builtin", "Settings"]] as const)(
    "sourceText %s", (source, text) => {
      expect(sourceText({ effective: { profile: null, source } })).toBe(text);
    });

  it("confirms only for new_session", () => {
    expect(confirmText(choice({ effect: "new_session" }))).toContain("starts a new session on Alpha");
    for (const effect of ["current", "resumes", "starts"] as const) expect(confirmText(choice({ effect }))).toBeNull();
  });

  it("confirms following Settings when it starts a new session", () => {
    expect(confirmText(view().follow_settings!)).toContain("new session on Settings Agent");
  });

  it("renders the profile, options and problems", () => {
    const html = renderToStaticMarkup(
      <ManagerSessionProfileView view={view({ problem: "settings broken" })} busy={false} error={null} onSelect={() => {}} />,
    );
    expect(html).toContain("Effective Agent — claude-code · anthropic / opus");
    expect(html).not.toContain("undefined");
    expect(html).toContain("this session");
    expect(html).toContain("Follow Settings");
    expect(html).toContain("1. Alpha");
    expect(html).toMatch(/<option[^>]*disabled[^>]*>2\. Beta \(runtime missing\)/);
    expect(html).toContain("settings broken");
  });

  it("sends a null profile_id to follow Settings", async () => {
    const fetch = vi.fn().mockResolvedValue(Response.json({ ...view(), effect: "current", text: "ok" }));
    vi.stubGlobal("fetch", fetch);
    await setManagerSessionProfile(null);
    expect(fetch.mock.calls[0][0]).toBe("/api/manager/session/profile");
    expect(fetch.mock.calls[0][1].method).toBe("PUT");
    expect(JSON.parse(fetch.mock.calls[0][1].body)).toEqual({ profile_id: null });
  });
});
