// @vitest-environment happy-dom
import { act } from "react";
import { createRoot } from "react-dom/client";
import { expect, it, vi } from "vitest";
import { fetchRegistry, type Registry } from "@/lib/api";
import { useRegistry } from "@/lib/use-registry";

vi.mock("@/lib/api", async (original) => ({
  ...await original<typeof import("@/lib/api")>(), fetchRegistry: vi.fn(),
}));
(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

it("polls a refreshing cached login until the current status arrives", async () => {
  vi.useFakeTimers();
  const registry = (state: string, refreshing?: boolean) => ({
    providers: [{ login: { state, detail: state, refreshing } }],
  } as Registry);
  vi.mocked(fetchRegistry).mockResolvedValueOnce(registry("not_logged_in", true))
    .mockResolvedValue(registry("logged_in"));
  function Login() {
    return <p>{useRegistry().registry?.providers[0].login?.state}</p>;
  }
  const host = document.createElement("div"), root = createRoot(host);
  try {
    await act(async () => root.render(<Login />));
    expect(host.textContent).toBe("not_logged_in");
    await act(async () => vi.advanceTimersByTimeAsync(1000));
    expect(host.textContent).toBe("logged_in");
    expect(fetchRegistry).toHaveBeenCalledTimes(2);
    await act(async () => vi.advanceTimersByTimeAsync(2000));
    expect(fetchRegistry).toHaveBeenCalledTimes(2);
  } finally {
    await act(async () => root.unmount());
    expect(vi.getTimerCount()).toBe(0);
    vi.useRealTimers();
    vi.restoreAllMocks();
  }
});
