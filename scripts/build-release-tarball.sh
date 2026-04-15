#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
OUT1="$ROOT_DIR/release/quaid-release.tar.gz"
OUT2="${ROOT_DIR%/dev}/quaid-release.tar.gz"
TMP_TAR="/tmp/quaid-release.tar.gz"
HEAD_SHA="$(git -C "$ROOT_DIR" rev-parse HEAD)"

mkdir -p "$(dirname "$OUT1")"

cd "$ROOT_DIR"

# Build the release artifact from tracked files at HEAD only.
# Do not package the whole working tree: local VM images, temp dirs, prior
# tarballs, and other untracked/ignored artifacts can be enormous and are not
# part of the release surface.
git archive --format=tar "$HEAD_SHA" | gzip -c > "$TMP_TAR"

cp "$TMP_TAR" "$OUT1"
cp "$TMP_TAR" "$OUT2"

echo "[release-tarball] wrote:"
ls -lh "$OUT1" "$OUT2"

echo "[release-tarball] sha256:"
shasum -a 256 "$OUT1" "$OUT2"
