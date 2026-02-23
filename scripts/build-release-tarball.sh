#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
OUT1="$ROOT_DIR/release/quaid-release.tar.gz"
OUT2="${ROOT_DIR%/dev}/quaid-release.tar.gz"
TMP_TAR="/tmp/quaid-release.tar.gz"

mkdir -p "$(dirname "$OUT1")"

cd "$ROOT_DIR"

tar \
  --exclude='.git' \
  --exclude='node_modules' \
  --exclude='logs' \
  --exclude='.pytest-home' \
  -czf "$TMP_TAR" \
  .

cp "$TMP_TAR" "$OUT1"
cp "$TMP_TAR" "$OUT2"

echo "[release-tarball] wrote:"
ls -lh "$OUT1" "$OUT2"

echo "[release-tarball] sha256:"
shasum -a 256 "$OUT1" "$OUT2"
