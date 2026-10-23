// @vitest-environment happy-dom
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { ApiClientsSection } from "@/components/api-clients";
import * as api from "@/lib/api";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

vi.mock("@/lib/api", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/lib/api")>()),
  fetchApiClients: vi.fn(),
  createApiClient: vi.fn(),
  revokeApiClient: vi.fn(),
}));

const m = vi.mocked(api);

// Not a token any server issued.
const TOKEN = "lpc_" + "x".repeat(43);

const LAPTOP: api.ApiClient = {
  id: "client-aaaaaaaaaaaa", name: "laptop", created_at: "2026-10-01T08:00:00Z", last_used_at: "2026-10-20T09:30:00Z",
};
const CI: api.ApiClient = { id: "client-bbbbbbbbbbbb", name: "ci", created_at: "2026-10-02T08:00:00Z", last_used_at: null };

describe("API clients settings", () => {
  let container: HTMLDivElement;
  let root: Root;

  const render = async () => {
    await act(async () => root.render(<ApiClientsSection />));
  };
  const button = (text: string) =>
    Array.from(container.querySelectorAll("button")).find((b) => b.textContent?.includes(text))!;
  const click = (el: Element) => act(async () => (el as HTMLElement).click());
  const nameInput = () => container.querySelector("input") as HTMLInputElement;
  const type = async (el: HTMLInputElement, value: string) => {
    await act(async () => {
      Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value")!.set!.call(el, value);
      el.dispatchEvent(new Event("input", { bubbles: true }));
    });
  };
  const create = async (name: string) => {
    await type(nameInput(), name);
    await click(button("Create"));
  };

  beforeEach(() => {
    vi.clearAllMocks();
    m.fetchApiClients.mockResolvedValue([LAPTOP, CI]);
    m.createApiClient.mockResolvedValue({ client: { ...CI, id: "client-cccccccccccc", name: "new" }, token: TOKEN });
    m.revokeApiClient.mockResolvedValue(undefined);
    container = document.createElement("div");
    document.body.append(container);
    root = createRoot(container);
  });

  afterEach(() => {
    act(() => root.unmount());
    container.remove();
    vi.unstubAllGlobals();
  });

  it("lists the clients, with never used for one that has no last use", async () => {
    await render();
    expect(container.textContent).toContain("laptop");
    expect(container.textContent).toContain("ci");
    expect(container.textContent).toContain("created ");
    expect(container.textContent).toMatch(/last used (?!never)/);
    expect(container.textContent).toContain("never used");
    expect(container.textContent?.match(/never used/g)).toHaveLength(1);
  });

  it("shows the load error inline", async () => {
    m.fetchApiClients.mockRejectedValue(new Error("clients 500"));
    await render();
    expect(container.querySelector('[role="alert"]')?.textContent).toContain("clients 500");
  });

  it("creates a client, shows the token and the login command, and Done removes the token", async () => {
    await render();
    await create("  new ");
    expect(m.createApiClient).toHaveBeenCalledWith("new");
    const field = container.querySelector("input[readonly]") as HTMLInputElement;
    expect(field.value).toBe(TOKEN);
    expect(container.textContent).toContain(`loopercli login --url ${window.location.origin}`);
    expect(container.textContent).toContain("Paste the token when loopercli asks for it. It is not shown again.");
    await click(button("Done"));
    expect(container.querySelector("input[readonly]")).toBeNull();
    expect(container.innerHTML).not.toContain(TOKEN);
    expect(container.textContent).not.toContain("loopercli login");
  });

  it("copies the token and the command", async () => {
    const writeText = vi.fn(async () => {});
    vi.stubGlobal("navigator", { clipboard: { writeText } });
    await render();
    await create("new");
    const copies = Array.from(container.querySelectorAll("button")).filter((b) => b.textContent?.includes("Copy"));
    expect(copies).toHaveLength(2);
    await click(copies[0]);
    await click(copies[1]);
    expect(writeText).toHaveBeenNthCalledWith(1, TOKEN);
    expect(writeText).toHaveBeenNthCalledWith(2, `loopercli login --url ${window.location.origin}`);
  });

  it("shows a refusal of the name and no token box", async () => {
    m.createApiClient.mockRejectedValue(new Error("an API client named 'ci' exists; revoke it or pick another name"));
    await render();
    await create("ci");
    expect(container.querySelector('[role="alert"]')?.textContent).toContain("an API client named 'ci' exists");
    expect(container.querySelector("input[readonly]")).toBeNull();
    expect(container.textContent).not.toContain("loopercli login");
  });

  it("asks before it revokes, then revokes and reloads", async () => {
    const ask = vi.fn(() => true);
    vi.stubGlobal("confirm", ask);
    await render();
    m.fetchApiClients.mockResolvedValue([CI]);
    await click(container.querySelector('[aria-label="Revoke API client laptop"]')!);
    expect(ask).toHaveBeenCalledWith(
      "Revoke laptop? The token stops working at once; what it did stays recorded under its name.");
    expect(m.revokeApiClient).toHaveBeenCalledWith(LAPTOP.id);
    expect(m.fetchApiClients).toHaveBeenCalledTimes(2);
    expect(container.textContent).not.toContain("laptop");
  });

  it("revokes nothing when the question is cancelled", async () => {
    vi.stubGlobal("confirm", vi.fn(() => false));
    await render();
    await click(container.querySelector('[aria-label="Revoke API client laptop"]')!);
    expect(m.revokeApiClient).not.toHaveBeenCalled();
    expect(m.fetchApiClients).toHaveBeenCalledTimes(1);
  });
});
