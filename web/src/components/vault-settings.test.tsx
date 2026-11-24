// @vitest-environment happy-dom
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { VaultSettingsSection } from "@/components/vault-settings";
import { executionChanges, executionDraft, ProjectExecutionFields } from "@/components/project-execution";
import { SettingsPage } from "@/pages/settings";
import { SETTINGS_PAGES, settingsHref } from "@/lib/settings-pages";
import type { Project, VaultSettings } from "@/lib/api";

vi.mock("@/lib/api", async (original) => ({
  ...await original<typeof import("@/lib/api")>(), fetchWorkflows: vi.fn(async () => []),
}));
(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

const EMPTY: VaultSettings = {
  configured: false, path: null, writable: [], rules_file: null, profile_source: "关于我.md", git: false, problem: null,
};
const CONFIGURED: VaultSettings = {
  configured: true, path: "/srv/notes", writable: ["codex-mem"], rules_file: "codex-mem/AGENTS.md", profile_source: "关于我.md", git: true, problem: null,
};

describe("Knowledge vault settings", () => {
  let container: HTMLDivElement;
  let root: Root;
  let settings: VaultSettings;
  let put: (body: unknown) => Promise<Response>;
  let fetchMock: ReturnType<typeof vi.fn>;

  beforeEach(() => {
    settings = { ...EMPTY };
    put = async () => Response.json({ status: "ok", result: CONFIGURED });
    fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      if (init?.method !== "POST") throw new Error(`Unexpected method: ${init?.method}`);
      if (String(input) === "/api/v1/profile_settings/get") return Response.json({ status: "ok", result: { enabled: false } });
      if (String(input) === "/api/v1/profile_settings/update") return Response.json({ status: "ok", result: JSON.parse(String(init.body)) });
      if (String(input) === "/api/v1/knowledge_vault/update") return put(JSON.parse(String(init.body)));
      if (String(input) === "/api/v1/knowledge_vault/get") return Response.json({ status: "ok", result: settings });
      throw new Error(`Unexpected request: ${input}`);
    });
    vi.stubGlobal("fetch", fetchMock);
    container = document.createElement("div");
    document.body.append(container);
    root = createRoot(container);
  });

  afterEach(() => {
    act(() => root.unmount());
    container.remove();
    vi.useRealTimers();
    vi.unstubAllGlobals();
  });

  const render = async () => { await act(async () => root.render(<VaultSettingsSection />)); };
  const field = (label: string) => {
    const node = [...container.querySelectorAll("label")].find((item) => item.textContent === label)!;
    return document.getElementById(node.htmlFor) as HTMLInputElement;
  };
  const edit = async (label: string, value: string) => {
    await act(async () => {
      const input = field(label);
      Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value")!.set!.call(input, value);
      input.dispatchEvent(new Event("input", { bubbles: true }));
    });
  };
  const submit = async () => {
    await act(async () => {
      container.querySelector("form")!.dispatchEvent(new Event("submit", { bubbles: true, cancelable: true }));
    });
  };
  const button = (label: string) => [...container.querySelectorAll("button")].find((node) => node.textContent === label)!;
  const sent = () => fetchMock.mock.calls.find(([input]) => String(input) === "/api/v1/knowledge_vault/update")![1] as RequestInit;

  it("shows loading until the request finishes", async () => {
    let finish!: (value: Response) => void;
    fetchMock.mockImplementationOnce(() => new Promise<Response>((resolve) => { finish = resolve; }));
    await render();
    expect(container.textContent).toContain("Loading…");
    await act(async () => finish(Response.json({ status: "ok", result: EMPTY })));
    expect(container.textContent).toContain("Not configured: agents have no vault.");
  });

  it("renders the not-configured state and connects every field to its help", async () => {
    await render();
    expect(container.textContent).toContain("Not configured: agents have no vault.");
    expect(button("Turn off")).toBeUndefined();
    for (const label of ["Vault folder", "Writable folders", "Rules file"]) {
      const input = field(label);
      expect(input.value).toBe("");
      expect(document.getElementById(input.getAttribute("aria-describedby")!)?.textContent).toBeTruthy();
    }
    expect(container.querySelector('[role="status"] svg')).not.toBeNull();
  });

  it.each([
    [true, "Configured: a git repository; each write is a commit."],
    [false, "Configured: not a git repository; writes are not committed."],
  ])("shows configured status with git=%s and initializes the fields", async (git, status) => {
    settings = { ...CONFIGURED, git, writable: ["codex-mem", "notes"] };
    await render();
    expect(container.textContent).toContain(status);
    expect(field("Vault folder").value).toBe("/srv/notes");
    expect(field("Writable folders").value).toBe("codex-mem, notes");
    expect(field("Rules file").value).toBe("codex-mem/AGENTS.md");
    expect(button("Turn off")).toBeDefined();
  });

  it("shows an invalid stored configuration with an alert icon and keeps it editable", async () => {
    settings = { ...CONFIGURED, configured: false, problem: "The vault folder no longer exists." };
    await render();
    const alert = container.querySelector('[role="alert"]')!;
    expect(alert.textContent).toBe(settings.problem);
    expect(alert.classList.contains("text-destructive")).toBe(true);
    expect(alert.querySelector("svg")).not.toBeNull();
    expect(field("Vault folder").value).toBe("/srv/notes");
    expect(field("Vault folder").disabled).toBe(false);
  });

  it("retries a failed load", async () => {
    fetchMock.mockResolvedValueOnce(Response.json({ status: "error", error: { code: "unavailable", message: "Settings unavailable" } }, { status: 503 }));
    await render();
    expect(container.querySelector('[role="alert"]')?.textContent).toBe("Settings unavailable");
    await act(async () => button("Retry").click());
    expect(container.textContent).toContain("Not configured: agents have no vault.");
    expect(fetchMock.mock.calls.filter(([url]) => String(url).includes("knowledge_vault"))).toHaveLength(2);
  });

  it("saves parsed folders and empty rules, then uses the returned form values", async () => {
    await render();
    await edit("Vault folder", " /srv/notes/ ");
    await edit("Writable folders", "codex-mem, notes,");
    await submit();
    expect(JSON.parse(String(sent().body))).toEqual({ path: "/srv/notes/", writable: ["codex-mem", "notes"], rules_file: null });
    expect(sent().headers).toMatchObject({ "Content-Type": "application/json" });
    expect(field("Vault folder").value).toBe(CONFIGURED.path);
    expect(field("Writable folders").value).toBe("codex-mem");
    expect(field("Rules file").value).toBe(CONFIGURED.rules_file);
    expect(container.textContent).toContain("Saved.");
  });

  it("sends an empty folder list for read only access and trims the rules file", async () => {
    await render();
    await edit("Vault folder", "/srv/notes");
    await edit("Writable folders", " , , ");
    await edit("Rules file", " notes/rules.md ");
    await submit();
    expect(JSON.parse(String(sent().body))).toEqual({ path: "/srv/notes", writable: [], rules_file: "notes/rules.md" });
  });

  it("keeps the edited form and shows the server's 400 message", async () => {
    put = async () => Response.json({ status: "error", error: { code: "invalid", message: "path must be absolute" } }, { status: 400 });
    await render();
    await edit("Vault folder", "relative");
    await edit("Writable folders", "codex-mem, notes,");
    await submit();
    expect(container.querySelector('[role="alert"]')?.textContent).toBe("path must be absolute");
    expect(field("Vault folder").value).toBe("relative");
    expect(field("Writable folders").value).toBe("codex-mem, notes,");
    expect(button("Save").disabled).toBe(false);
    expect(container.textContent).not.toContain("Saved.");
  });

  it("saves the selected profile note and preserves the independent Manager read permission", async () => {
    settings = { ...CONFIGURED, profile_source: "notes/profile.md", manager_read: true };
    await render();
    expect(field("画像文件").value).toBe("notes/profile.md");
    await edit("画像文件", " notes/updated.md ");
    await submit();
    expect(JSON.parse(String(sent().body))).toEqual({
      path: "/srv/notes", writable: ["codex-mem"], rules_file: "codex-mem/AGENTS.md",
      profile_source: "notes/updated.md", manager_read: true,
    });
  });

  it("uses the default profile when the path is cleared", async () => {
    settings = { ...CONFIGURED, profile_source: "notes/profile.md" };
    await render();
    await edit("画像文件", "");
    await submit();
    expect(JSON.parse(String(sent().body))).not.toHaveProperty("profile_source");
  });

  it("confirms and saves the resident profile switch independently of the vault form", async () => {
    const confirm = vi.fn().mockReturnValueOnce(false).mockReturnValueOnce(true);
    vi.stubGlobal("confirm", confirm);
    await render();
    const toggle = container.querySelector<HTMLButtonElement>('[role="switch"][aria-label="管家常驻画像"]')!;
    expect(toggle.getAttribute("aria-checked")).toBe("false");
    await act(async () => toggle.click());
    expect(fetchMock.mock.calls.some(([input]) => String(input).endsWith("profile_settings/update"))).toBe(false);
    await act(async () => toggle.click());
    expect(toggle.getAttribute("aria-checked")).toBe("true");
    const request = fetchMock.mock.calls.find(([input]) => String(input).endsWith("profile_settings/update"))![1];
    expect(JSON.parse(String(request?.body))).toEqual({ enabled: true });
    expect(fetchMock.mock.calls.some(([input]) => String(input).endsWith("knowledge_vault/update"))).toBe(false);
  });

  it("turns off the vault with only path=null and clears the form", async () => {
    settings = CONFIGURED;
    put = async () => Response.json({ status: "ok", result: EMPTY });
    await render();
    await act(async () => button("Turn off").click());
    expect(JSON.parse(String(sent().body))).toEqual({ path: null });
    expect(container.textContent).toContain("Not configured: agents have no vault.");
    expect(field("Vault folder").value).toBe("");
    expect(field("Writable folders").value).toBe("");
    expect(field("Rules file").value).toBe("");
    expect(button("Turn off")).toBeUndefined();
  });

  it("disables both actions while saving", async () => {
    settings = CONFIGURED;
    let finish!: (value: Response) => void;
    put = () => new Promise<Response>((resolve) => { finish = resolve; });
    await render();
    await submit();
    expect(button("Saving…").disabled).toBe(true);
    expect(button("Turn off").disabled).toBe(true);
    await act(async () => finish(Response.json({ status: "ok", result: CONFIGURED })));
    expect(button("Save").disabled).toBe(false);
    expect(button("Turn off").disabled).toBe(false);
  });

  it("removes the saved notice after a short time", async () => {
    vi.useFakeTimers();
    await render();
    await submit();
    expect(container.textContent).toContain("Saved.");
    await act(async () => vi.advanceTimersByTime(2000));
    expect(container.textContent).not.toContain("Saved.");
  });

  it("opens the vault page listed after scheduler and before recording, gateway and transfer", async () => {
    const index = SETTINGS_PAGES.findIndex((page) => page.id === "vault");
    expect(SETTINGS_PAGES.slice(index - 1, index + 4).map((page) => page.id)).toEqual(["scheduler", "vault", "recording", "gateway", "transfer"]);
    await act(async () => root.render(<SettingsPage sub={settingsHref("vault").split("/")[1]} />));
    expect(container.querySelector("h1")?.textContent).toBe("Knowledge vault");
    expect(container.textContent).toContain("A local folder of Markdown notes");
    expect(container.textContent).toContain("Not configured: agents have no vault.");
  });

  it.each([null, "off", "on"] as const)("round trips the project switch when saved as %s", async (vault) => {
    const project = { vault } as Project;
    const draft = executionDraft(project);
    let changed = draft;
    await act(async () => root.render(<ProjectExecutionFields idPrefix="project" draft={draft}
                                                             onChange={(value) => { changed = value; }} />));
    const select = container.querySelector<HTMLSelectElement>("#project-vault")!;
    expect(select.value).toBe(vault || "off");
    expect(container.querySelector('label[for="project-vault"]')?.textContent).toBe("Knowledge vault");
    expect(document.getElementById(select.getAttribute("aria-describedby")!)?.textContent).toContain("off by default");
    expect(executionChanges(draft, project)).toEqual({});
    await act(async () => {
      select.value = vault === "on" ? "off" : "on";
      select.dispatchEvent(new Event("change", { bubbles: true }));
    });
    expect(executionChanges(changed, project)).toEqual({ vault: vault === "on" ? "off" : "on" });
  });

  it.each([null, "off", "on"] as const)("round trips the advisor switch, its limits shown only when on (%s)", async (advisor) => {
    const project = { advisor, advisor_consults_per_step: 3 } as Project;
    let draft = executionDraft(project);
    await act(async () => root.render(<ProjectExecutionFields idPrefix="project" draft={draft}
      onChange={(value) => { draft = value; }} />));
    const select = container.querySelector<HTMLSelectElement>("#project-advisor")!;
    expect(select.value).toBe(advisor || "off");
    expect(document.getElementById(select.getAttribute("aria-describedby")!)?.textContent).toContain("off by default");
    expect(Boolean(container.querySelector("#project-advisor_consults_per_step"))).toBe(advisor === "on");
    expect(executionChanges(draft, project)).toEqual({});
    await act(async () => {
      select.value = advisor === "on" ? "off" : "on";
      select.dispatchEvent(new Event("change", { bubbles: true }));
    });
    await act(async () => root.render(<ProjectExecutionFields idPrefix="project" draft={draft}
      onChange={(value) => { draft = value; }} />));
    expect(executionChanges(draft, project)).toEqual({ advisor: advisor === "on" ? "off" : "on" });
    const limit = container.querySelector<HTMLInputElement>("#project-advisor_consults_per_step");
    expect(Boolean(limit)).toBe(advisor !== "on");
    if (!limit) return;
    expect(limit.value).toBe("3");
    await act(async () => {
      Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value")!.set!.call(limit, "");
      limit.dispatchEvent(new Event("input", { bubbles: true }));
    });
    expect(executionChanges(draft, project)).toEqual({ advisor: "on", advisor_consults_per_step: null });
  });

  it.each([null, "off", "on"] as const)("round trips dependency conditions, off by default (%s)", async (dependency_conditions) => {
    const project = { dependency_conditions } as Project;
    let draft = executionDraft(project);
    await act(async () => root.render(<ProjectExecutionFields idPrefix="project" draft={draft}
      onChange={(value) => { draft = value; }} />));
    const control = container.querySelector<HTMLButtonElement>('[role="switch"][aria-label="Dependency conditions"]')!;
    expect(control.getAttribute("aria-checked")).toBe(String(dependency_conditions === "on"));
    expect(executionChanges(draft, project)).toEqual({});
    await act(async () => control.click());
    expect(executionChanges(draft, project)).toEqual({ dependency_conditions: dependency_conditions === "on" ? "off" : "on" });
  });

  it.each([null, "off", "on"] as const)("round trips wait commands, off by default (%s)", async (wait_commands) => {
    const project = { wait_commands } as Project;
    let draft = executionDraft(project);
    await act(async () => root.render(<ProjectExecutionFields idPrefix="project" draft={draft}
      onChange={(value) => { draft = value; }} />));
    const select = container.querySelector<HTMLSelectElement>("#project-wait_commands")!;
    expect(select.value).toBe(wait_commands || "off");
    expect(document.getElementById(select.getAttribute("aria-describedby")!)?.textContent).toContain("server's user");
    expect(executionChanges(draft, project)).toEqual({});
    await act(async () => {
      select.value = wait_commands === "on" ? "off" : "on";
      select.dispatchEvent(new Event("change", { bubbles: true }));
    });
    expect(executionChanges(draft, project)).toEqual({ wait_commands: wait_commands === "on" ? "off" : "on" });
  });
});
