/** Display text for the Needs you page and the work panel. One rule set.
 * A sentence that only mentions a token stays. An assignment to a secret-like key,
 * a Bearer or Basic header, a known token prefix, or a long opaque token is replaced.
 */

const REDACTED = "[redacted]";

/** Match only credential keys, so a nonsecret prefix cannot consume an inner assignment. */
const SECRET_SEGMENT = "(?:api[_-]?key|private[_-]?key|passwd|password|authorization|bearer|cookie|session|token|secret|credential|key)";
const SECRET_KEY = `(?:[A-Za-z0-9]+[_-])*${SECRET_SEGMENT}(?:[_-][A-Za-z0-9]+)*`;
const ASSIGNMENT = new RegExp(
  String.raw`(?<![A-Za-z0-9_-])((?:"${SECRET_KEY}"|'${SECRET_KEY}'|${SECRET_KEY})\s*[:=]\s*)("(?:[^"\\]|\\.)*"|'(?:[^'\\]|\\.)*'|\[redacted\]|[^\s,;&?#{}\[\]"'<>]+)`,
  "gi",
);

function assigned(value: string): string {
  return value.replace(ASSIGNMENT, (_match, keyAndSeparator: string) => `${keyAndSeparator}${REDACTED}`);
}

/** `value` with credential material replaced. Mentioning a token keeps the sentence. */
export function redactDisplay(value: string): string {
  return assigned(value.replace(/\b(?:Bearer|Basic)\s+[A-Za-z0-9\-._~+/=]+/gi, REDACTED))
    .replace(/\b(?:sk|rk|pk|ghp|gho|github_pat|xox[baprs]|AKIA)[-_][A-Za-z0-9_\-]{8,}\b/gi, REDACTED)
    .replace(/\b[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b/g, REDACTED)
    // Opaque values can be hex or base64 without a mixture of cases and digits.
    .replace(
      /(?<![A-Za-z0-9+/_-])[A-Za-z0-9+/_-]{40,}={0,2}/g,
      REDACTED,
    );
}

/** Known display text. Anything other than a string is blank. */
export function displayText(value: unknown): string {
  return typeof value === "string" ? redactDisplay(value) : "";
}
