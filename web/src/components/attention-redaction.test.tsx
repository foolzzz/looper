// @vitest-environment happy-dom
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { WorkAttention } from "@/components/work-attention";
import { displayText } from "@/lib/attention-display";
import { attentionSettingsGet, fetchAttention, type AttentionSummary } from "@/lib/api-attention";
import { NeedsYouPage } from "@/pages/needs-you";

vi.mock("@/lib/api-attention", async (original) => ({
  ...await original<typeof import("@/lib/api-attention")>(),
  attentionSettingsGet: vi.fn(),
  fetchAttention: vi.fn(),
}));
(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

const REDACTED = "[redacted]";
const SAFE = ["partly signed in (no refresh token)", "token expired", "not signed in"];
const KEYS = [
  "refresh_token", "access_token", "api_key", "password", "passwd", "secret", "authorization", "cookie", "session",
  "bearer", "key", "token", "client-secret", "private_key", "credential",
];
const HEX = "0123456789abcdef".repeat(4);
const OPAQUE = `qW7${"nR2mK9pL4".repeat(5)}`;
const BASE64 = "c3ludGhldGljY3JlZGVudGlhbGZvcnRlc3Rpbmdvbmx5";
const JWT = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJzeW50aGV0aWMifQ.c2lnbmF0dXJl";
const CASES = [
  ...KEYS.flatMap((key) => ["=", ": "].map((sep) => ({
    name: `${key}${sep}`, input: `${key}${sep}synthetic_value`, expected: `${key}${sep}${REDACTED}`,
    secrets: ["synthetic_value"],
  }))),
  ...[
    ["failure prefix", "failed: refresh_token=rt_live_supersecretvalue", `failed: refresh_token=${REDACTED}`, "rt_live_supersecretvalue"],
    ["nested prefix", 'failed: reason: {"refresh_token":"rt_live_supersecretvalue"}', `failed: reason: {"refresh_token":${REDACTED}}`, "rt_live_supersecretvalue"],
    ["quoted key", '{"token":"tk_json_synthetic"}', `{"token":${REDACTED}}`, "tk_json_synthetic"],
    ["single quoted key", "{'access_token': 'tk_synthetic value'}", `{'access_token': ${REDACTED}}`, "tk_synthetic value"],
    ["quoted value", 'password="pw_quoted_secret_value with spaces"', `password=${REDACTED}`, "pw_quoted_secret_value"],
    ["escaped quote", String.raw`token: "tk_synthetic\"value"`, `token: ${REDACTED}`, "tk_synthetic"],
    ["single quoted value", "token: 'tk_single_quoted_value'", `token: ${REDACTED}`, "tk_single_quoted_value"],
    ["URL query", "https://example.test/login?token=tk_query_value&state=ok", `https://example.test/login?token=${REDACTED}&state=ok`, "tk_query_value"],
    ["multiple URL parameters", "?state=ok&access_token=tk_access_value&refresh_token=rt_refresh_value#done", `?state=ok&access_token=${REDACTED}&refresh_token=${REDACTED}#done`, "tk_access_value", "rt_refresh_value"],
    ["Bearer header", "Authorization: Bearer AbCdEfGh1234567890", `Authorization: ${REDACTED}`, "AbCdEfGh1234567890"],
    ["Basic header", "Proxy-Authorization: Basic dXNlcjpwYXNzZW9yZA", `Proxy-Authorization: ${REDACTED}`, "dXNlcjpwYXNzZW9yZA"],
    ["short Basic header", "Authorization: Basic dTpw", `Authorization: ${REDACTED}`, "dTpw"],
    ["quoted Basic header", 'authorization="Basic dTpw"', `authorization=${REDACTED}`, "dTpw"],
    ["standalone short Basic", "failed: Basic dTpw", `failed: ${REDACTED}`, "dTpw"],
    ["short Bearer", "failed: bearer x7", `failed: ${REDACTED}`, "x7"],
    ["known token prefix", "sk-live-ABCDEFGHIJKLMNOPQRSTUVWXYZ12", REDACTED, "sk-live-ABCDEFGHIJKLMNOPQRSTUVWXYZ12"],
    ["opaque", OPAQUE, REDACTED, OPAQUE],
    ["hex", HEX, REDACTED, HEX],
    ["uppercase hex", HEX.toUpperCase(), REDACTED, HEX.toUpperCase()],
    ["base64", BASE64, REDACTED, BASE64],
    ["padded base64", `${BASE64}==`, REDACTED, BASE64],
    ["JWT", JWT, REDACTED, JWT],
    ["adjacent assignments", "state: failed; token=tk_first, password=pw_second", `state: failed; token=${REDACTED}, password=${REDACTED}`, "tk_first", "pw_second"],
  ].map(([name, input, expected, ...secrets]) => ({ name, input, expected, secrets })),
  ...[...SAFE, "count=3; state: ok", "keyboard=connected; tokenizer: ready"].map((input) => ({
    name: input, input, expected: input, secrets: [],
  })),
];

function summary(input: string): AttentionSummary {
  const item = { title: "Synthetic diagnosis", context: "", since: null, href: null, ref: null };
  return {
    read_at: "2026-11-20T08:00:00Z", total: 3, sections: [
      { kind: "failure_holds", state: "ok", count: 1, error: null,
        items: [{ ...item, context: input, key: "failure:1", facts: { streak: 2 } }] },
      { kind: "deliveries", state: "ok", count: 1, error: null,
        items: [{ ...item, key: "delivery:1", facts: { source: "subscription", fix: input } }] },
      { kind: "logins", state: "ok", count: 1, error: null,
        items: [{ ...item, key: "login:synthetic", facts: { detail: input, fix: input } }] },
    ],
  };
}

function detail(host: HTMLElement, section: string): string | null | undefined {
  return host.querySelector(`section[aria-label="${section}"] li .text-xs`)?.textContent;
}

let workRoot: Root;
let needsRoot: Root;
let workHost: HTMLElement;
let needsHost: HTMLElement;

beforeEach(() => {
  vi.mocked(attentionSettingsGet).mockResolvedValue({ enabled: true });
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
  it.each(CASES)("renders the same safe diagnosis for $name", async ({ input, expected, secrets }) => {
    expect(displayText(input)).toBe(expected);
    vi.mocked(fetchAttention).mockResolvedValue(summary(input));
    await act(async () => {
      workRoot.render(<WorkAttention />);
      needsRoot.render(<NeedsYouPage />);
    });
    for (const { host, deliveryTitle, deliverySource } of [
      { host: workHost, deliveryTitle: "可能未送达的消息", deliverySource: "订阅推送" },
      { host: needsHost, deliveryTitle: "Messages that may not have arrived", deliverySource: "Subscription push" },
    ]) {
      expect(detail(host, "运行失败等你处理")).toBe(`${expected} · 连续失败 2 次`);
      expect(detail(host, deliveryTitle)).toBe(`${deliverySource} · ${expected}`);
      for (const secret of secrets) {
        expect(host.textContent).not.toContain(secret);
        expect(host.innerHTML).not.toContain(secret);
      }
    }
    // Needs you leaves successful login reads to the startup banner; exercise the panel's detail/fix path too.
    expect(detail(workHost, "登录异常")).toBe(`${expected} · 处理方法：${expected}`);
  });
});
