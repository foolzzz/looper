// @vitest-environment happy-dom
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { ProjectSkillsSection } from "@/components/project-skills";
import { SkillHistory } from "@/components/skill-history";
import {
  fetchCatalog,
  fetchContextBudget,
  fetchSkillProposals,
  fetchProjectInstalls,
  fetchSkillVersions,
  rollbackProjectSkill,
  type Catalog,
  type CatalogItem,
  type ProjectInstall,
  type ProjectInstalls,
  type SkillVersions,
} from "@/lib/api";

vi.mock("@/lib/api", async (original) => ({
  ...await original<typeof import("@/lib/api")>(),
  fetchProjectInstalls: vi.fn(),
  fetchCatalog: vi.fn(),
  fetchSkillVersions: vi.fn(),
  rollbackProjectSkill: vi.fn(),
  fetchProjectSkill: vi.fn(),
  fetchContextBudget: vi.fn(),
  fetchSkillProposals: vi.fn(),
}));
(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

const history = (): SkillVersions => ({
  skill_id: "skill-1",
  current_version: 2,
  versions: [
    {
      version: 1, name: "deploy-check", description: "Old desc", sha256: "a", author_kind: "operator",
      author_ref: null, reason: "imported", created_at: "2026-01-01T00:00:00Z",
    },
    {
      version: 2, name: "deploy-check", description: "New desc", sha256: "b", author_kind: "rollback",
      author_ref: "1", reason: "", created_at: "2026-01-02T00:00:00Z",
    },
  ],
  affected_projects: [{ project_id: "p-alpha", project_name: "Alpha" }],
  diff: null,
});

const ranged = (fromVersion: number, toVersion: number, affected = history().affected_projects): SkillVersions => {
  const forward = fromVersion < toVersion;
  return {
    ...history(),
    affected_projects: affected,
    diff: {
      from_version: fromVersion,
      to_version: toVersion,
      name: { from: "deploy-check", to: "deploy-check" },
      description: forward ? { from: "Old desc", to: "New desc" } : { from: "New desc", to: "Old desc" },
      content: forward
        ? "--- version 1\n+++ version 2\n-one\n+two\n"
        : "--- version 2\n+++ version 1\n-two\n+one\n",
    },
  };
};

function item(over: Partial<CatalogItem>): CatalogItem {
  return {
    item_id: "skill-1",
    kind: "skill",
    name: "deploy-check",
    qualified_name: "project:deploy-check",
    origin: "project",
    owner_project_id: "proj-1",
    owner_project_name: "Calc",
    description: "Use for deploy-check.",
    source: "",
    version: "",
    sha256: "abc",
    size_bytes: 20,
    file_count: 1,
    modified: false,
    imported_at: "2026-01-01T00:00:00Z",
    updated_at: "2026-01-02T00:00:00Z",
    mcp: null,
    installs: [],
    ...over,
  };
}

function install(over: Partial<ProjectInstall> & { item: CatalogItem }): ProjectInstall {
  return {
    install_id: "inst-1",
    item_id: over.item.item_id,
    scope: "project",
    project_id: "proj-1",
    project_name: "Calc",
    roles: ["worker"],
    enabled: true,
    created_at: "2026-01-01T00:00:00Z",
    updated_at: "2026-01-02T00:00:00Z",
    inherited: false,
    ...over,
  };
}

const installs = (): ProjectInstalls => ({
  project_id: "proj-1",
  owned_count: 1,
  task_roles: ["planner", "worker", "reviewer"],
  skill_caps: { name_max_chars: 64, description_max_chars: 1024, content_max_bytes: 65536, project_max_skills: 20 },
  installs: [
    install({ item: item({}) }),
    install({
      install_id: "inst-global",
      inherited: true,
      scope: "global",
      project_id: null,
      project_name: null,
      item: item({
        item_id: "plugin-1", kind: "plugin", name: "tools", qualified_name: "tools", origin: "machine",
        owner_project_id: null, owner_project_name: null, description: "Tools.",
      }),
    }),
    install({
      install_id: "inst-other",
      item: item({
        item_id: "skill-other", name: "elsewhere", qualified_name: "project:elsewhere",
        owner_project_id: "proj-other", owner_project_name: "Other",
      }),
    }),
  ],
});

let root: Root;
let host: HTMLElement;
const rolledBack = vi.fn(async () => true);

async function renderHistory() {
  await act(async () => {
    root.render(
      <SkillHistory id="history" projectId="proj-1" skillId="skill-1" revision="2026-01-02T00:00:00Z" busy={false}
        onRolledBack={rolledBack} />,
    );
  });
}

const buttons = () => [...host.querySelectorAll("button")];
const labelOf = (button: Element) => (button.textContent ?? "").trim();

beforeEach(() => {
  vi.mocked(fetchSkillVersions).mockImplementation(async (_projectId, _skillId, range) => (
    range ? ranged(range.fromVersion, range.toVersion) : history()
  ));
  vi.mocked(rollbackProjectSkill).mockResolvedValue({} as never);
  vi.mocked(fetchProjectInstalls).mockResolvedValue(installs());
  vi.mocked(fetchCatalog).mockResolvedValue({ items: [] } as unknown as Catalog);
  vi.mocked(fetchSkillProposals).mockResolvedValue({ mode: "off", proposals: [] });
  vi.mocked(fetchContextBudget).mockResolvedValue({
    limit: 8000,
    counts_plugin_skills: false,
    roles: {
      planner: { chars: 1, skills: 1, over: false },
      worker: { chars: 1, skills: 1, over: false },
      reviewer: { chars: 1, skills: 1, over: false },
    },
  });
  rolledBack.mockClear();
  host = document.createElement("div");
  document.body.appendChild(host);
  root = createRoot(host);
});

afterEach(() => {
  act(() => root.unmount());
  host.remove();
  vi.clearAllMocks();
});

describe("SkillHistory", () => {
  it("lists the version, the author, the reason and the time, and the current version has no rollback", async () => {
    await renderHistory();
    expect(host.textContent).toContain("Version 1");
    expect(host.textContent).toContain("Operator");
    expect(host.textContent).toContain("imported");
    expect(host.textContent).toContain("Rollback from version 1");
    const times = [...host.querySelectorAll("time")].map((node) => node.getAttribute("datetime"));
    expect(times).toEqual(["2026-01-01T00:00:00Z", "2026-01-02T00:00:00Z"]);
    const rollbacks = buttons().filter((button) => button.getAttribute("aria-label")?.startsWith("Roll back to"));
    expect(rollbacks.map((button) => button.getAttribute("aria-label"))).toEqual(["Roll back to version 1"]);
    expect(host.textContent).toContain("Current");
  });

  it("shows the difference and the installed projects before it writes the rollback", async () => {
    await renderHistory();
    const rollBack = buttons().find((button) => button.getAttribute("aria-label") === "Roll back to version 1")!;
    await act(async () => rollBack.click());
    expect(rollbackProjectSkill).not.toHaveBeenCalled();
    expect(fetchSkillVersions).toHaveBeenCalledWith("proj-1", "skill-1", { fromVersion: 2, toVersion: 1 });
    const pre = host.querySelector("pre")!;
    expect(pre.textContent).toContain("--- version 2");
    expect(pre.textContent).toContain("+++ version 1");
    expect(pre.textContent).toContain("-two");
    expect(pre.textContent).toContain("+one");
    expect(host.textContent).toContain("New desc → Old desc");
    expect(pre.className).toContain("overflow-auto");
    expect(pre.className).toContain("break-words");
    expect(host.textContent).toContain("Old desc");
    expect(host.textContent).toContain("Alpha");
    const confirm = buttons().find((button) => labelOf(button) === "Confirm rollback")!;
    await act(async () => confirm.click());
    expect(rollbackProjectSkill).toHaveBeenCalledWith("proj-1", "skill-1", 1);
    expect(rolledBack).toHaveBeenCalledOnce();
  });

  it("says when no project installs the skill", async () => {
    vi.mocked(fetchSkillVersions).mockImplementation(async (_projectId, _skillId, range) => (
      range ? ranged(range.fromVersion, range.toVersion, []) : history()
    ));
    await renderHistory();
    await act(async () => buttons().find((button) => labelOf(button) === "Roll back")!.click());
    expect(host.textContent).toContain("No project installs this skill.");
    expect(rollbackProjectSkill).not.toHaveBeenCalled();
  });

  it("says when the skill has no saved versions", async () => {
    vi.mocked(fetchSkillVersions).mockResolvedValue({ ...history(), current_version: null, versions: [], diff: null });
    await renderHistory();
    expect(host.textContent).toContain("This skill has no saved versions.");
    expect(buttons().some((button) => labelOf(button) === "Roll back")).toBe(false);
  });

  it("shows a fetch error", async () => {
    vi.mocked(fetchSkillVersions).mockRejectedValue(new Error("cannot load versions"));
    await renderHistory();
    expect(host.querySelector("[role=alert]")?.textContent).toContain("cannot load versions");
  });
});

describe("ProjectSkillsSection history", () => {
  it("offers History only for a skill written in this project", async () => {
    await act(async () => {
      root.render(<ProjectSkillsSection projectId="proj-1" />);
    });
    const historyButtons = buttons().filter((button) => labelOf(button) === "History");
    expect(historyButtons).toHaveLength(1);
    expect(historyButtons[0].getAttribute("aria-expanded")).toBe("false");
    await act(async () => historyButtons[0].click());
    expect(host.textContent).toContain("Operator");
    expect(host.textContent).toContain("Version 1");
  });
});
