#!/usr/bin/env bash
# preflight-review pre-push hook v1
# Report-only: this hook never blocks a push. Set PREFLIGHT=0 to skip.
set -u

input=$(cat)
hookdir=$(dirname "$0")

# Chained pre-existing hook: its decision is the only thing that can block.
if [ -x "$hookdir/pre-push.pre-preflight" ]; then
  printf '%s\n' "$input" | "$hookdir/pre-push.pre-preflight" "$@"
  chained=$?
  if [ "$chained" -ne 0 ]; then
    exit "$chained"
  fi
fi

if [ "${PREFLIGHT:-1}" = "0" ]; then
  echo "preflight: skipped (PREFLIGHT=0)"
  exit 0
fi

if [ -n "${PREFLIGHT_REVIEW_CMD:-}" ]; then
  cmd=$PREFLIGHT_REVIEW_CMD
elif command -v preflight-review >/dev/null 2>&1; then
  cmd="preflight-review"
elif command -v uv >/dev/null 2>&1; then
  cmd="uv tool run preflight-review"
else
  cmd="python3 -m preflight_review"
fi

# Word-splitting of $cmd and PREFLIGHT_REVIEW_ARGS is intentional.
# shellcheck disable=SC2086
output=$($cmd review --committed-only --incremental ${PREFLIGHT_REVIEW_ARGS:-} </dev/null 2>&1)
code=$?

printf '%s\n' "$output" | tail -n 1
if [ "$code" -eq 1 ]; then
  echo "preflight: review incomplete — see report"
elif [ "$code" -ne 0 ]; then
  echo "preflight: review failed (exit $code); push continues"
fi
exit 0
