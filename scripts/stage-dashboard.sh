#!/usr/bin/env bash
# Build the agent dashboard into the package (looper/web/dashboard) so a wheel
# ships it; `looper serve` from an installed wheel serves that copy. The staged
# output is generated and never belongs in Git.
set -euo pipefail
root="$(cd "$(dirname "$0")/.." && pwd)"
cd "$root/web"
npm ci
npx vite build --outDir "$root/looper/web/dashboard" --emptyOutDir
