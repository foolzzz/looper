#!/usr/bin/env bash
# Pi Responses eval: explicit PI_EVAL_API_KEY, PI_EVAL_BASE_URL and budgets.
# Raw results stay ignored. No operator configuration is read.
set -euo pipefail
EVAL_KIND=responses
source "$(dirname "${BASH_SOURCE[0]}")/eval-common.sh"
