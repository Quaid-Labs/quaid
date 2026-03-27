#!/usr/bin/env node
import fs from "node:fs";
import path from "node:path";
import { spawnSync } from "node:child_process";
import { fileURLToPath } from "node:url";

const __filename = fileURLToPath(import.meta.url);
const ROOT = path.resolve(path.dirname(__filename), "..");
const COMPAT_PATH = process.env.QUAID_COMPATIBILITY_PATH || path.join(ROOT, "compatibility.json");
const PKG_PATH = path.join(ROOT, "modules", "quaid", "package.json");
const APPROVAL_PATH =
  process.env.QUAID_RELEASE_APPROVAL_PATH || path.join(ROOT, ".release-approval.local.json");
const REQUIRED_HOSTS = ["openclaw", "claude-code"];

function die(message, code = 1) {
  console.error(`[release-compat] ${message}`);
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

function todayIso() {
  return new Date().toISOString().slice(0, 10);
}

function isShaPlaceholder(value) {
  return typeof value === "string" && /^[0-9a-f]{7,40}$/i.test(value.trim());
}

function isAncestor(older, newer) {
  const res = git(["merge-base", "--is-ancestor", older, newer]);
  return res.status === 0;
}

function commitsSince(older, newer) {
  const res = git(["log", "--oneline", `${older}..${newer}`]);
  return (res.stdout || "").trim();
}

function atomicWriteJson(targetPath, data) {
  const dir = path.dirname(targetPath);
  fs.mkdirSync(dir, { recursive: true });
  const tmpPath = `${targetPath}.tmp-${process.pid}-${Date.now()}.json`;
  fs.writeFileSync(tmpPath, `${JSON.stringify(data, null, 2)}\n`, "utf8");
  fs.renameSync(tmpPath, targetPath);
}

function loadApproval() {
  if (!fs.existsSync(APPROVAL_PATH)) {
    return {
      schema_version: 1,
      approved_head: null,
      approved_at: null,
      approved_by: "",
      notes: "",
      evidence: {},
      compatibility: {},
    };
  }
  try {
    const raw = JSON.parse(fs.readFileSync(APPROVAL_PATH, "utf8"));
    return raw && typeof raw === "object"
      ? {
          schema_version: Number(raw.schema_version || 1),
          approved_head: typeof raw.approved_head === "string" ? raw.approved_head.trim() : null,
          approved_at: typeof raw.approved_at === "string" ? raw.approved_at.trim() : null,
          approved_by: typeof raw.approved_by === "string" ? raw.approved_by : "",
          notes: typeof raw.notes === "string" ? raw.notes : "",
          evidence: raw.evidence && typeof raw.evidence === "object" ? raw.evidence : {},
          compatibility: raw.compatibility && typeof raw.compatibility === "object" ? raw.compatibility : {},
        }
      : {
          schema_version: 1,
          approved_head: null,
          approved_at: null,
          approved_by: "",
          notes: "",
          evidence: {},
          compatibility: {},
        };
  } catch {
    return {
      schema_version: 1,
      approved_head: null,
      approved_at: null,
      approved_by: "",
      notes: "",
      evidence: {},
      compatibility: {},
    };
  }
}

const compat = JSON.parse(fs.readFileSync(COMPAT_PATH, "utf8"));
const pkg = JSON.parse(fs.readFileSync(PKG_PATH, "utf8"));
const releaseVersion = String(pkg.version || "").trim();
const head = gitRequired(["rev-parse", "HEAD"], "git rev-parse HEAD");
const approval = loadApproval();

if (!releaseVersion) {
  die("package version is empty");
}

const matrix = Array.isArray(compat.matrix) ? compat.matrix : [];
const pendingRows = matrix.filter((entry) => entry.pending_release === true || isShaPlaceholder(entry.quaid_range));

function compatibleRowsForHost(host) {
  return matrix.filter(
    (entry) =>
      (entry.host || "").trim() === host &&
      String(entry.quaid_range || "").trim() === releaseVersion &&
      String(entry.status || "").trim() === "compatible"
  );
}

if (pendingRows.length === 0) {
  for (const host of REQUIRED_HOSTS) {
    const compatibleRows = compatibleRowsForHost(host);
    if (compatibleRows.length === 0) {
      const legacyCompatible = matrix.some(
        (entry) =>
          (entry.host || "").trim() === host &&
          String(entry.status || "").trim() === "compatible"
      );
      if (legacyCompatible) {
        die(
          `host '${host}' only has legacy non-SHA compatibility rows; run a fresh full live clear to record SHA-backed evidence`
        );
      }
      die(`missing pending or promoted compatible row for host '${host}' and Quaid ${releaseVersion}`);
    }
    const validated = compatibleRows
      .map((entry) => String(entry.validated_sha || "").trim())
      .filter(Boolean);
    if (validated.length === 0) {
      die(
        `host '${host}' only has legacy compatibility rows for Quaid ${releaseVersion}; run a fresh full live clear to record SHA-backed evidence`
      );
    }
    if (validated.some((sha) => sha === head)) {
      continue;
    }
    const matchingAncestor = validated.find((sha) => isAncestor(sha, head));
    if (matchingAncestor) {
      if (
        approval.approved_head === head &&
        typeof approval.compatibility?.[host] === "string" &&
        approval.compatibility[host].trim() === matchingAncestor
      ) {
        continue;
      }
      const delta = commitsSince(matchingAncestor, head);
      die(
        `host '${host}' compatibility was last cleared at ${matchingAncestor}, not current HEAD ${head}; commits since clear:\n${delta || "(none)"}`
      );
    }
    die(`host '${host}' has compatibility rows, but none point to an ancestor of release HEAD ${head}`);
  }
  console.log(`[release-compat] already promoted for Quaid ${releaseVersion}`);
  process.exit(0);
}

const failures = [];
for (const host of REQUIRED_HOSTS) {
  if (!pendingRows.some((entry) => (entry.host || "").trim() === host)) {
    failures.push(`missing pending compatibility clear for host '${host}'`);
  }
}

for (const entry of pendingRows) {
  const host = String(entry.host || "").trim() || "(unknown)";
  const clearSha = String(entry.quaid_range || "").trim();
  if (!isShaPlaceholder(clearSha)) {
    failures.push(`${host}: pending row has non-SHA quaid_range '${clearSha}'`);
    continue;
  }
  if (entry.install_verified !== true) {
    failures.push(`${host}: install_verified=false; live clear included manual patching or install was not clean`);
    continue;
  }
  if (clearSha === head) {
    continue;
  }
  if (!isAncestor(clearSha, head)) {
    failures.push(`${host}: clear sha ${clearSha} is not an ancestor of release HEAD ${head}`);
    continue;
  }
  if (
    approval.approved_head === head &&
    typeof approval.compatibility?.[host] === "string" &&
    approval.compatibility[host].trim() === clearSha
  ) {
    continue;
  }
  const delta = commitsSince(clearSha, head);
  failures.push(
    `${host}: clear sha ${clearSha} is behind release HEAD ${head}; commits since clear:\n${delta || "(none)"}`
  );
}

if (failures.length) {
  console.error("[release-compat] PROMOTION BLOCKED");
  for (const failure of failures) {
    console.error(`- ${failure}`);
  }
  process.exit(1);
}

const promotedRows = [];
for (const entry of matrix) {
  const isPending = entry.pending_release === true || isShaPlaceholder(entry.quaid_range);
  if (!isPending) {
    promotedRows.push(entry);
    continue;
  }
  const nextEntry = {
    ...entry,
    quaid_range: releaseVersion,
    validated_sha: String(entry.quaid_range || "").trim(),
    validated_at: entry.cleared_at || new Date().toISOString(),
  };
  delete nextEntry.pending_release;
  delete nextEntry.install_verified;
  delete nextEntry.cleared_at;
  promotedRows.push(nextEntry);
}

const deduped = [];
const seen = new Map();
for (const entry of promotedRows) {
  const key = [
    String(entry.host || "").trim(),
    String(entry.host_range || "").trim(),
    String(entry.quaid_range || "").trim(),
    String(entry.status || "").trim(),
  ].join("|");
  const existingIndex = seen.get(key);
  if (existingIndex === undefined) {
    seen.set(key, deduped.length);
    deduped.push(entry);
    continue;
  }

  const existing = deduped[existingIndex];
  const existingScore = Number(Boolean(existing.validated_sha)) + Number(Boolean(existing.validated_at));
  const nextScore = Number(Boolean(entry.validated_sha)) + Number(Boolean(entry.validated_at));
  if (nextScore > existingScore) {
    deduped[existingIndex] = entry;
    continue;
  }
  if (nextScore === existingScore) {
    const existingStamp = String(existing.validated_at || "");
    const nextStamp = String(entry.validated_at || "");
    if (nextStamp > existingStamp) {
      deduped[existingIndex] = entry;
    }
  }
}

compat.matrix = deduped;
compat.updated_at = todayIso();
atomicWriteJson(COMPAT_PATH, compat);

for (const host of REQUIRED_HOSTS) {
  console.log(`[release-compat] promoted ${host} -> Quaid ${releaseVersion}`);
}
