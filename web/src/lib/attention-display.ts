/** Display text for the Needs you page and the work panel. One rule set.
 * A sentence that only mentions a token stays. An assignment to a secret-like key,
 * a Bearer or Basic header, a known token prefix, or a long opaque token is replaced.
 */

const REDACTED = "[redacted]";

/** A key whose value is a credential: a whole segment, so "keyboard" and "no refresh token" are not. */
const SECRET_KEY = /(?:^|[_-])(?:api[_-]?key|private[_-]?key|passwd|password|authorization|bearer|cookie|session|token|secret|credential|key)(?=$|[_-])/i;

function assigned(value: string): string {
  return value.replace(
    /\b([A-Za-z0-9]+(?:[_-][A-Za-z0-9]+)*)(\s*[:=]\s*)("(?:[^"\\]|\\.)*"|'(?:[^'\\]|\\.)*'|[^\s,;&]+)/g,
    (match, key: string, sep: string) => (SECRET_KEY.test(key) ? `${key}${sep}${REDACTED}` : match),
  );
}

/** `value` with credential material replaced. Mentioning a token keeps the sentence. */
export function redactDisplay(value: string): string {
  return assigned(value.replace(/\b(?:Bearer|Basic)\s+[A-Za-z0-9\-._~+/=]{8,}/gi, REDACTED))
    .replace(/\b(?:sk|rk|pk|ghp|gho|github_pat|xox[baprs]|AKIA)[-_][A-Za-z0-9_\-]{8,}\b/gi, REDACTED)
    .replace(
      /(?<![A-Za-z0-9+/_-])(?=[A-Za-z0-9+/_-]*\d)(?=[A-Za-z0-9+/_-]*[a-z])(?=[A-Za-z0-9+/_-]*[A-Z])[A-Za-z0-9+/_-]{40,}={0,2}/g,
      REDACTED,
    );
}

/** Known display text. Anything other than a string is blank. */
export function displayText(value: unknown): string {
  return typeof value === "string" ? redactDisplay(value) : "";
}
