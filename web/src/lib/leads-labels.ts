/** Labels shared by the leads tab and the Intents page. They match the vocabulary the server already uses. */

export const LEADS_NOTICE = "来自飞书消息，未经确认；只当线索，不当权威。";

export const LEAD_KIND_ZH: Record<string, string> = {
  decision: "决定", commitment: "承诺", request: "请求", status: "进展", issue: "问题", fact: "事实",
};

export const CONFIDENCE_ZH: Record<string, string> = { high: "高", medium: "中", low: "低" };

export const LEAD_STATE_ZH: Record<string, string> = {
  active: "有效", expired: "已过期", superseded: "已被推翻", promoted: "已提升", inaccurate: "不准确",
  dropped: "已被复核丢弃", all: "全部",
};

export const INTENT_STATE_ZH: Record<string, string> = {
  pending: "待处理", tasked: "已建任务", rejected: "已驳回", expired: "已过期", merged: "已合并", dropped: "已被复核丢弃",
};

export const PRIORITY_ZH: Record<string, string> = { urgent: "紧急", normal: "普通", quiet: "不推送" };

export const PUSH_ZH: Record<string, string> = {
  none: "未推送", pending: "未推送", sending: "推送中", sent: "已推送", failed: "推送失败", uncertain: "结果不明",
};

export const EDITED_VIA_ZH: Record<string, string> = { dashboard: "界面", api: "接口", builtin: "内置", cli: "命令行" };

export function label(map: Record<string, string>, key: string): string {
  return map[key] ?? key;
}

/** How far `iso` is from now, in either direction. An unreadable value is "—". */
export function relative(iso: string | null): string {
  if (!iso) return "—";
  const delta = new Date(iso).getTime() - Date.now();
  if (!Number.isFinite(delta)) return "—";
  const past = delta < 0;
  const minutes = Math.round(Math.abs(delta) / 60000);
  const text = minutes < 1 ? "不到 1 分钟" : minutes < 60 ? `${minutes} 分钟` : minutes < 1440
    ? `${Math.round(minutes / 60)} 小时` : `${Math.round(minutes / 1440)} 天`;
  return past ? `${text}前` : `${text}后`;
}

/** A rate the stats answer left null (nothing to divide by) is an em dash, not zero. */
export function rate(value: number | null): string {
  if (value == null || !Number.isFinite(value)) return "—";
  return `${Math.round(value * 1000) / 10}%`;
}
