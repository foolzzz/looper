import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";
import { Markdown } from "@/components/markdown";

const record = [
  "## Process record", "",
  "- Roles: write_api `run_cmd` __main__ *args* ~~removed~~ [link](#tasks/t1)",
  "- Reviews: passed", "- Verification: completed", "- Escalations: none", "",
  "Full record: task page / looper_tasks get",
].join("\n");

describe("delivery record Markdown", () => {
  it.each(["chat", "task summary"])("renders agent fragments literally in the %s", (view) => {
    const content = view === "chat" ? `任务完成：Done\n\n${record}` : `## Result\nDone\n\n${record}`;
    const html = renderToStaticMarkup(<Markdown>{content}</Markdown>);
    expect(html).toContain("<h2>Process record</h2>");
    expect(html).toContain("<li>Roles: write_api `run_cmd` __main__ *args* ~~removed~~ [link](#tasks/t1)</li>");
    expect(html).not.toMatch(/<(strong|em|del|code|a)[ >]/);
    expect(html).not.toContain("\\");
  });

  it("keeps surrounding agent Markdown and task links working", () => {
    const html = renderToStaticMarkup(<Markdown>{`**Delivered**\n\n${record}\n\n[Task](#tasks/t1)`}</Markdown>);
    expect(html).toContain("<strong>Delivered</strong>");
    expect(html).toContain('<a href="#tasks/t1">Task</a>');
    expect(html).not.toContain("<strong>main</strong>");
  });
});

describe("remote content in agent Markdown", () => {
  const render = (text: string) => renderToStaticMarkup(<Markdown>{text}</Markdown>);
  const textOf = (html: string) => html.replace(/<[^>]*>/g, "");

  it("shows a remote image as a link with its host and never as an img", () => {
    const html = render("![diagram](https://a.example/p.png?d=secret)");
    expect(html).not.toContain("<img");
    expect(textOf(html)).toContain("image: diagram (a.example)");
    expect(html).toContain('target="_blank"');
  });

  it("labels an image without alt text", () => {
    expect(textOf(render("![](https://a.example/p.png)"))).toContain("image: image (a.example)");
  });

  it("shows the real host next to a link whose text differs, and opens it safely", () => {
    const html = render("[docs](https://b.example/x)");
    expect(html).toContain("docs");
    expect(textOf(html)).toContain("docs (b.example)");
    expect(html).toContain('target="_blank"');
    expect(html).toMatch(/rel="[^"]*noopener[^"]*"/);
    expect(html).toMatch(/rel="[^"]*noreferrer[^"]*"/);
    expect(html).toMatch(/referrerpolicy="no-referrer"/i);
  });

  it("does not repeat the host when the link text is the address", () => {
    const html = render("[https://b.example/x](https://b.example/x)");
    expect(textOf(html)).not.toContain("(b.example)");
  });

  it("shows the host of a link that claims another address", () => {
    expect(textOf(render("[https://bank.example](https://evil.example/x)"))).toContain("(evil.example)");
  });

  it("neutralizes a javascript: link", () => {
    const html = render("[click](javascript:alert(1))");
    expect(html).not.toContain("javascript:");
    expect(html).toContain("click");
    expect(html).not.toContain("<a");
  });
});
