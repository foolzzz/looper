// @vitest-environment happy-dom
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { ProjectSkillsSection } from "@/components/project-skills";
import {
  deprecateProjectSkill,
  fetchCatalog,
  fetchContextBudget,
  fetchProjectInstalls,
  fetchSkillVersions,
  restoreProjectSkill,
  type Catalog,
  type CatalogItem,
  type ContextBudget,
  type ProjectInstall,
  type ProjectInstalls,
  type SkillVersions,
} from "@/lib/api";

vi.mock("@/lib/api", async (original) => ({
  ...await original<typeof import("@/lib/api")>(),
  fetchProjectInstalls: vi.fn(),
  fetchCatalog: vi.fn(),
  fetchContextBudget: vi.fn(),
  fetchSkillVersions: vi.fn(),
  fetchProjectSkill: vi.fn(),
  deprecateProjectSkill: vi.fn(),
  restoreProjectSkill: vi.fn(),
}));
(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

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
    lifecycle: "active",
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
    roles: ["planner", "worker", "reviewer"],
    enabled: true,
    created_at: "2026-01-01T00:00:00Z",
    updated_at: "2026-01-02T00:00:00Z",
    inherited: false,
    ...over,
  };
}

const versions = (places: SkillVersions["affected_projects"]): SkillVersions => ({
  skill_id: "skill-1",
  current_version: 1,
  versions: [],
  affected_projects: places,
  diff: null,
});

function budget(over: Partial<Record<"planner" | "worker" | "reviewer", boolean>> = {}): ContextBudget {
  return {
    limit: 8000,
    counts_plugin_skills: false,
    roles: {
      planner: { chars: 120, skills: 1, over: over.planner ?? false },
      worker: { chars: 80, skills: 1, over: over.worker ?? false },
      reviewer: { chars: 40, skills: 1, over: over.reviewer ?? false },
    },
  };
}

function view(): ProjectInstalls {
  return {
    project_id: "proj-1",
    owned_count: 3,
    task_roles: ["planner", "worker", "reviewer"],
    skill_caps: { name_max_chars: 64, description_max_chars: 1024, content_max_bytes: 65536, project_max_skills: 20 },
    installs: [
      install({ item: item({}) }),
      install({
        install_id: "inst-off",
        enabled: false,
        item: item({
          item_id: "skill-off", name: "gate", qualified_name: "project:gate", description: "A disabled install.",
        }),
      }),
      install({
        install_id: "inst-old",
        item: item({
          item_id: "skill-old", name: "old-check", qualified_name: "project:old-check",
          description: "Skip this one.", lifecycle: "deprecated",
        }),
      }),
      install({
        install_id: "inst-other",
        item: item({
          item_id: "skill-other", name: "elsewhere", qualified_name: "project:elsewhere",
          owner_project_id: "proj-other", owner_project_name: "Other",
        }),
      }),
      install({
        install_id: "inst-plugin",
        inherited: true,
        scope: "global",
        project_id: null,
        project_name: null,
        item: item({
          item_id: "plugin-1", kind: "plugin", name: "tools", qualified_name: "tools", origin: "machine",
          owner_project_id: null, owner_project_name: null, description: "Tools plugin.", lifecycle: "active",
        }),
      }),
    ],
  };
}

let root: Root;
let host: HTMLDivElement;
const buttons = () => [...host.querySelectorAll("button")];
const labelOf = (button: Element) => (button.textContent ?? "").trim();
const opener = (name: string) => buttons().find((button) => button.getAttribute("aria-label") === name);

async function renderSection() {
  await act(async () => {
    root.render(<ProjectSkillsSection projectId="proj-1" />);
  });
}

function typeReason(value: string) {
  const field = host.querySelector<HTMLInputElement>("input[aria-label='Reason for deprecating deploy-check']")!;
  act(() => {
    Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value")!.set!.call(field, value);
    field.dispatchEvent(new Event("input", { bubbles: true }));
  });
}

beforeEach(() => {
  vi.mocked(fetchProjectInstalls).mockResolvedValue(view());
  vi.mocked(fetchCatalog).mockResolvedValue({ items: [] } as unknown as Catalog);
  vi.mocked(fetchContextBudget).mockResolvedValue(budget());
  vi.mocked(fetchSkillVersions).mockResolvedValue(versions([{ project_id: "proj-1", project_name: "Calc" }]));
  vi.mocked(deprecateProjectSkill).mockResolvedValue({} as never);
  vi.mocked(restoreProjectSkill).mockResolvedValue({} as never);
  host = document.createElement("div");
  document.body.appendChild(host);
  root = createRoot(host);
});

afterEach(() => {
  act(() => root.unmount());
  host.remove();
  vi.clearAllMocks();
});

describe("ProjectSkillsSection lifecycle", () => {
  it("names each skill's state and keeps a deprecated skill listed", async () => {
    await renderSection();
    expect(host.textContent).toContain("project:deploy-check");
    expect(host.textContent).toContain("project:old-check");
    expect(host.textContent).toContain("Active");
    expect(host.textContent).toContain("Disabled in this project");
    expect(host.textContent).toContain("Deprecated");
    expect(host.textContent).not.toContain("Active in this project");
    expect(opener("Deprecate deploy-check")).toBeTruthy();
    expect(opener("Deprecate gate")).toBeTruthy();
    expect(opener("Deprecate elsewhere")).toBeUndefined();
    expect(opener("Deprecate old-check")).toBeUndefined();
    expect(opener("Restore old-check")).toBeTruthy();
    expect(opener("Restore deploy-check")).toBeUndefined();
    expect(opener("Deprecate tools")).toBeUndefined();
  });

  it("confirms deprecation before it writes, then refetches installs and the budget", async () => {
    vi.mocked(deprecateProjectSkill).mockImplementation(async () => {
      const next = view();
      next.installs[0].item = { ...next.installs[0].item, lifecycle: "deprecated" };
      vi.mocked(fetchProjectInstalls).mockResolvedValue(next);
      return {} as never;
    });
    await renderSection();
    await act(async () => opener("Deprecate deploy-check")!.click());
    expect(fetchSkillVersions).toHaveBeenCalledWith("proj-1", "skill-1");
    expect(deprecateProjectSkill).not.toHaveBeenCalled();
    expect(host.textContent).toContain(
      "Deprecate this skill? New runs skip it. Installs, versions and files stay.");
    expect(host.textContent).not.toContain("Installed in");
    typeReason("  too broad  ");
    const confirm = buttons().find((button) => labelOf(button) === "Deprecate" && !button.getAttribute("aria-label"))!;
    await act(async () => confirm.click());
    expect(deprecateProjectSkill).toHaveBeenCalledWith("proj-1", "skill-1", "too broad");
    expect(fetchProjectInstalls).toHaveBeenCalledTimes(2);
    expect(vi.mocked(fetchContextBudget).mock.calls.length).toBeGreaterThanOrEqual(2);
    expect(host.textContent).toContain("Deprecated");
    expect(opener("Deprecate deploy-check")).toBeUndefined();
  });

  it("names the other projects before deprecating a skill installed in more than one", async () => {
    vi.mocked(fetchSkillVersions).mockResolvedValue(versions([
      { project_id: "proj-1", project_name: "Calc" },
      { project_id: "proj-2", project_name: "Docs" },
    ]));
    await renderSection();
    await act(async () => opener("Deprecate deploy-check")!.click());
    expect(host.textContent).toContain("Installed in Calc, Docs.");
    expect(deprecateProjectSkill).not.toHaveBeenCalled();
  });

  it("shows a versions-read error and does not deprecate", async () => {
    vi.mocked(fetchSkillVersions).mockRejectedValue(new Error("cannot read versions"));
    await renderSection();
    await act(async () => opener("Deprecate deploy-check")!.click());
    expect(host.querySelector("[role=alert]")?.textContent).toContain("cannot read versions");
    expect(host.textContent).not.toContain("Deprecate this skill?");
    expect(deprecateProjectSkill).not.toHaveBeenCalled();
  });

  it("confirms restore, then refetches", async () => {
    vi.mocked(restoreProjectSkill).mockImplementation(async () => {
      const next = view();
      next.installs[2].item = { ...next.installs[2].item, lifecycle: "active" };
      vi.mocked(fetchProjectInstalls).mockResolvedValue(next);
      return {} as never;
    });
    await renderSection();
    await act(async () => opener("Restore old-check")!.click());
    expect(restoreProjectSkill).not.toHaveBeenCalled();
    expect(host.textContent).toContain("Restore this skill? New runs load it again.");
    const confirm = buttons().find((button) => labelOf(button) === "Restore" && !button.getAttribute("aria-label"))!;
    await act(async () => confirm.click());
    expect(restoreProjectSkill).toHaveBeenCalledWith("proj-1", "skill-old");
    expect(fetchProjectInstalls).toHaveBeenCalledTimes(2);
    expect(vi.mocked(fetchContextBudget).mock.calls.length).toBeGreaterThanOrEqual(2);
    expect(opener("Restore old-check")).toBeUndefined();
    expect(opener("Deprecate old-check")).toBeTruthy();
  });

  it("shows the budget line when every role is under the display threshold", async () => {
    await renderSection();
    expect(host.textContent).toContain(
      "Each run lists 120 / 80 / 40 characters of skill descriptions (planner / worker / reviewer).");
    expect(host.textContent).toContain("Plugin-bundled skills are not counted.");
    expect(host.textContent).not.toContain("over 8000");
  });

  it("flags one role that is over 8000 characters", async () => {
    vi.mocked(fetchContextBudget).mockResolvedValue(budget({ planner: true }));
    await renderSection();
    expect(host.textContent).toContain("planner is over 8000 characters.");
  });

  it("flags every role that is over 8000 characters", async () => {
    vi.mocked(fetchContextBudget).mockResolvedValue(budget({ planner: true, worker: true, reviewer: true }));
    await renderSection();
    expect(host.textContent).toContain("planner, worker and reviewer are over 8000 characters.");
  });

  it("shows a failed budget read and does not invent a zero count", async () => {
    vi.mocked(fetchContextBudget).mockRejectedValue(new Error("budget unavailable"));
    await renderSection();
    expect(host.querySelector("[role=alert]")?.textContent).toContain("budget unavailable");
    expect(host.textContent).not.toContain("Each run lists");
    expect(host.textContent).not.toContain("0 / 0 / 0");
  });

  it("wraps the skills tab at 390 px", async () => {
    host.style.width = "390px";
    host.style.maxWidth = "390px";
    await renderSection();
    const section = host.querySelector("section")!;
    expect(section.className).toContain("min-w-0");
    expect(host.querySelectorAll(".break-words").length).toBeGreaterThan(0);
    expect(host.querySelectorAll(".flex-wrap").length).toBeGreaterThan(0);
    expect(host.textContent).toContain("Deprecated");
    expect(host.textContent).toContain("Each run lists");
    if (section.scrollWidth > 0 && section.clientWidth > 0) {
      expect(section.scrollWidth).toBeLessThanOrEqual(section.clientWidth);
    }
  });
});
