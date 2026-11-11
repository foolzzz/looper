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
