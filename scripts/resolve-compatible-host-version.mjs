#!/usr/bin/env node
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const __filename = fileURLToPath(import.meta.url);
const ROOT = path.resolve(path.dirname(__filename), "..");
const COMPAT_PATH = process.env.QUAID_COMPATIBILITY_PATH || path.join(ROOT, "compatibility.json");

function usage() {
  console.log(`Usage:
  node scripts/resolve-compatible-host-version.mjs --host <openclaw|claude-code|codex>
`);
}

function die(message) {
  console.error(`[compat-resolve] ${message}`);
  process.exit(1);
}

function parseArgs(argv) {
  const opts = {};
  const args = [...argv];
  while (args.length) {
    const token = args.shift();
    if (!token.startsWith("--")) {
      die(`unexpected positional argument '${token}'`);
    }
    const key = token.slice(2);
    if (!args.length || args[0].startsWith("--")) {
      opts[key] = true;
      continue;
    }
    opts[key] = args.shift();
  }
  return opts;
}

function isConcreteHostVersion(value) {
  const text = String(value || "").trim();
  return Boolean(text) && !/[<>=~^*xX\s]/.test(text);
}

function timestampMs(entry) {
  const candidates = [entry.cleared_at, entry.validated_at, entry.updated_at].map((value) =>
    Date.parse(String(value || ""))
  );
  for (const value of candidates) {
    if (Number.isFinite(value)) return value;
  }
  return 0;
}

function versionParts(version) {
  return String(version || "")
    .split(".")
    .map((part) => {
      const parsed = Number.parseInt(part, 10);
      return Number.isFinite(parsed) ? parsed : 0;
    });
}

function compareVersionDesc(a, b) {
  const aa = versionParts(a.host_range);
  const bb = versionParts(b.host_range);
  const len = Math.max(aa.length, bb.length);
  for (let i = 0; i < len; i += 1) {
    const diff = (bb[i] || 0) - (aa[i] || 0);
    if (diff !== 0) return diff;
  }
  return 0;
}

function main() {
  const opts = parseArgs(process.argv.slice(2));
  const host = String(opts.host || "").trim();
  if (!host) {
    usage();
    die("--host is required");
  }
  const data = JSON.parse(fs.readFileSync(COMPAT_PATH, "utf8"));
  const matrix = Array.isArray(data.matrix) ? data.matrix : [];
  const candidates = matrix
    .filter((entry) => String(entry.host || "").trim() === host)
    .filter((entry) => String(entry.status || "").trim() === "compatible")
    .filter((entry) => isConcreteHostVersion(entry.host_range))
    .filter((entry) => entry.install_verified !== false);

  if (!candidates.length) {
    die(`no concrete compatible version found for host '${host}' in ${COMPAT_PATH}`);
  }

  candidates.sort((a, b) => {
    const timeDiff = timestampMs(b) - timestampMs(a);
    if (timeDiff !== 0) return timeDiff;
    return compareVersionDesc(a, b);
  });

  console.log(String(candidates[0].host_range).trim());
}

main();
