// @vitest-environment happy-dom
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { renderToStaticMarkup } from "react-dom/server";
import { afterEach, describe, expect, it, vi } from "vitest";
import { RunTree, RunTreeContent } from "@/components/run-tree";
import type { RunTreeNode } from "@/lib/api";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
const node = (id: number, extra: Partial<RunTreeNode> = {}): RunTreeNode => ({
  id, kind: "worker", parent_record_id: null, spawner: { role: null, seat: null }, reason: "worker",
  model: "example-model", status: "completed", started_at: "2026-11-22T10:00:00Z", ended_at: "2026-11-22T10:01:00Z",
  own_cost: { subscription_usd: 0.2, api_usd: 0 }, subtree_cost: { subscription_usd: 0.3, api_usd: 0 },
  result_summary: null, result_verified: null, failure_class: null, failure_cause: null,
  state: null, delivery: null, cancel_reason: null, parent_spend_known: null, wait_intervals: [], ...extra,
});
const nodes = [node(1, { wait_intervals: [{ started_at: "2026-11-22T10:00:10Z", ended_at: "2026-11-22T10:00:40Z" }] }),
  node(2, { kind: "explorer", parent_record_id: 1, spawner: { role: "worker", seat: "worker@s1" }, reason: "Find the boundary",
    state: "answered", result_summary: "The boundary is explicit.", result_verified: false }),
  node(3, { kind: "reviewer" })];

describe("Run tree", () => {
  it("shows the hierarchy, creator, reason, state, duration, both costs and unverified result", () => {
    const html = renderToStaticMarkup(<RunTreeContent nodes={nodes} />);
    const host = document.createElement("div"); host.innerHTML = html;
    const tree = host.querySelector('[aria-label="Run hierarchy"]')!;
    expect(tree.querySelector('[data-run-id="1"] [data-run-id="2"]')).not.toBeNull();
    for (const text of ["worker@s1", "Find the boundary", "Answered", "1m 0s", "Own", "Subtree", "$0.2000", "$0.3000", "Unverified", "The boundary is explicit."])
      expect(tree.textContent).toContain(text);
  });
  it("has one timeline bar per run and dashed wait segments; below sm uses a time-ordered list", () => {
    const host = document.createElement("div"); host.innerHTML = renderToStaticMarkup(<RunTreeContent nodes={[...nodes].reverse()} />);
    const timeline = host.querySelector('[aria-label="Run timeline"]')!;
    expect(timeline.className).toContain("hidden sm:block");
    expect(timeline.querySelectorAll('[data-run-bar][role="img"]')).toHaveLength(3);
    expect(timeline.querySelector('[aria-label="Waiting for sub-agents"]')?.getAttribute("role")).toBe("img");
    expect(timeline.querySelector('[aria-label="Waiting for sub-agents"]')?.className).toContain("border-dashed");
    const list = host.querySelector('[aria-label="Runs in time order"]')!;
    expect(list.className).toContain("sm:hidden");
    expect([...list.querySelectorAll('[data-run-id]')].map((item) => item.getAttribute("data-run-id"))).toEqual(["1", "2", "3"]);
  });
  it("shows no runs and unknown cost without implying zero", () => {
    expect(renderToStaticMarkup(<RunTreeContent nodes={[]} />)).toContain("No runs yet");
    const html = renderToStaticMarkup(<RunTreeContent nodes={[node(4, { own_cost: { subscription_usd: null, api_usd: null }, subtree_cost: { subscription_usd: null, api_usd: null } })]} />);
    expect(html).toContain("cost unknown");
    expect(html).not.toContain("$0.0000");
    const partial = renderToStaticMarkup(<RunTreeContent nodes={[node(5, { own_cost: { subscription_usd: null, api_usd: 0.2 } })]} />);
    expect(partial).toContain("cost unknown"); expect(partial).toContain("$0.2000");
  });
});

let root: Root | undefined;
afterEach(() => { if (root) act(() => root!.unmount()); root = undefined; vi.unstubAllGlobals(); });
it("reads the task tree and exposes loading and a failed read's reason", async () => {
  const host = document.createElement("div"); root = createRoot(host);
  let fail!: (reason: Error) => void;
  vi.stubGlobal("fetch", vi.fn(() => new Promise((_, reject) => { fail = reject; })));
  await act(async () => root!.render(<RunTree taskId="task-tree" />));
  expect(host.textContent).toContain("Loading run tree");
  await act(async () => fail(new Error("Database unavailable")));
  expect(host.querySelector('[role="alert"]')?.textContent).toContain("Database unavailable");
});

it("keeps the tree mounted while refreshing and on a failed refresh, then clears it for another task", async () => {
  const host = document.createElement("div"); root = createRoot(host);
  let finish!: (value: Response) => void;
  let fail!: (reason: Error) => void;
  vi.stubGlobal("fetch", vi.fn(() => new Promise<Response>((resolve, reject) => { finish = resolve; fail = reject; })));
  await act(async () => root!.render(<RunTree taskId="task-tree" refreshKey="initial" />));
  await act(async () => finish(Response.json({ status: "ok", result: { task_id: "task-tree", nodes } })));
  const hierarchy = host.querySelector('[aria-label="Run hierarchy"]')!;
  expect(hierarchy).not.toBeNull();
  host.scrollTop = 120;
  await act(async () => root!.render(<RunTree taskId="task-tree" refreshKey="refresh" />));
  expect(host.querySelector('[aria-label="Run hierarchy"]')).toBe(hierarchy);
  expect(host.textContent).not.toContain("Loading run tree");
  expect(host.scrollTop).toBe(120);
  await act(async () => fail(new Error("Read failed")));
  expect(host.querySelector('[role="alert"]')?.textContent).toContain("Read failed");
  expect(host.querySelector('[aria-label="Run hierarchy"]')).toBe(hierarchy);
  await act(async () => root!.render(<RunTree taskId="another-task" />));
  expect(host.querySelector('[aria-label="Run hierarchy"]')).toBeNull();
  expect(host.textContent).toContain("Loading run tree");
  await act(async () => finish(Response.json({ status: "ok", result: { task_id: "another-task", nodes: [] } })));
  expect(host.textContent).toContain("No runs yet");
});
