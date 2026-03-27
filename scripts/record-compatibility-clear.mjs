#!/usr/bin/env node
import fs from "node:fs";
import path from "node:path";
import { spawnSync } from "node:child_process";
import { fileURLToPath } from "node:url";

const __filename = fileURLToPath(import.meta.url);
const ROOT = path.resolve(path.dirname(__filename), "..");
const COMPAT_PATH = process.env.QUAID_COMPATIBILITY_PATH || path.join(ROOT, "compatibility.json");
const VALID_HOSTS = new Set(["openclaw", "claude-code"]);

function usage() {
  console.log(`Usage:
  node scripts/record-compatibility-clear.mjs --host <openclaw|claude-code> --host-version <version> [--sha <commit>] [--notes <text>] [--install-verified true|false]
`);
}

function die(message, code = 1) {
  console.error(`[compat-clear] ${message}`);
  process.exit(code);
}

function gitRequired(args, label) {
  const res = spawnSync("git", args, {
    cwd: ROOT,
    env: process.env,
    encoding: "utf8",
  });
  if (res.status !== 0) {
    die(`${label} failed: ${(res.stderr || res.stdout || "").trim()}`);
  }
  return (res.stdout || "").trim();
}

function parseArgs(argv) {
  const args = [...argv];
  const opts = {};
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

function atomicWriteJson(targetPath, data) {
  const dir = path.dirname(targetPath);
  fs.mkdirSync(dir, { recursive: true });
  const tmpPath = `${targetPath}.tmp-${process.pid}-${Date.now()}.json`;
  fs.writeFileSync(tmpPath, `${JSON.stringify(data, null, 2)}\n`, "utf8");
  fs.renameSync(tmpPath, targetPath);
}

function todayIso() {
  return new Date().toISOString().slice(0, 10);
}

function nowIso() {
  return new Date().toISOString();
}

function isShaPlaceholder(value) {
  return typeof value === "string" && /^[0-9a-f]{7,40}$/i.test(value.trim());
}

const opts = parseArgs(process.argv.slice(2));
const host = String(opts.host || "").trim();
const hostVersion = String(opts["host-version"] || "").trim();
const notes = String(opts.notes || "").trim();
const installVerified = String(opts["install-verified"] || "false").trim().toLowerCase() === "true";

if (!VALID_HOSTS.has(host)) {
  usage();
  die(`invalid --host '${host}'`);
}
if (!hostVersion) {
  usage();
  die("--host-version is required");
}

const clearSha = String(
  opts.sha || gitRequired(["rev-parse", "HEAD"], "git rev-parse HEAD")
).trim();
if (!clearSha) {
  die("could not resolve compatibility clear sha");
}
gitRequired(["rev-parse", "--verify", clearSha], `git rev-parse --verify ${clearSha}`);
const data = JSON.parse(fs.readFileSync(COMPAT_PATH, "utf8"));
const matrix = Array.isArray(data.matrix) ? data.matrix : [];

const nextMatrix = matrix.filter((entry) => {
  if ((entry.host || "").trim() !== host) return true;
  if ((entry.host_range || "").trim() === hostVersion) return false;
  if (entry.pending_release === true) return false;
  if (isShaPlaceholder(entry.quaid_range)) return false;
  return true;
});

nextMatrix.push({
  host,
  host_range: hostVersion,
  quaid_range: clearSha,
  status: "compatible",
  notes,
  message: "",
  fix: "",
  pending_release: true,
  install_verified: installVerified,
  cleared_at: nowIso(),
});

data.matrix = nextMatrix;
data.updated_at = todayIso();
atomicWriteJson(COMPAT_PATH, data);

console.log(
  `[compat-clear] recorded ${host} host_version=${hostVersion} quaid_sha=${clearSha} install_verified=${installVerified}`
);
