import { renderToStaticMarkup } from "react-dom/server";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
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

  // A link must either name the host the browser would open or not be a link: the dashboard may be served over http or https.
  const SPOOFS: [string, string][] = [
    ["scheme without slashes", "https:evil.example/x"],
    ["http scheme without slashes", "http:evil.example/x"],
    ["uppercase scheme", "HTTPS://evil.example/x"],
    ["uppercase scheme without slashes", "HTTP:evil.example/x"],
    ["scheme-relative", "//evil.example/x"],
    ["encoded backslashes", "https:%5C%5Cevil.example/x"],
    ["backslashes", "https:\\\\evil.example/x"],
    ["slash backslash", "https:/\\evil.example/x"],
    ["tab inside the scheme", "ht%09tps://evil.example/x"],
    ["newline inside the scheme", "https:%0A//evil.example/x"],
    ["userinfo", "https://bank.example@evil.example/x"],
  ];
  describe.each([["http", "http://dashboard.example/"], ["https", "https://dashboard.example/"]])("on an %s dashboard", (_p, origin) => {
    beforeEach(() => vi.stubGlobal("location", new URL(origin)));
    afterEach(() => vi.unstubAllGlobals());

    it.each(SPOOFS)("does not hide the real host of a disguised address: %s", (_name, target) => {
      const html = render(`[https://bank.example](${target})`);
      const a = html.match(/<a [^>]*href="([^"]*)"/);
      if (a && !/^(?:#|\/(?!\/))/.test(a[1]) && !new URL(a[1], origin).href.startsWith(origin)) {
        expect(textOf(html)).toContain("(evil.example)");
        expect(html).toContain('target="_blank"');
      }
    });

    it("shows the host of a scheme-less address that leaves this origin", () => {
      const other = origin.startsWith("https") ? "http:evil.example/x" : "https:evil.example/x";
      const html = render(`[https://bank.example](${other})`);
      expect(textOf(html)).toContain("(evil.example)");
    });
  });

  it("shows the host when the text is an address with userinfo", () => {
    expect(textOf(render("[https://bank.example@evil.example/](https://bank.example@evil.example/)"))).toContain("(evil.example)");
  });

  it("never links what is neither a task link nor a web address", () => {
    for (const target of ["data:text/html,x", "vbscript:x", "mailto:a@b.example", "irc://x.example/y"]) {
      expect(render(`[t](${target})`)).not.toContain("<a");
    }
  });

  it("keeps task links and anchors internal", () => {
    const html = render("[a](#tasks/t1) [b](/api/v1/x)");
    expect(html).not.toContain("_blank");
    expect(html).toContain('href="#tasks/t1"');
  });

  it("does not nest a linked image inside its link", () => {
    const html = render("[![CI](https://img.example/b.svg)](https://ci.example/run)");
    expect(html.match(/<a[ >]/g)).toHaveLength(1);
    expect(textOf(html)).toContain("image: CI");
    expect(textOf(html)).toContain("(ci.example)");
  });

  it("neutralizes a javascript: link", () => {
    const html = render("[click](javascript:alert(1))");
    expect(html).not.toContain("javascript:");
    expect(html).toContain("click");
    expect(html).not.toContain("<a");
  });
});
