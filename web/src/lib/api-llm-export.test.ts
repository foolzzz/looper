// @vitest-environment happy-dom
import { afterEach, expect, it, vi } from "vitest";
import { exportLlmRequests } from "@/lib/api";

afterEach(() => { vi.restoreAllMocks(); vi.unstubAllGlobals(); });

it("stops an oversized paginated export before creating a download", async () => {
  // Simulate expanded page sizes without allocating hundreds of MiB in the test.
  vi.spyOn(Blob.prototype, "size", "get")
    .mockReturnValueOnce(0)
    .mockReturnValueOnce(128 * 1024 * 1024)
    .mockReturnValueOnce(128 * 1024 * 1024 + 1);
  const fetcher = vi.fn()
    .mockResolvedValueOnce(Response.json({ status: "ok", result: { requests: [{ id: 1 }], next_after_id: 1 } }))
    .mockResolvedValueOnce(Response.json({ status: "ok", result: { requests: [{ id: 2 }], next_after_id: 2 } }))
    .mockResolvedValueOnce(Response.json({ status: "ok", result: { requests: [{ id: 3 }], next_after_id: null } }));
  vi.stubGlobal("fetch", fetcher);
  const download = vi.spyOn(URL, "createObjectURL").mockReturnValue("blob:example");
  vi.spyOn(HTMLAnchorElement.prototype, "click").mockImplementation(() => {});

  await expect(exportLlmRequests({ agent_id: "worker-example" })).rejects.toThrow("loopercli llm-requests export");
  expect(fetcher).toHaveBeenCalledTimes(2);
  expect(download).not.toHaveBeenCalled();
});
