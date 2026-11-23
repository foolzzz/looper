// @vitest-environment happy-dom
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { SkillProposals, SkillProposalsSetting } from "@/components/skill-proposals";
import {
  confirmSkillProposal,
  fetchSkillProposals,
  rejectSkillProposal,
  restoreSkillProposal,
  setSkillProposalMode,
  type SkillProposal,
} from "@/lib/api";
import { V1Error } from "@/lib/v1";

vi.mock("@/lib/api", async (original) => ({
  ...await original<typeof import("@/lib/api")>(),
  fetchSkillProposals: vi.fn(),
  confirmSkillProposal: vi.fn(),
  rejectSkillProposal: vi.fn(),
  restoreSkillProposal: vi.fn(),
  setSkillProposalMode: vi.fn(),
}));
(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

function proposal(over: Partial<SkillProposal> = {}): SkillProposal {
  return {
    proposal_id: 7, project_id: "proj-1", action: "create", skill_id: null, name: "release-check",
    description: "Use before a release.", why: "Three tasks re-derived these steps.",
    evidence: [{ task: "task-1", step: "s2" }], proposer: "planner:task-1", status: "pending",
    content_sha256: "a".repeat(64), base_sha256: null, content: "# Release check\n1. Run the tests.\n",
    created_at: "2026-11-20T12:00:00Z", expires_at: "2026-11-27T12:00:00Z", decided_at: null, decision_note: null,
    current: null, diff: null, stale: false, affected_projects: [], ...over,
  };
}

const update = () => proposal({
  action: "update", skill_id: "skill-1", base_sha256: "b".repeat(64), name: "deploy-check",
  current: { name: "deploy-check", description: "Old.", sha256: "b".repeat(64) },
  diff: { name: { from: "deploy-check", to: "deploy-check" }, description: { from: "Old.", to: "Use before a release." },
    content: "--- current content\n+++ proposed content\n@@ -1 +1 @@\n-Old line.\n+New line.\n" },
  affected_projects: [{ project_id: "proj-1", project_name: "Calc" }, { project_id: "proj-2", project_name: "Docs" }],
});

let root: Root;
let host: HTMLDivElement;
const written = vi.fn();
const buttons = () => [...host.querySelectorAll("button")];
const named = (label: string) => buttons().find((b) => b.getAttribute("aria-label") === label || (b.textContent ?? "").trim() === label)!;
const render = async () => {
  // A new root each time: the list is read when the component mounts.
  await act(async () => root.unmount());
  root = createRoot(host);
  await act(async () => { root.render(<SkillProposals projectId="proj-1" onWritten={written} />); });
};
function type(field: HTMLInputElement | HTMLTextAreaElement, value: string) {
  const proto = field instanceof HTMLTextAreaElement ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;
  act(() => {
    Object.getOwnPropertyDescriptor(proto, "value")!.set!.call(field, value);
    field.dispatchEvent(new Event("input", { bubbles: true }));
  });
}

beforeEach(() => {
  vi.mocked(fetchSkillProposals).mockResolvedValue({ mode: "on", proposals: [proposal()] });
  vi.mocked(confirmSkillProposal).mockResolvedValue({ proposal: proposal({ status: "confirmed" }), version: 1 });
  vi.mocked(rejectSkillProposal).mockResolvedValue(proposal({ status: "rejected" }));
  vi.mocked(restoreSkillProposal).mockResolvedValue(proposal());
  host = document.createElement("div");
  document.body.appendChild(host);
  root = createRoot(host);
});

afterEach(() => {
  act(() => root.unmount());
  host.remove();
  vi.clearAllMocks();
});

describe("SkillProposals", () => {
  it("shows nothing while proposals are off and none exist, and shows the column when on or when any exist", async () => {
    vi.mocked(fetchSkillProposals).mockResolvedValue({ mode: "off", proposals: [] });
    await render();
    expect(host.textContent).toBe("");
    vi.mocked(fetchSkillProposals).mockResolvedValue({ mode: "on", proposals: [] });
    await render();
    expect(host.textContent).toContain("Proposed skills");
    expect(host.textContent).toContain("No proposals are waiting.");
    vi.mocked(fetchSkillProposals).mockResolvedValue({ mode: "off", proposals: [proposal()] });
    await render();
    expect(host.textContent).toContain("switched off for this project");
    expect(host.textContent).toContain("release-check");
  });

  it("shows a new skill's full text with its reason and evidence, and nothing as taken effect", async () => {
    await render();
    expect(host.textContent).toContain("New skill");
    expect(host.textContent).toContain("Three tasks re-derived these steps.");
    expect(host.textContent).toContain("1. Run the tests.");
    expect(host.textContent).toContain("no run sees a proposal until you confirm it");
    expect(host.querySelector("a[href='#tasks/task-1']")?.textContent).toBe("Task task-1, step s2");
  });

  it("shows an update as the server's difference and lists every project it reaches", async () => {
    vi.mocked(fetchSkillProposals).mockResolvedValue({ mode: "on", proposals: [update()] });
    await render();
    expect(host.textContent).toContain("Update");
    expect(host.textContent).toContain("-Old line.");
    expect(host.textContent).toContain("+New line.");
    expect(host.textContent).toContain("Description: Old. → Use before a release.");
    expect(host.textContent).toContain("every project that installs it: Calc, Docs");
    expect(host.textContent).not.toContain("# Release check");
  });

  it("confirms with the hash that was displayed, then reads the list and the installs again", async () => {
    await render();
    await act(async () => named("Confirm proposal release-check").click());
    expect(confirmSkillProposal).toHaveBeenCalledWith("proj-1", 7, "a".repeat(64));
    expect(written).toHaveBeenCalledTimes(1);
    expect(fetchSkillProposals).toHaveBeenCalledTimes(2);
  });

  it("on a 409 reloads, says why, and does not call it a success", async () => {
    vi.mocked(confirmSkillProposal).mockRejectedValue(
      new V1Error(409, "conflict", "the skill changed: the proposal is superseded; draft it again"));
    await render();
    vi.mocked(fetchSkillProposals).mockResolvedValue({ mode: "on", proposals: [] });
    await act(async () => named("Confirm proposal release-check").click());
    expect(host.querySelector("[role=alert]")?.textContent).toContain("the proposal is superseded");
    expect(host.textContent).toContain("The list is reloaded.");
    expect(host.textContent).toContain("No proposals are waiting.");
    expect(written).not.toHaveBeenCalled();
  });

  it("warns that a stale update will be superseded", async () => {
    vi.mocked(fetchSkillProposals).mockResolvedValue({ mode: "on", proposals: [{ ...update(), stale: true }] });
    await render();
    expect(host.querySelector("[role=alert]")?.textContent).toContain("Confirming marks the proposal superseded and writes nothing.");
  });

  it("rejects with a reason", async () => {
    await render();
    await act(async () => named("Reject proposal release-check").click());
    type(host.querySelector<HTMLInputElement>("input")!, "  too specific ");
    await act(async () => named("Reject proposal").click());
    expect(rejectSkillProposal).toHaveBeenCalledWith("proj-1", 7, "too specific");
    expect(confirmSkillProposal).not.toHaveBeenCalled();
  });

  it("edits, then confirms the operator's text against the displayed hash", async () => {
    await render();
    await act(async () => named("Edit proposal release-check then confirm").click());
    expect(named("Save and confirm").hasAttribute("disabled")).toBe(true);
    const content = host.querySelector<HTMLTextAreaElement>("textarea")!;
    type(content, "# Release check\nRun the tests twice.\n");
    await act(async () => named("Save and confirm").click());
    expect(confirmSkillProposal).toHaveBeenCalledWith("proj-1", 7, "a".repeat(64), {
      content: "# Release check\nRun the tests twice.\n" });
  });

  it("restores an expired proposal and offers no Confirm for it", async () => {
    vi.mocked(fetchSkillProposals).mockResolvedValue({ mode: "on", proposals: [proposal({ status: "expired" })] });
    await render();
    expect(host.textContent).toContain("Expired");
    expect(buttons().some((b) => b.getAttribute("aria-label")?.startsWith("Confirm proposal"))).toBe(false);
    await act(async () => named("Restore proposal release-check").click());
    expect(restoreSkillProposal).toHaveBeenCalledWith("proj-1", 7);
  });

  it("shows a refusal from the server where it happened", async () => {
    vi.mocked(restoreSkillProposal).mockRejectedValue(new V1Error(400, "invalid", "the skill changed after this was drafted; draft it again"));
    vi.mocked(fetchSkillProposals).mockResolvedValue({ mode: "on", proposals: [proposal({ status: "expired" })] });
    await render();
    await act(async () => named("Restore proposal release-check").click());
    expect(host.querySelector("[role=alert]")?.textContent).toContain("draft it again");
  });
});

describe("SkillProposalsSetting", () => {
  it("saves the switch at once and says proposals never take effect unconfirmed", async () => {
    vi.mocked(setSkillProposalMode).mockResolvedValue("on");
    await act(async () => root.render(<SkillProposalsSetting projectId="proj-1" initial={undefined} />));
    const toggle = host.querySelector<HTMLButtonElement>("[role=switch]")!;
    expect(toggle.getAttribute("aria-checked")).toBe("false");
    expect(host.textContent).toContain("never takes effect until you confirm it");
    await act(async () => toggle.click());
    expect(setSkillProposalMode).toHaveBeenCalledWith("proj-1", "on");
    expect(toggle.getAttribute("aria-checked")).toBe("true");
  });

  it("keeps the switch where it was when saving fails", async () => {
    vi.mocked(setSkillProposalMode).mockRejectedValue(new Error("not allowed"));
    await act(async () => root.render(<SkillProposalsSetting projectId="proj-1" initial="on" />));
    const toggle = host.querySelector<HTMLButtonElement>("[role=switch]")!;
    await act(async () => toggle.click());
    expect(toggle.getAttribute("aria-checked")).toBe("true");
    expect(host.querySelector("[role=alert]")?.textContent).toBe("not allowed");
  });
});
