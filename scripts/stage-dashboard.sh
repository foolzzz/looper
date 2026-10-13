#!/usr/bin/env bash
# Build the agent dashboard into the package (loopx/web/dashboard) so a wheel
# ships it; `loopx serve` from an installed wheel serves that copy. The staged
# output is generated and never belongs in Git.
set -euo pipefail
root="$(cd "$(dirname "$0")/.." && pwd)"
cd "$root/web"
npm ci
npx vite build --outDir "$root/loopx/web/dashboard" --emptyOutDir
