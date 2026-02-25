#!/usr/bin/env bash
set -Eeuo pipefail

REPO_ROOT="${QUAID_REPO_ROOT:-${HOME}/quaid/dev}"
RUNNER="${REPO_ROOT}/modules/quaid/scripts/nightly-full-suite.sh"
CRON_TAG="# QUAID_NIGHTLY_E2E"
CRON_EXPR="0 3 * * *"
CRON_LINE="${CRON_EXPR} ${RUNNER} >/dev/null 2>&1 ${CRON_TAG}"

if [[ ! -x "${RUNNER}" ]]; then
  echo "Runner missing or not executable: ${RUNNER}" >&2
  exit 1
fi

current="$(crontab -l 2>/dev/null || true)"
filtered="$(printf '%s\n' "$current" | sed "/${CRON_TAG//\//\\/}/d")"
new_cron="${filtered}"
if [[ -n "${new_cron}" && "${new_cron: -1}" != $'\n' ]]; then
  new_cron+=$'\n'
fi
new_cron+="${CRON_LINE}"
new_cron+=$'\n'
printf '%s' "$new_cron" | crontab -

echo "Installed nightly cron:" 
echo "  ${CRON_LINE}"
echo "Current matching entries:" 
crontab -l | rg "QUAID_NIGHTLY_E2E|nightly-full-suite\.sh" -n || true
