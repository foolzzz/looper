/** A SKILL.md split into the fields a project skill takes: the server refuses frontmatter in its body. */
export interface SkillFile {
  name: string;
  description: string;
  content: string;
}

/** One frontmatter value: unquoted, "JSON-quoted" or 'single-quoted'. */
function scalar(raw: string): string {
  const value = raw.trim();
  if (value.startsWith('"') && value.endsWith('"') && value.length > 1) {
    try {
      return String(JSON.parse(value));
    } catch {
      return value.slice(1, -1);
    }
  }
  if (value.startsWith("'") && value.endsWith("'") && value.length > 1) return value.slice(1, -1).replace(/''/g, "'");
  return value;
}

/**
 * Read `name` and `description` from a SKILL.md's frontmatter and return the body below it. Handles `key: value`,
 * quoted values and block scalars (`>` or `|`, joined into one line); a file without frontmatter is all body.
 */
export function parseSkillFile(text: string): SkillFile {
  const lines = text.replace(/^﻿/, "").split(/\r?\n/);
  if (lines[0]?.trim() !== "---") return { name: "", description: "", content: text };
  const end = lines.findIndex((line, i) => i > 0 && line.trim() === "---");
  if (end < 0) return { name: "", description: "", content: text };
  const fields: Record<string, string> = {};
  for (let i = 1; i < end; i++) {
    const match = /^([A-Za-z_][\w-]*):\s*(.*)$/.exec(lines[i]);
    if (!match) continue;
    const [, key, rest] = match;
    if (/^[>|][+-]?$/.test(rest.trim())) {
      const block: string[] = [];
      while (i + 1 < end && (lines[i + 1].trim() === "" || /^\s/.test(lines[i + 1]))) block.push(lines[++i].trim());
      fields[key] = block.filter(Boolean).join(" ");
    } else {
      fields[key] = scalar(rest);
    }
  }
  const body = lines.slice(end + 1);
  while (body.length && !body[0].trim()) body.shift();
  return { name: fields.name ?? "", description: fields.description ?? "", content: body.join("\n") };
}
