// @vitest-environment happy-dom
import { act } from "react";
import { createRoot } from "react-dom/client";
import { describe, expect, it, vi } from "vitest";
import { MessageCollectionStatus } from "./message-collection-status";
import { getMessageCollection, type MessageCollection } from "@/lib/api-message-collection";

vi.mock("@/lib/api-message-collection", () => ({ getMessageCollection: vi.fn() }));
(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

const facts: MessageCollection = {
  source_id: "synthetic-source", ingest_mode: "collect", generation: "9007199254740993",
  settings: { collect_cron: "*/10 * * * *", alerts_enabled: true },
  auth: { state: null, checked_at: null, refresh_expires_at: null },
  last_collect_ok_at: null, collect_enabled_at: "2026-11-22T12:00:00Z", debt: { chats: 0, threads: 2 },
  latest_run: { run_id: "17", status: "partial", search_hits: 0, unindexed: null, sweep_chats: 1,
    sweep_found: 0, events_new: 0, events_seen: 8, chats_failed: 0, threads_failed: 2 },
  parity: { session: "9007199254740994", state: "pending", reason: null, empty_window: false,
    window: { from: "2026-11-21T12:00:00Z", to: "2026-11-22T12:00:00Z" },
    coverage: { complete: 0, pending: 2, failed: 0 }, totals: { bridge: 8, collect: 0, bridge_only: 8, collect_only: 0 },
    buckets: [{ chat_id: "oc_synthetic", hour: "2026-11-22T11:00:00Z", bridge_only: 8, collect_only: 0, identity_conflicts: 0 }] },
  alerts: [{ id: "3", kind: "auth", opened_at: "2026-11-22T12:00:00Z", resolved_at: null,
    parity_session_id: null, metadata: {}, open_state: "unknown", recovery_state: "pending" }],
};

describe("MessageCollectionStatus", () => {
  it.each(["pending", "drift", "matched", "incomplete"] as const)("renders explicit collection and parity states: %s", async (state) => {
    vi.mocked(getMessageCollection).mockResolvedValue({ ...facts, parity: { ...facts.parity!, state } });
    const host = document.createElement("div");
    const root = createRoot(host);
    try {
      await act(async () => root.render(<MessageCollectionStatus sourceId="synthetic-source" />));
      expect(host.textContent).toContain("未验证");
      expect(host.textContent).toContain("到期时间未知");
      expect(host.textContent).toContain("投递结果未知");
      expect(host.textContent).toContain({ pending: "等待采集", drift: "存在差异", matched: "一致", incomplete: "证据不足" }[state]);
      expect(host.textContent).toContain("9007199254740994");
      expect(host.textContent).toContain("重复 8");
      expect(host.textContent).toContain("oc_synthetic · 2026-11-22T11:00 UTC");
      expect(host.textContent).not.toContain("9007199254740993");
      expect(host.querySelector("button")).toBeNull();
    } finally { act(() => root.unmount()); }
  });

  it("shows auth failure, empty window, and read errors without claiming success", async () => {
    vi.mocked(getMessageCollection).mockResolvedValueOnce({ ...facts, auth: { ...facts.auth, state: "missing" },
      parity: { ...facts.parity!, state: "matched", empty_window: true } });
    const host = document.createElement("div");
    const root = createRoot(host);
    try {
      await act(async () => root.render(<MessageCollectionStatus sourceId="synthetic-source" />));
      expect(host.textContent).toContain("缺少登录");
      expect(host.textContent).toContain("空窗口，尚不能证明采集有效");
      vi.mocked(getMessageCollection).mockRejectedValueOnce(new Error("unavailable"));
      await act(async () => root.render(<MessageCollectionStatus sourceId="other-source" />));
      expect(host.querySelector('[role="alert"]')?.textContent).toContain("unavailable");
    } finally { act(() => root.unmount()); }
  });

  it.each([[0, null, "比率 未知"], [10, 0, "比率 0.0%"], [100, 0.03, "比率 3.0%"]] as const)(
    "keeps unknown fallback coverage distinct from measured zero: %s", async (events, ratio, expected) => {
      vi.mocked(getMessageCollection).mockResolvedValue({ ...facts,
        fallback_24h: { events_new: events, sweep_found: ratio ? 3 : 0, ratio } });
      const host = document.createElement("div");
      const root = createRoot(host);
      try {
        await act(async () => root.render(<MessageCollectionStatus sourceId="synthetic-source" />));
        expect(host.textContent).toContain(expected);
        if (ratio === null) expect(host.textContent).not.toContain("0.0%");
        if (ratio && ratio > 0.02) expect(host.textContent).toContain("超过 2%");
      } finally { act(() => root.unmount()); }
    });

  it("keeps an unresolved earlier parity window identifiable beside a newer matched report", async () => {
    vi.mocked(getMessageCollection).mockResolvedValue({ ...facts, parity: { ...facts.parity!, state: "matched" },
      alerts: [{ ...facts.alerts[0], kind: "parity_drift", parity_session_id: "41",
        metadata: { window_from: "2026-11-20T12:00:00Z", window_to: "2026-11-21T12:00:00Z", bridge_only: 2, collect_only: 0 } }] });
    const host = document.createElement("div");
    const root = createRoot(host);
    try {
      await act(async () => root.render(<MessageCollectionStatus sourceId="synthetic-source" />));
      expect(host.textContent).toContain("对账会话 41");
      expect(host.textContent).toContain("仅参考 2 · 仅采集 0");
      expect(host.textContent).toContain("对账差异 · 待处理");
    } finally { act(() => root.unmount()); }
  });
});
