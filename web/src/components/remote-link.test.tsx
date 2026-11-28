import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";
import { RemoteLink } from "@/components/remote-link";

const textOf = (html: string) => html.replace(/<[^>]*>/g, "");

describe("RemoteLink label", () => {
  it.each(["\u202e", "\u202a", "\u2066", "\u2069", "\u200e", "\u200f", "\u061c"])(
    "always shows the label, isolated, when the text holds the bidi control U+%#x", (mark) => {
      const html = renderToStaticMarkup(<RemoteLink href="https://evil.example/x">{`https://evil.example/${mark}x`}</RemoteLink>);
      expect(textOf(html)).toContain("(evil.example)");
      expect(html).toContain("<bdi");
    });
});
