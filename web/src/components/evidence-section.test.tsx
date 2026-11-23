// @vitest-environment happy-dom
import { act, useState } from "react";
import { createRoot, type Root } from "react-dom/client";
import { renderToStaticMarkup } from "react-dom/server";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { FailedChecksLine } from "@/components/failed-checks-line";
import { NodeCardView } from "@/components/pipeline-node-card";
import { stepCard, type NodeCard } from "@/lib/node-cards";
import { stepsNeedEvidence, useStepEvidence, type EvidenceLoad, type EvidenceRun, type TaskEvidence } from "@/lib/evidence";
import { callV1 } from "@/lib/v1";
import type { AcceptanceCheck, TaskStep } from "@/lib/api";

vi.mock("@/lib/v1", async (original) => ({
  ...await original<typeof import("@/lib/v1")>(),
  callV1: vi.fn(),
}));
(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

const LOOK = { label: "Failed", border: "var(--color-destructive)", borderStyle: "solid" as const, background: "bg", color: "fg" };

function check(id: string, criterion: number, command: string, required = true): AcceptanceCheck {
  return { id, criterion, type: "command", command, timeout_seconds: 30, required, network: false, protect: [] };
}

function taskStep(partial: Partial<TaskStep> = {}): TaskStep {
  return {
    id: "step-1", task_id: "t1", step_index: 0, step_key: "build", kind: "work", instruction: "Build it",
    status: "failed", acceptance_criteria: [], target_files: [], depends_on: [], review: false,
    role_key: null, role_version: null, review_roles: [], review_stage: null, review_position: null,
    retries: 0, worker_id: null, reviewer_id: null, result: null,
    created_at: "2026-01-01T00:00:00Z", updated_at: "2026-01-01T00:00:00Z", completed_at: null, started_at: null,
    ...partial,
  };
}

function evidenceRun(partial: Partial<EvidenceRun>): EvidenceRun {
  return {
    id: 1, task_id: "t1", step_id: "step-1", check_id: "pass", attempt: 1, run: 1, state: "passed",
    error_kind: null, exit_code: null, duration_ms: null, head_sha: null, guarded: true, required: true,
    output_tail: "", artifacts: [], protected_changed: [], ...partial,
  };
}

function draw(step: TaskStep, load?: EvidenceLoad): string {
  const card = stepCard(step, {
    taskId: "t1", active: false, look: LOOK, lookOf: () => LOOK, stepsById: new Map([[step.id, step]]), evidence: load,
  });
  return renderToStaticMarkup(<NodeCardView card={card} />);
}

const COMMAND = `${"a".repeat(80)}TAIL`;

const ready: EvidenceLoad = { status: "ready", data: {
  task_id: "t1",
  steps: [{ step_id: "step-1", step: "build", checks: [], runs: [
    evidenceRun({
      id: 1, check_id: "pass", state: "passed", exit_code: 0, duration_ms: 90_000,
      head_sha: "abcdef1234567890ffff", guarded: false, output_tail: "<script>alert(1)</script>",
      artifacts: [{ name: "1.png", mime: "image/png", bytes: 8, sha256: "aa" }],
      protected_changed: ["secrets.env"],
    }),
    evidenceRun({ id: 2, check_id: "fail", state: "failed", exit_code: 1, required: false,
      artifacts: [{ name: "2.txt", mime: "text/plain", bytes: 4, sha256: "bb" }] }),
    evidenceRun({ id: 3, check_id: "err", state: "error", error_kind: "timeout" }),
    evidenceRun({ id: 4, check_id: "old", state: "stale" }),
    evidenceRun({ id: 5, check_id: "live", state: "running" }),
    evidenceRun({ id: 6, check_id: "retry", attempt: 1, run: 1, state: "failed", output_tail: "hidden-tail" }),
    evidenceRun({ id: 7, check_id: "retry", attempt: 1, run: 2, state: "failed", output_tail: "oldest-tail" }),
    evidenceRun({ id: 8, check_id: "retry", attempt: 2, run: 1, state: "failed", output_tail: "older-tail" }),
    evidenceRun({ id: 9, check_id: "retry", attempt: 3, run: 1, state: "passed", output_tail: "current-tail" }),
  ] }],
} };

const checked = taskStep({
  acceptance_criteria: [
    "the command exits 0", "the command exits 1", "it stays in its limit", "it is still current",
    "it is running", "a person reads it", "a retry settles it", "it has not run",
  ],
  checks: [
    check("pass", 1, COMMAND),
    check("fail", 2, "npm test", false),
    check("err", 3, "npm run build"),
    check("old", 4, "npm run lint"),
    check("live", 5, "npm start"),
    check("retry", 7, "npm run retry"),
    check("missing", 8, "npm run missing"),
  ],
});

describe("step evidence", () => {
  it("draws each state in words, and leaves a step with no checks unchanged", () => {
    const plain = taskStep({ acceptance_criteria: ["done"] });
    const untouched = stepCard(plain, {
      taskId: "t1", active: false, look: LOOK, lookOf: () => LOOK, stepsById: new Map([[plain.id, plain]]),
      evidence: ready,
    });
    expect(untouched.sections.some((section) => section.kind === "evidence" || section.title === "Evidence")).toBe(false);

    const html = draw(checked, ready);
    expect(html).toContain(">Passed<");
    expect(html).toContain(">Failed<");
    expect(html).toContain(">Error: timed out<");
    expect(html).toContain(">Stale<");
    expect(html).toContain(">Running<");
    expect(html).toContain("No check — judged by the Reviewer");
    expect(html).toContain("No run recorded");
    expect(html).toContain("unguarded");
    expect(html).toContain("advisory");
    expect(html).not.toContain("report_only");
    expect(html).toContain("exit 0");
    expect(html).toContain("1m 30s");
    expect(html).toContain("abcdef123456");
    expect(html).not.toContain("abcdef1234567890");
    expect(html).toContain("TAIL");
    expect(html).toContain("…");
    expect(html).toContain("&lt;script&gt;alert(1)&lt;/script&gt;");
    expect(html).not.toContain("<script>");
    expect(html).toContain('src="/api/evidence/t1/1/0"');
    expect(html).toContain('href="/api/evidence/t1/2/0"');
    expect(html).toContain(">2.txt<");
    expect(html).toContain("Protected paths changed");
    expect(html).toContain("secrets.env");

    const current = html.indexOf("current-tail");
    const earlier = html.indexOf("Earlier attempts");
    const older = html.indexOf("older-tail");
    const oldest = html.indexOf("oldest-tail");
    expect(current).toBeGreaterThan(-1);
    expect(current).toBeLessThan(earlier);
    expect(earlier).toBeLessThan(older);
    expect(older).toBeLessThan(oldest);
    expect(html).not.toContain("hidden-tail");
    expect(html).not.toMatch(/<details[^>]*\sopen/);
  });

  it("shows a failed evidence read and does not leave the section empty", () => {
    const html = draw(checked, { status: "error", error: "the database is down" });
    expect(html).toContain('role="alert"');
    expect(html).toContain("the database is down");
    expect(html).toContain("Evidence");
    expect(html).not.toContain(">Passed<");
  });

  it("says when the evidence is still loading", () => {
    expect(draw(checked, { status: "loading" })).toContain("Loading evidence…");
  });
});

describe("Needs attention line", () => {
  it("names the failed checks and links to the step card", () => {
    const html = renderToStaticMarkup(<FailedChecksLine taskId="task 1" checks={["c1", "c2"]} stepId="step-a" />);
    expect(html).toContain("required check failed: c1, c2");
    expect(html).toContain('href="#tasks/task%201?tab=pipeline&amp;step=step-a"');
  });

  it("does not open the row when the line is clicked", async () => {
    const host = document.createElement("div");
    document.body.appendChild(host);
    function Row() {
      const [opened, setOpened] = useState(false);
      return (
        <div onClick={() => setOpened(true)}>
          <FailedChecksLine taskId="t1" checks={["c1"]} stepId="step-a" />
          {opened ? <span>row opened</span> : null}
        </div>
      );
    }
    const root = createRoot(host);
    await act(async () => { root.render(<Row />); });
    await act(async () => { host.querySelector("a")!.click(); });
    expect(host.textContent).toContain("required check failed: c1");
    expect(host.textContent).not.toContain("row opened");
    await act(async () => { root.unmount(); });
    host.remove();
  });
});

describe("evidence requests", () => {
  let root: Root;
  let host: HTMLElement;

  beforeEach(() => {
    vi.mocked(callV1).mockReset();
    vi.mocked(callV1).mockResolvedValue({ task_id: "t1", steps: [] } satisfies TaskEvidence);
    host = document.createElement("div");
    document.body.appendChild(host);
    root = createRoot(host);
  });

  afterEach(async () => {
    await act(async () => { root.unmount(); });
    host.remove();
  });

  function Probe({ status, steps }: { status: string; steps: TaskStep[] }) {
    useStepEvidence("t1", stepsNeedEvidence(steps), status, steps);
    return null;
  }

  it("does not ask when no step has checks", async () => {
    await act(async () => { root.render(<Probe status="failed" steps={[taskStep()]} />); });
    expect(callV1).not.toHaveBeenCalled();
  });

  it("asks with task_evidence.get when a step has checks", async () => {
    await act(async () => { root.render(<Probe status="failed" steps={[checked]} />); });
    expect(callV1).toHaveBeenCalledWith("/api/v1/task_evidence/get", { task: "t1" }, expect.any(Object));
  });

  it("reads evidence again after a step status change", async () => {
    const step = { ...checked, status: "running", retries: 1 };
    await act(async () => { root.render(<Probe status="running" steps={[step]} />); });
    expect(callV1).toHaveBeenCalledTimes(1);
    await act(async () => { root.render(<Probe status="running" steps={[{ ...step }]} />); });
    expect(callV1).toHaveBeenCalledTimes(1);
    await act(async () => { root.render(<Probe status="running" steps={[{ ...step, status: "failed" }]} />); });
    expect(callV1).toHaveBeenCalledTimes(2);
    expect(callV1).toHaveBeenLastCalledWith("/api/v1/task_evidence/get", { task: "t1" }, expect.any(Object));
    await act(async () => { root.render(<Probe status="running" steps={[{ ...step, status: "failed", retries: 2 }]} />); });
    expect(callV1).toHaveBeenCalledTimes(3);
  });
});

describe("evidence targets", () => {
  it("gives every summary and link a coarse-pointer height and a visible expand mark", () => {
    const host = document.createElement("div");
    host.innerHTML = draw(checked, ready);
    const summaries = [...host.querySelectorAll("summary")];
    expect(summaries.length).toBeGreaterThanOrEqual(3);
    for (const summary of summaries) {
      expect(summary.className).toContain("pointer-coarse:min-h-11");
      expect(summary.className).toContain("items-center");
      expect(summary.innerHTML).toContain("-rotate-90");
    }
    const download = [...host.querySelectorAll("a")].find((link) => link.textContent === "2.txt");
    expect(download?.className).toContain("pointer-coarse:min-h-11");
    const image = host.querySelector("img");
    expect(image?.closest("a")?.getAttribute("href")).toBe("/api/evidence/t1/1/0");
    expect(image?.closest("a")?.getAttribute("target")).toBe("_blank");
    const line = document.createElement("div");
    line.innerHTML = renderToStaticMarkup(<FailedChecksLine taskId="t1" checks={["c1"]} stepId="step-a" />);
    expect(line.querySelector("a")?.className).toContain("pointer-coarse:min-h-11");
  });
});

describe("a missing image", () => {
  async function showImage() {
    const card: NodeCard = stepCard(checked, {
      taskId: "t1", active: false, look: LOOK, lookOf: () => LOOK, stepsById: new Map([[checked.id, checked]]),
      evidence: ready,
    });
    const host = document.createElement("div");
    document.body.appendChild(host);
    const root = createRoot(host);
    await act(async () => { root.render(<NodeCardView card={card} />); });
    const image = host.querySelector("img");
    expect(image).not.toBeNull();
    return { host, root, image: image! };
  }

  afterEach(() => { vi.unstubAllGlobals(); });

  it("says the file is missing when the image answers 404", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue({ status: 404 }));
    const { host, root, image } = await showImage();
    await act(async () => { image.dispatchEvent(new Event("error")); });
    expect(host.textContent).toContain("file missing");
    expect(host.textContent).not.toContain("operator session required");
    expect(host.querySelector("img")).toBeNull();
    await act(async () => { root.unmount(); });
    host.remove();
  });

  it("says the session is required when the image answers 403", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue({ status: 403 }));
    const { host, root, image } = await showImage();
    await act(async () => { image.dispatchEvent(new Event("error")); });
    expect(host.textContent).toContain("operator session required");
    expect(host.textContent).not.toContain("file missing");
    expect(host.querySelector("img")).toBeNull();
    await act(async () => { root.unmount(); });
    host.remove();
  });
});
