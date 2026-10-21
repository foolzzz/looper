// @vitest-environment happy-dom
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { Sidebar } from "@/components/sidebar";
import { fetchOverview } from "@/lib/api";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

vi.mock("@/lib/api", () => ({
  fetchOverview: vi.fn(),
  fetchProjects: vi.fn(async () => []),
  PROJECTS_CHANGED_EVENT: "projects-changed",
}));
vi.mock("@/components/sign-out", () => ({ SignOut: () => null }));
vi.mock("@/components/project-dialog", () => ({ ProjectDialog: () => null }));

let root: Root;
let host: HTMLDivElement;

async function render() {
  await act(async () => {
    root.render(
      <Sidebar activePage="dashboard" activeSettingsPage={null} activeProjectId={null} onNavigate={() => {}}
        onOpenProject={() => {}} onOpenSettings={() => {}} open onClose={() => {}} />,
    );
  });
}

beforeEach(() => {
  host = document.createElement("div");
  document.body.appendChild(host);
  root = createRoot(host);
});
afterEach(() => {
  act(() => root.unmount());
  host.remove();
  vi.clearAllMocks();
});

describe("Sidebar version label", () => {
  it("shows the overview's version next to the brand", async () => {
    vi.mocked(fetchOverview).mockResolvedValue({ version: "2.13.1" } as never);
    await render();
    const label = host.querySelector('[title="2.13.1"]');
    expect(label?.textContent).toBe("v2.13.1");
    expect(label?.className).toContain("whitespace-nowrap");
  });

  it("shows nothing when the overview has no version", async () => {
    vi.mocked(fetchOverview).mockResolvedValue({} as never);
    await render();
    expect(host.textContent).toContain("Looper");
    expect(host.textContent).not.toMatch(/v\d/);
  });

  it("shows nothing while loading", async () => {
    vi.mocked(fetchOverview).mockReturnValue(new Promise(() => {}));
    await render();
    expect(host.textContent).not.toMatch(/v\d/);
  });
});
