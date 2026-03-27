#!/usr/bin/env node
import fs from "node:fs";
import path from "node:path";
import { spawnSync } from "node:child_process";
import { fileURLToPath } from "node:url";

const __filename = fileURLToPath(import.meta.url);
const ROOT = path.resolve(path.dirname(__filename), "..");
const EVIDENCE_PATH = process.env.QUAID_RELEASE_EVIDENCE_PATH || path.join(ROOT, "release-evidence.json");
const COMPAT_PATH = process.env.QUAID_COMPATIBILITY_PATH || path.join(ROOT, "compatibility.json");
const APPROVAL_PATH =
  process.env.QUAID_RELEASE_APPROVAL_PATH || path.join(ROOT, ".release-approval.local.json");
const VALID_SLOTS = ["unit", "ci", "xp"];
const VALID_HOSTS = ["openclaw", "claude-code"];
const DEFAULT_APPROVER = "Solomon Steadman";

function usage() {
  console.log(`Usage:
  node scripts/release-approve-delta.mjs [--head <commit>] [--approved-by <name>] [--notes <text>]
`);
}

function die(message, code = 1) {
  console.error(`[release-approve] ${message}`);
  process.exit(code);
}

function git(args) {
  return spawnSync("git", args, {
    cwd: ROOT,
    env: process.env,
    encoding: "utf8",
  });
}

function gitRequired(args, label) {
  const res = git(args);
  if (res.status !== 0) {
    die(`${label} failed: ${(res.stderr || res.stdout || "").trim()}`);
  }
  return (res.stdout || "").trim();
}

function isAncestor(older, newer) {
  const res = git(["merge-base", "--is-ancestor", older, newer]);
  return res.status === 0;
}

function nowIso() {
  return new Date().toISOString();
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

function loadJson(targetPath, fallback) {
  if (!fs.existsSync(targetPath)) return fallback;
  const parsed = JSON.parse(fs.readFileSync(targetPath, "utf8"));
  return parsed && typeof parsed === "object" ? parsed : fallback;
}

function atomicWriteJson(targetPath, data) {
  const dir = path.dirname(targetPath);
  fs.mkdirSync(dir, { recursive: true });
  const tmpPath = `${targetPath}.tmp-${process.pid}-${Date.now()}.json`;
  fs.writeFileSync(tmpPath, `${JSON.stringify(data, null, 2)}\n`, "utf8");
  fs.renameSync(tmpPath, targetPath);
}

function isShaPlaceholder(value) {
  return typeof value === "string" && /^[0-9a-f]{7,40}$/i.test(value.trim());
}

const opts = parseArgs(process.argv.slice(2));
if (opts.help) {
  usage();
  process.exit(0);
}

const head = String(opts.head || gitRequired(["rev-parse", "HEAD"], "git rev-parse HEAD")).trim();
const approvedBy = String(opts["approved-by"] || DEFAULT_APPROVER).trim() || DEFAULT_APPROVER;
const notes = String(opts.notes || "").trim();

const evidence = loadJson(EVIDENCE_PATH, { evidence: {} });
const compat = loadJson(COMPAT_PATH, { matrix: [] });

const approvedEvidence = {};
for (const slot of VALID_SLOTS) {
  const clearSha = String(evidence.evidence?.[slot]?.sha || "").trim();
  if (!clearSha || clearSha === head) continue;
  if (isAncestor(clearSha, head)) {
    approvedEvidence[slot] = clearSha;
  }
}

const matrix = Array.isArray(compat.matrix) ? compat.matrix : [];
const approvedCompatibility = {};
for (const host of VALID_HOSTS) {
  const pending = matrix.find(
    (entry) =>
      String(entry.host || "").trim() === host &&
      String(entry.status || "").trim() === "compatible" &&
      (entry.pending_release === true || isShaPlaceholder(entry.quaid_range))
  );
  const validated = matrix.find(
    (entry) =>
      String(entry.host || "").trim() === host &&
      String(entry.status || "").trim() === "compatible" &&
      typeof entry.validated_sha === "string" &&
      entry.validated_sha.trim()
  );
  const clearSha = String(
    pending ? pending.quaid_range || "" : validated ? validated.validated_sha || "" : ""
  ).trim();
  if (!clearSha || clearSha === head) continue;
  if (isAncestor(clearSha, head)) {
    approvedCompatibility[host] = clearSha;
  }
}

if (Object.keys(approvedEvidence).length === 0 && Object.keys(approvedCompatibility).length === 0) {
  die(`nothing behind HEAD ${head} requires approval`);
}

atomicWriteJson(APPROVAL_PATH, {
  schema_version: 1,
  approved_head: head,
  approved_at: nowIso(),
  approved_by: approvedBy,
  notes,
  evidence: approvedEvidence,
  compatibility: approvedCompatibility,
});

console.log(`[release-approve] wrote ${path.relative(ROOT, APPROVAL_PATH)}`);
for (const [slot, clearSha] of Object.entries(approvedEvidence)) {
  console.log(`[release-approve] evidence ${slot}: ${clearSha} -> ${head}`);
}
for (const [host, clearSha] of Object.entries(approvedCompatibility)) {
  console.log(`[release-approve] compatibility ${host}: ${clearSha} -> ${head}`);
}
