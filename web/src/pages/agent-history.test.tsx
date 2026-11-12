// @vitest-environment happy-dom
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { renderToStaticMarkup } from "react-dom/server";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { RunCard, sourceName } from "@/pages/agent-history";
import { exportLlmRequests, runProfile, type LlmRecord, type LlmRequest } from "@/lib/api";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

function render(mode?: "append" | "replace", metadata: object = {}, extra: object = {}) {
  const record = {
    id: 1, agent_id: "worker", status: "completed", created_at: "2026-10-18T10:00:00Z", duration_ms: 10,
    metadata: { presentation: { system_prompt: { text: "Looper prompt", mode: mode ?? null, sha256: null } }, ...metadata }, ...extra,
  } as LlmRecord;
  return renderToStaticMarkup(<RunCard initial={record} defaultOpen />);
}

describe("run system prompt mode", () => {
  it.each([{}, { input_tokens: 100, cache_read_tokens: 0, cache_creation_tokens: 0 }])(
    "hides cache hits while the run is still running", (usage) => {
      const html = render(undefined, {}, { status: "running", ...usage });
      expect(html).toContain("Waiting for the first event");
      expect(html).not.toContain("cache hit");
      expect(html).not.toContain("this runtime does not report cache usage");
    });

  it.each([
    [100, 80, 20, "40%"], [100, 0, 0, "0%"], [0, 0, 0, "–"], [100, null, 20, "–"], [100, undefined, undefined, "–"],
  ])("shows run cache hits from recorded prompt tokens", (input_tokens, cache_read_tokens, cache_creation_tokens, hit) => {
    const html = render(undefined, {}, { input_tokens, cache_read_tokens, cache_creation_tokens });
    expect(html).toContain(`cache hit ${hit}`);
    expect(html).toContain(hit === "–" && input_tokens !== 0
      ? "this runtime does not report cache usage" : "cache_read / (input + cache_read + cache_write)");
  });

  it("shows a replaced default badge with an explanation for replace runs", () => {
    const html = render("replace");
    expect(html).toContain("replaced default");
    expect(html).toContain('title="Looper replaced the runtime&#x27;s default system prompt for this run"');
    expect(html).not.toContain("appended by Looper");
  });

  it.each(["append", undefined] as const)("shows only the evidenced mode for %s runs", (mode) => {
    const html = render(mode);
    expect(html).toContain(mode ? "appended by Looper" : "mode unknown");
    expect(html).toContain("hash unknown");
    expect(html).not.toContain("replaced default");
  });
  it("warns when the recorded TTADK sign-in check was inconclusive", () => {
    const html = render(undefined, { ttadk_check: "timed out or failed; the run started anyway" });
    const host = document.createElement("div");
    host.innerHTML = html;
    const warning = [...host.querySelectorAll("p")].find((item) => item.textContent?.includes("TTADK sign-in check"))!;
    expect(warning.textContent).toBe("TTADK sign-in check before this run started: timed out or failed; the run started anyway");
    expect(warning.querySelector("svg")).not.toBeNull();
    expect(html).not.toContain("could not finish");
    expect(html).not.toContain("check_failed");
    expect(render()).not.toContain("TTADK sign-in check");
  });

  it("uses normalized sources and canonical Profiles without interpreting historical labels", () => {
    expect(sourceName({ code: null, label: "Manager override" })).toBe("Manager override");
    expect(sourceName({ code: "planner_step", label: null })).toBe("Planner's choice for this step");
    expect(sourceName({ code: null, label: null })).toBe("unknown");
    expect(runProfile({ metadata: { profile: { name: "Custom runtime" } } } as LlmRecord)?.name).toBe("Custom runtime");
  });
  it("keeps absent-transcript output and the current waiting state", () => {
    expect(render(undefined, {}, { output_text: "retained evidence" })).toContain("retained evidence");
    expect(render(undefined, {}, { status: "running" })).toContain("Waiting for the first event");
  });
  it("renders unknown library evidence and historical labels from the DTO", async () => {
    const host = document.createElement("div"), root = createRoot(host);
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(Response.json({ status: "ok", result: { events: [] } })));
    const record = { id: 1, status: "completed", created_at: "2026-10-18T10:00:00Z", metadata: {
      profile: { source: "project" },
      presentation: { sources: { profile: { code: null, label: "Manager override" } },
        system_prompt: { text: "stored evidence", mode: null, sha256: null },
        library: { items: [{ kind: "skill", name: "old", plugin: null, scope: null, project_id: null, sha256: null }],
          sha256: null, error: null, project_error: "unavailable" } },
    } } as LlmRecord;
    try {
      await act(async () => { root.render(<RunCard initial={record} defaultOpen />); });
      await act(async () => { for (const button of host.querySelectorAll("button")) {
        if (/System prompt|Plugins & skills/.test(button.textContent ?? "")) button.click();
      } });
      for (const value of ["unknown plugin:old", "Unknown scope or kind", "hash unknown", "Manager override", "mode unknown", "stored evidence", "unknown runtime", "unknown provider"])
        expect(host.textContent).toContain(value);
      expect(host.textContent).not.toContain("Skills (global)");
    } finally { await act(async () => root.unmount()); vi.unstubAllGlobals(); }
  });
});

function capturedRequest(id = 1, extra: Partial<LlmRequest> = {}): LlmRequest {
  return {
    id, route_id: "route-a", proxy_instance: "proxy-a", proxy_seq: id, record_id: 7, after_record_id: null,
    agent_id: "worker", task_id: "task-a", started_at: `2026-11-01T10:00:0${id}Z`, method: "POST", path: "/v1/messages",
    kind: "model", model: "recorded-model", stream: true, status: 200, outcome: "completed", error: null,
    ttfb_ms: 30, duration_ms: 120, request_headers: { authorization: "<redacted>", "content-type": "application/json" },
    response_headers: {}, request_bytes: 1200, response_bytes: 400, request_sha256: "request-hash",
    input_tokens: 100, cache_read_tokens: 80, cache_write_tokens: 20, output_tokens: 15,
    usage: { input_tokens: 100, output_tokens: 15 }, record_gap: null, body_sanitized: false,
    request_body: { model: "recorded-model", system: [{ type: "text", text: "Stored system instructions" }],
      tools: [{ name: "read_file", input_schema: { type: "object" } }],
      messages: [{ role: "user", content: [{ type: "text", text: "Earlier message" }] },
        { role: "assistant", content: [{ type: "thinking", thinking: "New reasoning" }] }] },
    response_body: { content: [{ type: "text", text: "Stored reply" }], stop_reason: "end_turn" },
    response_raw: null, new_from_index: 1,
    route: { id: "route-a", agent_id: "worker", task_id: "task-a", step_id: "step-a", role: "worker",
      runtime_type: "claude-code", provider_id: "provider-a", protocol: "anthropic-messages", model: "recorded-model",
      upstream_host: "api.example.test", upstream_path: "/v1/messages" }, ...extra,
  };
}

describe("recorded model requests", () => {
  let host: HTMLDivElement, root: Root;
  let fetcher: ReturnType<typeof vi.fn<typeof fetch>>;
  let row: LlmRequest;
  let latest: LlmRecord;
  const record = (metadata: object = { llmrec: { state: "recording" } }, status = "completed") => ({
    id: 7, agent_id: "worker", status, created_at: "2026-11-01T10:00:00Z", duration_ms: 120, metadata,
  } as LlmRecord);
  const ok = (result: object) => Response.json({ status: "ok", result });
  const button = (text: string) => {
    const match = [...host.querySelectorAll("button")].find((item) => item.textContent?.includes(text));
    expect(match, text).toBeTruthy();
    return match!;
  };
  const click = async (text: string) => act(async () => button(text).click());
  const show = async (initial = record()) => act(async () => root.render(<RunCard initial={initial} defaultOpen />));
  const listCalls = () => fetcher.mock.calls.filter(([url]) => String(url).endsWith("llm_requests/list"));

  beforeEach(() => {
    row = capturedRequest();
    latest = record();
    host = document.createElement("div");
    document.body.append(host);
    root = createRoot(host);
    fetcher = vi.fn<typeof fetch>(async (input, init) => {
      const url = String(input), body = JSON.parse(String(init?.body ?? "{}"));
      if (url.endsWith("runs/events")) return ok({ events: [] });
      if (url.endsWith("runs/get")) return ok({ record: latest });
      if (url.endsWith("llm_requests/list")) return ok({ requests: body.between_turns
        ? [capturedRequest(2, { model: "after-turn-model", record_id: null, after_record_id: 7 })] : [row] });
      if (url.endsWith("llm_requests/get")) return ok({ request: row });
      throw new Error(`Unexpected request: ${url}`);
    });
    vi.stubGlobal("fetch", fetcher);
  });
  afterEach(async () => {
    await act(async () => root.unmount());
    host.remove();
    vi.useRealTimers();
    vi.restoreAllMocks();
    vi.unstubAllGlobals();
  });

  it("hides the section when recording metadata is absent, and explains skipped recording", async () => {
    await show(record({}));
    expect(host.textContent).not.toContain("Model requests");
    expect(listCalls()).toHaveLength(0);
    // The run card owns its initial record; remount to represent another historical run.
    await act(async () => root.render(<RunCard key="skipped" initial={record({ llmrec: {
      state: "skipped", reason: "oauth_disabled", detail: "subscription" } })} defaultOpen />));
    expect(host.textContent).toContain("Requests not recorded: oauth_disabled (subscription)");
    expect(listCalls()).toHaveLength(0);
  });

  it("starts collapsed and shows rows, timing, token counts and requests after the turn in order", async () => {
    await show();
    expect(button("Model requests · 2").getAttribute("aria-expanded")).toBe("false");
    expect(host.textContent).not.toContain("after-turn-model");
    await click("Model requests · 2");
    for (const text of ["recorded-model", "200 · completed", "TTFB 30 ms", "Duration 120 ms", "100 / 80 / 20 / 15",
      "Request", "Response", "After the turn ended", "after-turn-model"]) expect(host.textContent).toContain(text);
    const text = host.textContent!;
    expect(text.indexOf("recorded-model")).toBeLessThan(text.indexOf("After the turn ended"));
    expect(text.indexOf("After the turn ended")).toBeLessThan(text.indexOf("after-turn-model"));
    expect(listCalls().slice(0, 2).map(([, init]) => JSON.parse(String(init?.body)))).toEqual([
      { record_id: 7, limit: 500 }, { record_id: 7, between_turns: true, limit: 500 }]);
  });

  it.each([
    [null, false, false],
    [{ common_prefix_bytes: 300, upstream_request_bytes: 1000 }, false, false],
    [{ common_prefix_tokens: 950, previous_prompt_tokens: 1000 }, true, false],
    [{ common_prefix_tokens: 0, previous_prompt_tokens: 1000 }, true, false],
    [{ common_prefix_tokens: null, previous_prompt_tokens: 1000 }, false, false],
    [{ common_prefix_tokens: 0, previous_prompt_tokens: 0 }, false, false],
    [{ usage_missing: true }, false, true],
  ])("distinguishes provider cache hits, prompt reuse and missing usage", async (translation, prefix, missing) => {
    row = capturedRequest(1, { translation: translation as LlmRequest["translation"] });
    await show();
    await click("Model requests · 2");
    const request = button("recorded-model");
    expect(request.textContent).toContain("hit 40%");
    expect(request.textContent?.includes("prefix reuse")).toBe(prefix);
    if (prefix) expect(request.textContent).toContain(`prefix reuse ${row.translation?.common_prefix_tokens ? "95%" : "0%"}`);
    expect(request.textContent?.includes("no usage")).toBe(missing);
    if (missing) expect([...request.querySelectorAll("span")].find((span) => span.textContent === "no usage")?.className)
      .toContain("text-destructive");
  });

  it.each([0, null])("omits request hits for empty or incomplete usage", async (input_tokens) => {
    row = capturedRequest(1, { input_tokens, cache_read_tokens: 0, cache_write_tokens: 0 });
    await show();
    await click("Model requests · 2");
    expect(button("recorded-model").textContent).not.toContain("hit ");
  });

  it("orders whole seconds and microsecond timestamps chronologically, breaking equal times by id", async () => {
    const requests = [
      capturedRequest(4, { started_at: "2026-11-01T10:00:00.1Z", model: "tenth-second" }),
      capturedRequest(3, { started_at: "2026-11-01T10:00:00.000002Z", model: "same-microsecond-later-id" }),
      capturedRequest(9, { started_at: "2026-11-01T10:00:00Z", model: "whole-second" }),
      capturedRequest(8, { started_at: "2026-11-01T10:00:00.000001Z", model: "first-microsecond" }),
      capturedRequest(1, { started_at: "2026-11-01T10:00:00.000002Z", model: "same-microsecond-first-id" }),
    ];
    const ordinaryFetch = fetcher.getMockImplementation()!;
    fetcher.mockImplementation((input, init) => String(input).endsWith("llm_requests/list")
      ? Promise.resolve(ok({ requests: JSON.parse(String(init?.body)).between_turns ? [] : requests }))
      : ordinaryFetch(input, init));
    await show();
    await click("Model requests · 5");
    const models = [...host.querySelectorAll('[aria-label="Model requests"] button span.break-all')]
      .map((item) => item.textContent);
    expect(models).toEqual(["whole-second", "first-microsecond", "same-microsecond-first-id",
      "same-microsecond-later-id", "tenth-second"]);
  });

  it("opens the full viewer with the new-message divider and visibly redacted headers", async () => {
    await show();
    await click("Model requests · 2");
    await click("recorded-model");
    const viewer = host.querySelector('[aria-label="Model request 1"]')!;
    expect(viewer).toBeTruthy();
    for (const text of ["Stored system instructions", "Tools · 1", "read_file", "Stored reply", "end_turn"])
      expect(viewer.textContent).toContain(text);
    const divider = [...viewer.querySelectorAll("p")].find((item) => item.textContent === "New in this request")!;
    expect(divider.parentElement?.textContent).toContain("New reasoning");
    expect(divider.parentElement?.textContent).not.toContain("Earlier message");
    await click("Headers");
    const redacted = [...viewer.querySelectorAll("dd")].find((item) => item.textContent === "<redacted>")!;
    expect(redacted.classList.contains("italic")).toBe(true);
    expect(redacted.classList.contains("text-muted-foreground")).toBe(true);
  });

  it.each([1, 2])("shows no new-message divider for %i unchanged messages", async (length) => {
    const body = row.request_body as { messages: unknown[] };
    row = capturedRequest(1, { request_body: { ...body, messages: body.messages.slice(0, length) }, new_from_index: null });
    await show();
    await click("Model requests · 2");
    await click("recorded-model");
    const viewer = host.querySelector('[aria-label="Model request 1"]')!;
    expect(viewer.textContent).toContain(`Messages · ${length}`);
    expect(viewer.textContent).toContain("Earlier message");
    expect(viewer.textContent).not.toContain("New in this request");
  });

  it.each([
    [399, "completed", false], [400, "completed", true], [429, "completed", true],
    [500, "completed", true], [200, "upstream_error", true], [null, "upstream_error", true],
  ] as const)("styles HTTP %s / %s as a failure: %s", async (status, outcome, failure) => {
    row = capturedRequest(1, { status, outcome });
    await show();
    await click("Model requests · 2");
    const badge = [...button("recorded-model").querySelectorAll("span")]
      .find((item) => item.textContent === `${status ?? "—"} · ${outcome}`)!;
    expect(badge.classList.contains("text-destructive")).toBe(failure);
    expect(badge.classList.contains("text-success")).toBe(!failure);
  });

  it("shows unassembled raw text directly in the Response section", async () => {
    row = capturedRequest(1, { response_body: null, response_raw: "data: incomplete synthetic response", record_gap: "unparsable_stream" });
    await show();
    await click("Model requests · 2");
    await click("recorded-model");
    const viewer = host.querySelector('[aria-label="Model request 1"]')!;
    const response = [...viewer.querySelectorAll("h4")].find((item) => item.textContent === "Response")!.nextElementSibling!;
    expect(response.textContent).toContain(row.response_raw);
    expect(response.textContent).not.toContain("null");
    expect(button("Raw response").parentElement?.querySelector("pre")).toBeNull();
  });

  it.each([
    ["an unsplit array", [{ unexpected_body: "retained array entry" }]],
    ["unknown fields and message attributes", { custom_parameter: { retained: "unknown field" },
      messages: [{ role: "tool", content: "retained content", name: "tool-name", tool_call_id: "call-id",
        custom_attribute: "retained attribute" }] }],
  ])("keeps %s available as complete stored request JSON", async (_label, body) => {
    row = capturedRequest(1, { request_body: body });
    await show();
    await click("Model requests · 2");
    await click("recorded-model");
    await click("Stored request JSON");
    const viewer = host.querySelector('[aria-label="Model request 1"]')!;
    const stored = [...viewer.querySelectorAll("pre")].find((item) => {
      try { return JSON.stringify(JSON.parse(item.textContent!)) === JSON.stringify(body); }
      catch { return false; }
    });
    expect(stored, "the complete original body, including unknown fields, remains readable").toBeTruthy();
  });

  it("shows an upstream error body without content and preserves complete response JSON", async () => {
    const response = { type: "error", error: { type: "overloaded_error", message: "Synthetic upstream unavailable" },
      request_id: "synthetic-request-id", custom_field: ["retained response field"] };
    row = capturedRequest(1, { response_body: response, outcome: "upstream_error", status: 503 });
    await show();
    await click("Model requests · 2");
    await click("recorded-model");
    const viewer = host.querySelector('[aria-label="Model request 1"]')!;
    expect(viewer.textContent).toContain("Synthetic upstream unavailable");
    await click("Stored response JSON");
    const jsonBlocks = [...viewer.querySelectorAll("pre")].flatMap((item) => {
      try { return [JSON.parse(item.textContent!)]; } catch { return []; }
    });
    expect(jsonBlocks).toContainEqual(response);
  });

  it.each([
    ["request_too_large", "The request body exceeded the recorder's size limit (16 MiB, or 64 KiB for other endpoints), so only its hash and size were kept."],
    ["response_too_large", "The response body exceeded 4 MiB; only usage, stop reason, stop sequence and status fields were kept when present."],
    ["body_encoding_unparsed", "The body was compressed in a way the recorder could not decode; the raw response text is kept when present."],
    ["undecodable_encoding", "The body was compressed in a way the recorder could not decode; the raw response text is kept when present."],
    ["unparsable_request", "The request body could not be parsed, so only its hash and size were kept."],
    ["unparsable_stream", "The response could not be assembled, so only raw text (up to 1,048,576 characters) was kept for its body."],
    ["rejected_by_database", "The database rejected the body; only the recording metadata was kept."],
  ])("explains the %s capture gap", async (gap, explanation) => {
    row = capturedRequest(1, { record_gap: gap });
    await show();
    await click("Model requests · 2");
    await click("recorded-model");
    expect(host.querySelector('[aria-label="Model request 1"]')?.textContent).toContain(explanation);
  });

  it("reloads a completed run's requests when reopened to include later between-turn requests", async () => {
    let arrived = false;
    const ordinaryFetch = fetcher.getMockImplementation()!;
    fetcher.mockImplementation((input, init) => {
      const body = JSON.parse(String(init?.body ?? "{}"));
      return String(input).endsWith("llm_requests/list") && body.between_turns && !arrived
        ? Promise.resolve(ok({ requests: [] })) : ordinaryFetch(input, init);
    });
    await show();
    await click("Model requests · 1");
    expect(host.textContent).not.toContain("After the turn ended");
    await click("Model requests · 1");
    arrived = true;
    await click("Model requests · 1");
    expect(host.textContent).toContain("After the turn ended");
    expect(host.textContent).toContain("after-turn-model");
  });

  it("stops polling when the run finishes", async () => {
    vi.useFakeTimers();
    latest = record({ llmrec: { state: "recording" } }, "running");
    await show(latest);
    await act(async () => { await vi.advanceTimersByTimeAsync(2000); });
    expect(listCalls().length).toBeGreaterThan(2);
    latest = { ...latest, status: "completed" };
    await act(async () => { await vi.advanceTimersByTimeAsync(2000); });
    const callsAfterEnd = listCalls().length;
    await act(async () => { await vi.advanceTimersByTimeAsync(6000); });
    expect(listCalls()).toHaveLength(callsAfterEnd);
  });

  it("exports two pages as a header followed by complete request lines", async () => {
    const requests = [row, capturedRequest(2, { record_gap: "unparsable_stream", response_raw: "retained raw" })];
    fetcher.mockImplementation(async (_url, init) => {
      const body = JSON.parse(String(init?.body));
      return ok({ requests: [requests[body.after_id ? 1 : 0]], next_after_id: body.after_id ? null : 1 });
    });
    const create = vi.spyOn(URL, "createObjectURL").mockReturnValue("blob:requests");
    const anchor = vi.spyOn(HTMLAnchorElement.prototype, "click").mockImplementation(() => {});
    const progress = vi.fn();
    expect(await exportLlmRequests({ record_id: 7 }, { onProgress: progress })).toBe(2);
    const lines = (await (create.mock.calls[0][0] as Blob).text()).trim().split("\n").map((line) => JSON.parse(line));
    expect(lines[0]).toEqual({ type: "llmrec_export", format: 1, exported_at: expect.any(String), filter: { record_id: 7 },
      redaction: "header values outside the allowlist are never stored; bodies are complete unless record_gap says otherwise" });
    expect(lines.slice(1)).toEqual(requests);
    expect(progress.mock.calls).toEqual([[1], [2]]);
    expect((anchor.mock.instances[0] as HTMLAnchorElement).download).toBe("llm-requests-record-7.jsonl");
  });

  it("shows export progress and cancels without downloading a partial file", async () => {
    let resolvePage!: (value: Response) => void;
    const ordinaryFetch = fetcher.getMockImplementation()!;
    fetcher.mockImplementation((input, init) => {
      if (!String(input).endsWith("llm_requests/export")) return ordinaryFetch(input, init);
      const body = JSON.parse(String(init?.body));
      return body.after_id ? new Promise<Response>((resolve) => { resolvePage = resolve; })
        : Promise.resolve(ok({ requests: [row], next_after_id: 1 }));
    });
    const create = vi.spyOn(URL, "createObjectURL").mockReturnValue("blob:requests");
    const anchor = vi.spyOn(HTMLAnchorElement.prototype, "click").mockImplementation(() => {});
    await show();
    await click("Export JSONL");
    expect(host.textContent).toContain("Exporting · 1 requests");
    await click("Cancel");
    await act(async () => resolvePage(ok({ requests: [capturedRequest(2)], next_after_id: null })));
    expect(host.textContent).not.toContain("Exporting");
    expect(create).not.toHaveBeenCalled();
    expect(anchor).not.toHaveBeenCalled();
    expect(button("Export JSONL").disabled).toBe(false);
  });
});
