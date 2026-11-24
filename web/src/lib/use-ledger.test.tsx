// @vitest-environment happy-dom
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { LedgerProvider, useLedger } from "@/lib/use-ledger";
import { fetchChatSettings } from "@/lib/api";
import { ledgerPage, type LedgerPageResult } from "@/lib/api-ledger";
vi.mock("@/lib/api", () => ({ fetchChatSettings: vi.fn() }));
vi.mock("@/lib/api-ledger", () => ({ ledgerPage: vi.fn() }));
(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean; }).IS_REACT_ACT_ENVIRONMENT = true;
let root: Root, host: HTMLDivElement;

function Probe() {
  const state = useLedger();
  return <><output>
    {JSON.stringify({ enabled: state.enabled, count: state.count, stale: state.stale })}
  </output><button onClick={state.changed}>写成功</button><button onClick={state.refresh}>刷新</button></>;
}
const result = (n: number) => ({ attention_count: n } as LedgerPageResult);
const settings = (enabled: boolean) => ({ ledger_enabled: enabled, stop_enabled: false, completion_turns: "off" as const });
async function mount() {
  await act(async () => {
    root.render(<LedgerProvider>
      <Probe />
    </LedgerProvider>);
  });
}
async function press(text: string) {
  await act(async () => {
    [...host.querySelectorAll("button")].find((b) => b.textContent === text)!.click();
  });
}
beforeEach(() => {
  vi.useFakeTimers();
  host = document.createElement("div");
  document.body.append(host);
  root = createRoot(host);
});
afterEach(() => {
  act(() => root.unmount());
  host.remove();
  vi.useRealTimers();
  vi.resetAllMocks();
});
it("does not fetch entries while disabled and refreshes the count after a write", async () => {
  vi.mocked(fetchChatSettings).mockResolvedValue(settings(false));
  await mount();
  expect(ledgerPage).not.toHaveBeenCalled();
  expect(host.textContent).toContain('"enabled":false');
  vi.mocked(fetchChatSettings).mockResolvedValue(settings(true));
  vi.mocked(ledgerPage).mockResolvedValue(result(7));
  await press("刷新");
  expect(host.textContent).toContain('"count":7');
  vi.mocked(ledgerPage).mockResolvedValue(result(2));
  await press("写成功");
  expect(host.textContent).toContain('"count":2');
  const before = vi.mocked(ledgerPage).mock.calls.length;
  await act(async () => {
    await vi.advanceTimersByTimeAsync(30_000);
  });
  expect(ledgerPage).toHaveBeenCalledTimes(before + 1);
});
it("keeps an unknown count distinct from zero and ignores stale responses", async () => {
  vi.mocked(fetchChatSettings).mockResolvedValue(settings(true));
  vi.mocked(ledgerPage).mockRejectedValue(new Error("离线"));
  await mount();
  expect(host.textContent).toContain('"count":null');
  expect(host.textContent).toContain('"stale":true');
  vi.mocked(ledgerPage).mockResolvedValue(result(9));
  await press("刷新");
  expect(host.textContent).toContain('"count":9');
  let old!: (value: LedgerPageResult) => void;
  vi.mocked(ledgerPage).mockImplementationOnce(() => new Promise((resolve) => {
    old = resolve;
  }));
  await press("刷新");
  vi.mocked(fetchChatSettings).mockResolvedValue(settings(false));
  await press("刷新");
  await act(async () => {
    old(result(99));
  });
  expect(host.textContent).toContain('"enabled":false');
  expect(host.textContent).not.toContain('"count":99');
  const before = vi.mocked(fetchChatSettings).mock.calls.length;
  act(() => root.unmount());
  root = createRoot(host);
  await act(async () => {
    await vi.advanceTimersByTimeAsync(60_000);
  });
  expect(fetchChatSettings).toHaveBeenCalledTimes(before);
});
