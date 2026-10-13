#!/usr/bin/env bash
# Print release-readiness-doc-smoke arguments for a release's surfaces file:
# one optional-capability surface per non-empty line, or the explicit
# no-change declaration when the file lists none.
set -euo pipefail
found=0
while IFS= read -r line || [ -n "$line" ]; do
  line="${line#"${line%%[![:space:]]*}"}"
  line="${line%"${line##*[![:space:]]}"}"
  [ -z "$line" ] && continue
  printf -- '--surface\n%s\n' "$line"
  found=1
done < "$1"
[ "$found" = 1 ] || printf -- '--expect-no-optional-capability-changes\n'
