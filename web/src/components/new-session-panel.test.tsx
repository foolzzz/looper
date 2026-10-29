import { renderToStaticMarkup } from "react-dom/server";
import { afterEach, describe, expect, it, vi } from "vitest";
import { NewSessionPanel } from "@/components/new-session-panel";
import { usedByPlaces } from "@/components/agent-profiles";
import { sourceName } from "@/pages/agent-history";
import { sendChat, type ManagerProfileChoice } from "@/lib/api";

const choice = (n: number, id: string, problem: string | null = null): ManagerProfileChoice => ({
  number: n, profile_id: id, name: id, runtime: "claude-code", provider: "p", model: "m", effect: "new_session", problem,
});

describe("NewSessionPanel", () => {
  it("renders the Settings option first, the choices and both buttons", () => {
    const html = renderToStaticMarkup(
      <NewSessionPanel settingsName="claude-code/opus" choices={[choice(1, "agent-a"), choice(2, "agent-b", "no login")]}
        onStart={() => {}} onCancel={() => {}} />,
    );
    expect(html).toContain("New Manager session");
    expect(html.indexOf("Follow Settings (claude-code/opus)")).toBeGreaterThan(-1);
    expect(html.indexOf("Follow Settings (claude-code/opus)")).toBeLessThan(html.indexOf("agent-a"));
    expect(html).toMatch(/<option value="agent-b" disabled="">[^<]*no login/);
    expect(html).toContain("Start new session");
    expect(html).toContain("Cancel");
  });
});

describe("sendChat", () => {
  afterEach(() => vi.unstubAllGlobals());
  const body = async (options?: { newSessionProfileId?: string }) => {
    const fetchMock = vi.fn().mockResolvedValue({ ok: true, json: async () => ({ status: "ok", result: { correlation_id: "new-session-reply" } }) });
    vi.stubGlobal("fetch", fetchMock);
    await sendChat("/new", "s", "m", { kind: "manager" }, options);
    return JSON.parse(fetchMock.mock.calls[0][1].body);
  };
  it("posts new_session_profile_id only when given", async () => {
    expect((await body({ newSessionProfileId: "agent-x" })).new_session_profile_id).toBe("agent-x");
    expect(await body()).not.toHaveProperty("new_session_profile_id");
  });
});

describe("session source", () => {
  it("labels a run whose profile was chosen for the session", () => {
    expect(sourceName({ code: "session", label: null })).toContain("chosen for this session");
  });
});

describe("usedByPlaces", () => {
  it("includes the Manager's current session", () => {
    const places = usedByPlaces({ layers: [], task_count: 0, schedule_count: 0, allowed_in: [], manager_session: true });
    expect(places).toContainEqual({ text: "the Manager's current session", href: "#chat" });
  });
});
