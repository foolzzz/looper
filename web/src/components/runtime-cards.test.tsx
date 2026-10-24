// @vitest-environment happy-dom
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { RuntimesSection } from "@/components/runtime-cards";
import type { Registry, Runtime } from "@/lib/api";
import { LOGIN_HELP, RUNTIME_TYPES } from "@/lib/runtime-catalog";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

const INSTALL = "npm install -g --ignore-scripts @earendil-works/pi-coding-agent";
const ROLES = ["manager", "planner", "worker", "reviewer"] as const;
const OLD_VERSION = "1.2.0 from pi --version is older than 1.3.0, the oldest pi Looper supports; upgrade pi and probe again";
const MISSING_FLAGS = "pi --help does not show --mode; upgrade pi to 1.3.0 or newer and probe again";

function runtime(fields: Partial<Runtime>): Runtime {
  return {
    runtime_id: "rt-pi", name: "pi", type: "pi", type_label: "pi", executable: "", default_executable: "pi",
    path: "/usr/local/bin/pi", protocols: ["openai-chat", "openai-responses"], version: "1.3.0",
    capabilities: ["run", "resident_session", "resume", "steer", "read_tools", "compaction_events", "mcp"],
    roles: [...ROLES], probed_at: "2026-10-20T00:00:00Z", probe_error: null, builtin: true, profile_count: 1,
    ...fields,
  };
}

function registry(pi: Runtime | null): Registry {
  return {
    runtime_types: [{
      type: "pi", label: "pi", default_executable: "pi", runtime_id: "rt-pi",
      supported_models: null,
      protocols: ["openai-chat", "openai-responses"],
      capabilities: ["run", "resident_session", "resume", "steer", "read_tools", "compaction_events", "mcp"],
      roles: [...ROLES], auth_kinds: ["api_key"], can_replace_system_prompt: true, replace_system_prompt_refusal: null, can_interrupt: false,
    }],
    runtimes: pi ? [pi] : [],
    providers: [], models: [], profiles: [], vendor_types: [], prices: { as_of: "", sources: [], models: {} },
    limits: {
      name_max_chars: 100, models_max: 10, model_fields: [], price_fields: [], protocols: ["openai-chat", "openai-responses"],
      auth_kinds: ["api_key"], effort_levels: [], role_capabilities: { manager: [], planner: [], worker: [], reviewer: [] },
      secret_mask: "", provider_env_owned: [],
    },
  };
}

describe("pi on Agent Runtimes", () => {
  let container: HTMLDivElement;
  let root: Root;
  let body: Registry;
  let calls: { url: string; method: string }[];
  let probeError: string;

  beforeEach(() => {
    calls = [];
    probeError = "";
    body = registry(runtime({}));
    vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      const method = init?.method ?? "GET";
      calls.push({ url, method });
      if (url === "/api/v1/registry/view" && method === "POST") return Response.json({ status: "ok", result: body });
      if (url === "/api/v1/runtimes/probe" && method === "POST") {
        expect(JSON.parse(String(init?.body))).toEqual({ runtime_id: "rt-pi" });
        if (probeError) return Response.json({ status: "error", error: { code: "unavailable", message: probeError } }, { status: 503 });
        body = registry(runtime({}));
        return Response.json({ status: "ok", result: { runtime: body.runtimes[0] } });
      }
      throw new Error(`unexpected ${method} ${url}`);
    }));
    container = document.createElement("div");
    document.body.append(container);
    root = createRoot(container);
  });

  afterEach(() => {
    act(() => root.unmount());
    container.remove();
    vi.unstubAllGlobals();
  });

  const show = async () => {
    await act(async () => root.render(<RuntimesSection />));
  };
  const card = () => {
    const heading = [...container.querySelectorAll("h3")].find((node) => node.textContent === "Pi CLI");
    expect(heading, "Pi CLI card").toBeTruthy();
    return heading!.closest("li")!;
  };
  const fact = (node: HTMLElement, label: string) => {
    const term = [...node.querySelectorAll("dt")].find((item) => item.textContent === label);
    return term?.nextElementSibling?.textContent ?? "";
  };

  it("names pi, shows the install command, and has no login help", () => {
    expect(RUNTIME_TYPES.pi).toEqual({ kind: "CLI", title: "Pi CLI", install: INSTALL });
    expect(LOGIN_HELP).not.toHaveProperty("pi");
  });

  it("shows a detected pi with four roles and no login", async () => {
    await show();
    const node = card();
    expect(node.textContent).toContain("CLI");
    expect(node.textContent).toContain("Detected");
    expect(fact(node, "Version")).toContain("1.3.0");
    expect(fact(node, "Path")).toContain("/usr/local/bin/pi");
    expect(fact(node, "Roles").match(/: can run/g)).toHaveLength(4);
    expect(fact(node, "Roles")).not.toContain(": cannot run");
    expect(node.textContent).toContain("Found as pi on the server's PATH.");
    expect(node.textContent).not.toMatch(/login/i);
    expect([...node.querySelectorAll("button")].map((button) => button.textContent)).toEqual(["Re-detect"]);
  });

  it.each([
    ["1.2.0", OLD_VERSION],
    ["1.3.0", MISSING_FLAGS],
  ])("keeps the probe error for version %s when capabilities are unavailable", async (version, probeError) => {
    body = registry(runtime({ version, probe_error: probeError, capabilities: [], roles: [] }));
    await show();
    const node = card();
    expect(node.textContent).toContain("Not detected");
    expect(fact(node, "Version")).toContain(version);
    expect(fact(node, "Version")).toContain(probeError);
    expect(fact(node, "Capabilities")).toBe("none");
    expect(fact(node, "Roles").match(/: cannot run/g)).toHaveLength(4);
    expect(node.textContent).toContain(INSTALL);
    expect(node.textContent).not.toMatch(/login/i);
  });

  it("shows a re-detect failure and keeps the current runtime without reloading", async () => {
    await show();
    probeError = "The executable could not be probed";
    await act(async () => card().querySelector("button")!.click());
    expect(card().querySelector('[role="alert"]')?.textContent).toContain(probeError);
    expect(fact(card(), "Version")).toContain("1.3.0");
    expect(calls.filter(({ url }) => url === "/api/v1/registry/view")).toHaveLength(1);
    expect(card().querySelector("button")!.disabled).toBe(false);
  });

  it("re-detects pi and replaces the probe error", async () => {
    body = registry(runtime({ version: "1.2.0", probe_error: OLD_VERSION, capabilities: [], roles: [] }));
    await show();
    const probe = card().querySelector("button")!;
    expect(probe.getAttribute("aria-label")).toBe("Re-detect Pi CLI");
    await act(async () => probe.click());
    expect(calls).toContainEqual({ url: "/api/v1/runtimes/probe", method: "POST" });
    const node = card();
    expect(node.textContent).toContain("Detected");
    expect(fact(node, "Version")).toContain("1.3.0");
    expect(fact(node, "Version")).not.toContain(OLD_VERSION);
    expect(fact(node, "Roles").match(/: can run/g)).toHaveLength(4);
  });
});
