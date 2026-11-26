import { RemoteLink, remoteHost } from "@/components/remote-link";
import type { LeadSource } from "@/lib/api-leads";

/** The sources a lead or an intent shows, plus how many more it has. */
export function LeadSources({ sources, total }: { sources: LeadSource[]; total: number }) {
  const extra = Math.max(0, total - sources.length);
  if (sources.length === 0 && extra === 0) return null;
  return (
    <ul className="space-y-1 text-xs text-muted-foreground">
      {sources.map((source, index) => (
        <li key={`${source.message_id}-${index}`}>
          {source.chat} · {source.sender} · {source.sent_at ? source.sent_at.slice(0, 16).replace("T", " ") : "—"}
          {source.link && remoteHost(source.link) && <> · <RemoteLink href={source.link} host={remoteHost(source.link)!}>打开</RemoteLink></>}
        </li>
      ))}
      {extra > 0 && <li>另 {extra} 条</li>}
    </ul>
  );
}
