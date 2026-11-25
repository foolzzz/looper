// @vitest-environment happy-dom
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { WorkAttention } from "@/components/work-attention";
import { redactDisplay } from "@/lib/attention-display";
import { attentionSettingsGet, fetchAttention, type AttentionSummary } from "@/lib/api-attention";
import { NeedsYouPage } from "@/pages/needs-you";

vi.mock("@/lib/api-attention", async (original) => ({
  ...await original<typeof import("@/lib/api-attention")>(),
  attentionSettingsGet: vi.fn(),
  fetchAttention: vi.fn(),
}));
vi.mock("@/lib/v1", async (original) => ({
  ...await original<typeof import("@/lib/v1")>(),
  callV1: vi.fn(),
  confirmV1: vi.fn(),
}));
(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

const SAFE = "partly signed in (no refresh token)";
const OPAQUE = `qW7${"nR2mK9pL4".repeat(5)}`;
/** Each secret is distinct so one masked form cannot hide another. */
const SECRETS = [
  "rt_live_supersecretvalue",
  "ak_live_colonsecretvalue",
  "pw_live_passwordvalue1",
  "pw_live_passwdvalue12",
  "sec_live_secretvalue1",
  "auth_live_headervalue",
  "bear_live_keyvalue123",
  "ck_live_cookievalue12",
  "sess_live_sessionvalue",
  "ky_live_barekeyvalue1",
  "tk_live_tokensecret12",
  "AbCdEfGh1234567890",
  "dXNlcjpwYXNzZW9yZA",
  "pw_quoted_secret_value",
  "tk_single_quoted_value",
  "sk-live-ABCDEFGHIJKLMNOPQRSTUVWXYZ12",
  OPAQUE,
];
const DIAGNOSIS = [
  SAFE,
  "refresh_token=rt_live_supersecretvalue",
  "api_key: ak_live_colonsecretvalue",
  "password=pw_live_passwordvalue1",
  "passwd: pw_live_passwdvalue12",
  "secret=sec_live_secretvalue1",
  "authorization: auth_live_headervalue",
  "bearer=bear_live_keyvalue123",
  "cookie=ck_live_cookievalue12",
  "session: sess_live_sessionvalue",
  "key=ky_live_barekeyvalue1",
  "token=tk_live_tokensecret12",
  "Authorization: Bearer AbCdEfGh1234567890",
  "Proxy-Authorization: Basic dXNlcjpwYXNzZW9yZA",
  'password="pw_quoted_secret_value"',
  "token: 'tk_single_quoted_value'",
  "sk-live-ABCDEFGHIJKLMNOPQRSTUVWXYZ12",
  OPAQUE,
].join(" | ");

const summary: AttentionSummary = {
  read_at: "2026-11-20T08:00:00Z",
  total: 2,
  sections: [
    {
      kind: "failure_holds", state: "ok", count: 1, error: null,
      items: [{
        key: "failure:1", title: "Held task", context: DIAGNOSIS, since: null, href: null, ref: null,
        facts: { streak: 2 },
      }],
    },
    {
      kind: "logins", state: "ok", count: 1, error: null,
      items: [{
        key: "login:cursor", title: "Cursor login", context: "", since: null, href: "#settings/runtimes", ref: null,
        facts: { detail: DIAGNOSIS, fix: DIAGNOSIS },
      }],
    },
  ],
};

function line(host: HTMLElement): string {
  const hits = [...host.querySelectorAll("p, span")]
    .map((node) => node.textContent ?? "")
    .filter((text) => text.includes(SAFE) && text.includes("连续失败"));
  return hits.sort((a, b) => a.length - b.length)[0] ?? "";
}

let workRoot: Root;
let needsRoot: Root;
let workHost: HTMLElement;
let needsHost: HTMLElement;

beforeEach(() => {
  vi.mocked(attentionSettingsGet).mockResolvedValue({ enabled: true });
  vi.mocked(fetchAttention).mockResolvedValue(summary);
  workHost = document.createElement("div");
  needsHost = document.createElement("div");
  document.body.append(workHost, needsHost);
  workRoot = createRoot(workHost);
  needsRoot = createRoot(needsHost);
});

afterEach(() => {
  act(() => { workRoot.unmount(); needsRoot.unmount(); });
  workHost.remove();
  needsHost.remove();
  vi.clearAllMocks();
});

describe("attention display redaction", () => {
  it("masks each credential form and keeps a diagnosis that only mentions a token", () => {
    expect(OPAQUE.length).toBeGreaterThanOrEqual(40);
    const masked = redactDisplay(DIAGNOSIS);
    expect(masked).toContain(SAFE);
    expect(masked).toContain("refresh_token=[redacted]");
    expect(masked).toContain("api_key: [redacted]");
    expect(masked).toContain("password=[redacted]");
    expect(masked).toContain("passwd: [redacted]");
    expect(masked).toContain("Authorization: [redacted]");
    expect(masked).toContain("Proxy-Authorization: [redacted]");
    for (const secret of SECRETS) expect(masked).not.toContain(secret);
    expect(redactDisplay(SAFE)).toBe(SAFE);
    expect(redactDisplay("count=3; state: ok")).toBe("count=3; state: ok");
  });

  it("renders the same masked text on the Needs you page and the work panel", async () => {
    await act(async () => {
      workRoot.render(<WorkAttention />);
      needsRoot.render(<NeedsYouPage />);
    });
    const workLine = line(workHost);
    const needsLine = line(needsHost);
    expect(workLine).toContain(SAFE);
    expect(needsLine).toContain(SAFE);
    expect(workLine).toBe(needsLine);
    expect(workHost.textContent).toContain("To fix:");
    for (const host of [workHost, needsHost]) {
      for (const secret of SECRETS) {
        expect(host.textContent).not.toContain(secret);
        expect(host.innerHTML).not.toContain(secret);
      }
    }
  });
});
