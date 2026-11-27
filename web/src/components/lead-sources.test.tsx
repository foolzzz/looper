import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";
import { LeadSources } from "@/components/lead-sources";

const source = (link: string) => ({ message_id: "m1", chat: "chat", sender: "bob", sent_at: "2026-01-01T00:00:00Z", link }) as never;
const textOf = (html: string) => html.replace(/<[^>]*>/g, "");

describe("lead source link", () => {
  it("shows its real host and opens safely", () => {
    const html = renderToStaticMarkup(<LeadSources sources={[source("https://c.example/m/1")]} total={1} />);
    expect(textOf(html)).toContain("打开 (c.example)");
    expect(html).toContain('target="_blank"');
    expect(html).toMatch(/rel="[^"]*noopener[^"]*"/);
    expect(html).toMatch(/rel="[^"]*noreferrer[^"]*"/);
    expect(html).toMatch(/referrerpolicy="no-referrer"/i);
  });

  it("renders no link for a non-web address", () => {
    const html = renderToStaticMarkup(<LeadSources sources={[source("javascript:alert(1)")]} total={1} />);
    expect(html).not.toContain("<a");
  });
});
