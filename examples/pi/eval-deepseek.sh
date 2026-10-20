#!/usr/bin/env bash
# Pi Chat Completions eval against https://api.deepseek.com, deepseek-flash.
# Requires DEEPSEEK_API_KEY, PI_EVAL_TOTAL_CAP_USD, PI_EVAL_CASE_BUDGET_USD,
# and a local LOOPER_EVAL_DATABASE_URL (or LOOPER_TEST_DATABASE_URL).
# PI_EVAL_DEADLINE_SECONDS defaults to 1500 and cannot exceed it.
# No generic/CPA key or endpoint fallback; never reads operator configuration.
# Launch in the background with output redirected to an ignored local log.
# Results use a fresh ignored directory; missing/unknown spend stops dialogs.
# Real protocol/schema/read-only/session acceptance requires separate readback;
# do not change shared prompts or schemas to fix an individual eval failure.
set -euo pipefail
EVAL_KIND=deepseek
source "$(dirname "${BASH_SOURCE[0]}")/eval-common.sh"
