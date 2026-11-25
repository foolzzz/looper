// @vitest-environment happy-dom
import { act } from "react";
import { createRoot } from "react-dom/client";
import { afterEach, expect, it, vi } from "vitest";
import { ClaudeCheckBanner } from "@/components/claude-check-banner";
import { redactDisplay } from "@/lib/attention-display";
import cases from "../../../tests/fixtures/attention-redaction.json";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
afterEach(() => vi.unstubAllGlobals());

it.each(cases)("shares the server redaction rule for $name", ({ input, expected }) => {
  expect(redactDisplay(input)).toBe(expected);
  expect(redactDisplay(expected)).toBe(expected);
});

it.each(cases)("renders the server's safe detail and fix for $name", async ({ expected, secrets }) => {
  // Python's HTTP regression checks this exact response text against the raw input.
  vi.stubGlobal("fetch", vi.fn(async () => Response.json({ status: "ok", result: { checks: [
    { id: "claude_login", label: "Synthetic login", status: "fail", detail: expected, fix: expected },
  ] } })));
  const host = document.createElement("div");
  const root = createRoot(host);
  try {
    await act(async () => root.render(<ClaudeCheckBanner />));
    expect(host.querySelector('[role="alert"]')?.textContent).toContain(`Synthetic login: ${expected}.`);
    expect(host.textContent).toContain(`To fix: ${expected}`);
    for (const secret of secrets) {
      expect(host.innerHTML).not.toContain(secret);
    }
  } finally {
    act(() => root.unmount());
  }
});
