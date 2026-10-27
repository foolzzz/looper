import { callV1, V1Error } from "@/lib/v1";

export interface ManagerInstall {
  item_id: string;
  kind: "plugin" | "skill";
  name: string;
  enabled: boolean;
  inherited: boolean;
  updated_at: string | null;
}

export interface ManagerInstallsSnapshot {
  manager_id: string;
  installs: ManagerInstall[];
}

async function request(action: "list" | "put" | "delete",
  body: { manager_id: string; item_id?: string; enabled?: boolean }, signal?: AbortSignal): Promise<ManagerInstallsSnapshot> {
  const result = await callV1<ManagerInstallsSnapshot>(`/api/v1/manager_installs/${action}`, body, { signal });
  if ("status" in result) throw new V1Error(202, "needs_confirmation", result.impact);
  if (result.manager_id !== body.manager_id) throw new Error("The server returned installs for another Manager.");
  return result;
}

export function fetchManagerInstalls(managerId: string, signal?: AbortSignal) {
  return request("list", { manager_id: managerId }, signal);
}

export function putManagerInstall(managerId: string, itemId: string, enabled?: boolean) {
  return request("put", { manager_id: managerId, item_id: itemId, ...(enabled === undefined ? {} : { enabled }) });
}

export function deleteManagerInstall(managerId: string, itemId: string) {
  return request("delete", { manager_id: managerId, item_id: itemId });
}
