// @vitest-environment happy-dom
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { Sidebar } from "@/components/sidebar";
import { fetchGates, fetchOverview, fetchProjects, type GateList } from "@/lib/api";

vi.mock("@/lib/api", async (original) => ({
  ...await original<typeof import("@/lib/api")>(),
  fetchGates: vi.fn(), fetchProjects: vi.fn(), fetchOverview: vi.fn(),
}));
vi.mock("@/components/sign-out", () => ({ SignOut: () => null }));
vi.mock("@/components/project-dialog", () => ({ ProjectDialog: () => null }));
(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

const gates = (waiting: number): GateList => ({
  gates: [], total: 0, limit: 1, offset: 0, next_cursor: null, has_more: false,
  counts: { open: waiting, waiting_user: waiting, waiting_planner: 0, closed: 0, all: waiting },
});

let root: Root;
let host: HTMLElement;
const onNavigate = vi.fn();

let activePage: "gates" | "dashboard" = "gates";

async function render() {
  await act(async () => {
    root.render(
      <Sidebar activePage={activePage} activeSettingsPage={null} activeProjectId={null} onNavigate={onNavigate}
        onOpenProject={vi.fn()} onOpenSettings={vi.fn()} open onClose={vi.fn()} />,
    );
  });
}
const gatesButton = () => [...host.querySelectorAll("nav button")].find((b) => b.textContent?.includes("User Gates"))!;

beforeEach(() => {
  vi.useFakeTimers();
  vi.mocked(fetchProjects).mockResolvedValue([]);
  vi.mocked(fetchGates).mockResolvedValue(gates(2));
  onNavigate.mockClear();
  vi.mocked(fetchOverview).mockResolvedValue({} as never);
  activePage = "gates";
  host = document.createElement("div");
  document.body.appendChild(host);
  root = createRoot(host);
});
afterEach(() => {
  act(() => root.unmount());
  host.remove();
  vi.useRealTimers();
  vi.clearAllMocks();
});

describe("Sidebar User Gates item", () => {
  it("sits between Tasks and Runs, is active and navigates", async () => {
    await render();
    const labels = [...host.querySelectorAll("nav")[0].querySelectorAll("button")].map((b) => b.textContent ?? "");
    const at = (name: string) => labels.findIndex((l) => l.includes(name));
    expect(at("Tasks")).toBeGreaterThanOrEqual(0);
    expect(at("User Gates")).toBe(at("Tasks") + 1);
    expect(at("Runs")).toBe(at("User Gates") + 1);
    expect(gatesButton().className).toContain("text-primary");
    act(() => (gatesButton() as HTMLButtonElement).click());
    expect(onNavigate).toHaveBeenCalledWith("gates");
  });

  it("shows the waiting count as a badge", async () => {
    await render();
    expect(host.querySelector('[aria-label="2 gates waiting for you"]')?.textContent).toBe("2");
  });

  it("uses the singular for one", async () => {
    vi.mocked(fetchGates).mockResolvedValue(gates(1));
    await render();
    expect(host.querySelector('[aria-label="1 gate waiting for you"]')?.textContent).toBe("1");
  });

  it("caps the badge at 99+", async () => {
    vi.mocked(fetchGates).mockResolvedValue(gates(150));
    await render();
    expect(host.querySelector('[aria-label="150 gates waiting for you"]')?.textContent).toBe("99+");
  });

  it("shows no badge for 0", async () => {
    vi.mocked(fetchGates).mockResolvedValue(gates(0));
    await render();
    expect(host.querySelector('[aria-label$="waiting for you"]')).toBeNull();
  });

  it("shows no badge when the fetch fails", async () => {
    vi.mocked(fetchGates).mockRejectedValue(new Error("down"));
    await render();
    expect(host.querySelector('[aria-label$="waiting for you"]')).toBeNull();
  });

  it("polls every 10 s, reloads on the gates event and stops after unmount", async () => {
    await render();
    expect(fetchGates).toHaveBeenCalledWith({ state: "open", waitingOn: "user", limit: 1 });
    expect(fetchGates).toHaveBeenCalledTimes(1);
    await act(async () => { await vi.advanceTimersByTimeAsync(10_000); });
    expect(fetchGates).toHaveBeenCalledTimes(2);
    await act(async () => { window.dispatchEvent(new Event("looper:gates-changed")); });
    expect(fetchGates).toHaveBeenCalledTimes(3);
    act(() => root.unmount());
    await vi.advanceTimersByTimeAsync(30_000);
    window.dispatchEvent(new Event("looper:gates-changed"));
    expect(fetchGates).toHaveBeenCalledTimes(3);
    root = createRoot(host);
  });
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
