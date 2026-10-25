// @vitest-environment happy-dom
import { afterEach, expect, it, vi } from "vitest";
import { createAgentProfile, updateAgentProfile, deleteAgentProfile, fetchPromptPreview,
  REGISTRY_CHANGED_EVENT, type AgentRole } from "@/lib/api";
import { resetSessionGuard } from "@/lib/session";

afterEach(() => { resetSessionGuard(); vi.unstubAllGlobals(); });
const id = "profile /?&中文";
const fields = { name: "Profile", runtime_id: "rt-codex", model_id: "model-one" };

it.each([
  [() => createAgentProfile(fields), "create", fields],
  [() => updateAgentProfile(id, { effort: null }), "update", { profile_id: id, fields: { effort: null } }],
  [() => deleteAgentProfile(id), "delete", { profile_id: id }],
] as const)("preserves results and refreshes profile selectors for %s", async (call, action, body) => {
  const result = action === "delete" ? { deleted: id } : { profile: { profile_id: id }, warnings: ["Missing login"] };
  const fetcher = vi.fn(async () => Response.json({ status: "ok", result }));
  vi.stubGlobal("fetch", fetcher);
  const changed = vi.fn();
  window.addEventListener(REGISTRY_CHANGED_EVENT, changed);
  try {
    await expect(call()).resolves.toEqual(result);
    expect(fetcher).toHaveBeenCalledExactlyOnceWith(`/api/v1/profiles/${action}`,
      expect.objectContaining({ method: "POST", body: JSON.stringify(body) }));
    expect(changed).toHaveBeenCalledTimes(1);
  } finally { window.removeEventListener(REGISTRY_CHANGED_EVENT, changed); }
});

it("sends preview identifiers and role as JSON without a registry mutation", async () => {
  const role = "worker /?&" as AgentRole, project = "project /?&中文";
  const result = { profile: { id, name: "Preview" }, blocks: [{ text: null }] };
  const fetcher = vi.fn(async () => Response.json({ status: "ok", result }));
  vi.stubGlobal("fetch", fetcher);
  const changed = vi.fn();
  window.addEventListener(REGISTRY_CHANGED_EVENT, changed);
  try {
    await expect(fetchPromptPreview(id, role, project)).resolves.toEqual(result);
    expect(fetcher).toHaveBeenCalledExactlyOnceWith("/api/v1/profiles/prompt_preview", expect.objectContaining({
      body: JSON.stringify({ profile_id: id, role, project_id: project }),
    }));
    expect(changed).not.toHaveBeenCalled();
  } finally { window.removeEventListener(REGISTRY_CHANGED_EVENT, changed); }
});

it.each([202, 409])("does not announce deletion or confirm automatically after HTTP %s", async (status) => {
  const answer = status === 202 ? { status: "needs_confirmation", confirmation_id: "pending", impact: "Delete?" }
    : { status: "error", error: { code: "conflict", message: "Profile is selected" } };
  const fetcher = vi.fn(async () => Response.json(answer, { status }));
  vi.stubGlobal("fetch", fetcher);
  const changed = vi.fn();
  window.addEventListener(REGISTRY_CHANGED_EVENT, changed);
  try {
    await expect(deleteAgentProfile(id)).rejects.toMatchObject({ name: "V1Error", status });
    expect(changed).not.toHaveBeenCalled();
    expect(fetcher).toHaveBeenCalledTimes(1);
  } finally { window.removeEventListener(REGISTRY_CHANGED_EVENT, changed); }
});
