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

  it.each([
    ["Cyrillic letter", "https://\u0430pple.com/login", "xn--pple-43d.com"],
    ["full-width letter", "https://\uff45vil.example/x", "evil.example"],
    ["percent-encoded letter", "https://%65vil.example/x", "evil.example"],
  ])("shows the ASCII host when the text spells the host differently: %s", (_name, address, host) => {
    expect(textOf(render(`[${address}](${address})`))).toContain(`(${host})`);
    if (!address.includes("%")) expect(textOf(render(`go ${address}`))).toContain(`(${host})`); // GFM does not autolink a % host
  });

  it.each(["HTTPS://EVIL.EXAMPLE/x", " https://evil.example/x ", "https://evil.example/", "https://evil.example/x"])(
    "does not repeat the host when the text is the address: %j", (text) => {
      expect(textOf(render(`[${text.trim()}](https://evil.example/x)`))).not.toContain("(evil.example)");
    });

  it.each([
    ["https text, http href", "https://evil.example/x", "http://evil.example/x"],
    ["http text, https href", "http://evil.example/x", "https://evil.example/x"],
    ["other port", "https://evil.example/x", "https://evil.example:8443/x"],
    ["href userinfo", "https://evil.example/x", "https://bank.example@evil.example/x"],
  ])("keeps the host label when the text differs from the destination in more than the path: %s", (_n, text, href) => {
    expect(textOf(render(`[${text}](${href})`))).toContain("(evil.example");
  });

  it("shows the host label when the ASCII host only appears in the path of look-alike text", () => {
    for (const text of ["https://\u0430pple.com/xn--pple-43d.com", "https://\u0430pple.com/?q=xn--pple-43d.com", "https://\u0430pple.com/#xn--pple-43d.com"]) {
      expect(textOf(render(`[${text}](${text})`))).toContain("(xn--pple-43d.com)");
    }
  });

  it.each(["\u202e", "\u202a", "\u2066", "\u2069", "\u200e", "\u200f", "\u061c"])(
    "removes the bidi control U+%#x from agent text", (mark) => {
      const html = render(`${mark}[https://evil.example/${mark}x](https://evil.example/x) ${mark}after`);
      expect(html).not.toMatch(/[\u202a-\u202e\u2066-\u2069\u200e\u200f\u061c]/);
    });

  it.each(["&#x202E;", "&#8238;", "&#x2066;", "&#x200F;", "&#x061C;"])("removes the entity-encoded bidi control %s", (entity) => {
    const html = render(`[pay ${entity} https://bank.example/x](https://evil.example/x) ${entity} plain`);
    expect(html).not.toMatch(/[\u202a-\u202e\u2066-\u2069\u200e\u200f\u061c]/);
    expect(textOf(html)).toContain("(evil.example)");
  });

  it("keeps two adjacent links in order and each label next to its own link under an override", () => {
    const html = render("\u202e[https://a.example/](https://b.example/) [https://c.example/](https://d.example/)");
    const text = textOf(html);
    expect(html).not.toMatch(/[\u202a-\u202e\u2066-\u2069]/);
    expect(text.indexOf("(b.example)")).toBeGreaterThan(text.indexOf("https://a.example/"));
    expect(text.indexOf("(b.example)")).toBeLessThan(text.indexOf("https://c.example/"));
    expect(text.indexOf("(d.example)")).toBeGreaterThan(text.indexOf("https://c.example/"));
  });

  it("drops the title of an external link", () => {
    expect(render('[t](https://b.example/x "bank.example")')).not.toContain("title=");
  });

  it("marks a same-site link whose text is an address on another site", () => {
    vi.stubGlobal("location", new URL("http://dashboard.example/"));
    try {
      const html = render("[https://evil.example/x](http:evil.example/x)");
      expect(html).not.toContain("_blank");
      expect(textOf(html)).toContain("(this site)");
      expect(textOf(render("[http://dashboard.example/x](/x) [t](#tasks/t1)"))).not.toContain("(this site)");
    } finally {
      vi.unstubAllGlobals();
    }
  });

  it("keeps footnote ids, aria attributes and link titles on internal links", () => {
    const html = render("text[^1] [a](#x \"Where to\")\n\n[^1]: note");
    expect(html).toContain('id="user-content-fnref-1"');
    expect(html).toContain("aria-describedby");
    expect(html).toContain('title="Where to"');
  });

  it("renders mailto links and email autolinks as text, and a relative image as a same-origin link", () => {
    expect(render("[m](mailto:a@b.example) a@b.example")).not.toContain("<a");
    const html = render("![x](/api/v1/f.png)");
    expect(html).not.toContain("<img");
    expect(html).toContain('href="/api/v1/f.png"');
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
