#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PLUGIN_DIR="$ROOT_DIR/modules/quaid"

echo "[release-check] docs consistency"
node "$ROOT_DIR/scripts/check-docs-consistency.mjs"

echo "[release-check] privacy audit"
node "$ROOT_DIR/scripts/privacy-audit.mjs"

echo "[release-check] release evidence"
node "$ROOT_DIR/scripts/release-evidence.mjs" check

echo "[release-check] compatibility promotion"
node "$ROOT_DIR/scripts/release-promote-compatibility.mjs"

echo "[release-check] release consistency"
node "$ROOT_DIR/scripts/release-verify.mjs"

echo "[release-check] ownership / attribution"
node "$ROOT_DIR/scripts/release-owner-check.mjs"

echo "[release-check] runtime ts/js pairs"
(
  cd "$PLUGIN_DIR"
  node scripts/check-runtime-pairs.mjs --strict
)

echo "[release-check] quaid release gate"
(
  cd "$PLUGIN_DIR"
  npm run test:release-gate
)

echo "[release-check] PASS"
