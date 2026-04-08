#!/usr/bin/env node
import fs from "node:fs";
import path from "node:path";
import { spawnSync } from "node:child_process";
import { fileURLToPath } from "node:url";

const __filename = fileURLToPath(import.meta.url);
const ROOT = path.resolve(path.dirname(__filename), "..");
const PKG_PATH = path.join(ROOT, "modules", "quaid", "package.json");
const VERSION_PATH = path.join(ROOT, "modules", "quaid", "VERSION");
const SETUP_MJS_PATH = path.join(ROOT, "setup-quaid.mjs");
const SETUP_SH_PATH = path.join(ROOT, "setup-quaid.sh");
const README_PATH = path.join(ROOT, "README.md");
const EVIDENCE_PATH = path.join(ROOT, "release-evidence.json");
const COMPAT_PATH = path.join(ROOT, "compatibility.json");
const APPROVAL_PATH =
  process.env.QUAID_RELEASE_APPROVAL_PATH || path.join(ROOT, ".release-approval.local.json");

const REQUIRED_EVIDENCE = ["unit", "ci", "xp"];
const REQUIRED_HOSTS = ["openclaw", "claude-code", "codex"];

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
    throw new Error(`${label} failed: ${(res.stderr || res.stdout || "").trim()}`);
  }
  return (res.stdout || "").trim();
}

function read(relPath) {
  return fs.readFileSync(path.join(ROOT, relPath), "utf8");
}

function readJsonFile(filePath, fallback) {
  if (!fs.existsSync(filePath)) return fallback;
  try {
    const parsed = JSON.parse(fs.readFileSync(filePath, "utf8"));
    return parsed && typeof parsed === "object" ? parsed : fallback;
  } catch {
    return fallback;
  }
}

function match(text, regex, label) {
  const found = text.match(regex);
  if (!found) throw new Error(`could not parse ${label}`);
  return found[1];
}

function isSha(value) {
  return typeof value === "string" && /^[0-9a-f]{7,40}$/i.test(value.trim());
}

function isAncestor(older, newer) {
  const res = git(["merge-base", "--is-ancestor", older, newer]);
  return res.status === 0;
}

function countCommits(older, newer) {
  const res = git(["rev-list", "--count", `${older}..${newer}`]);
  if (res.status !== 0) return "?";
  return (res.stdout || "").trim() || "0";
}

function statusLine(kind, label, detail) {
  const prefix = kind.padEnd(4, " ");
  console.log(`${prefix} ${label}${detail ? ` — ${detail}` : ""}`);
}

function summarizeEvidence(head, evidence, approval) {
  console.log("\nEvidence:");
  for (const slot of REQUIRED_EVIDENCE) {
    const entry = evidence.evidence?.[slot] || {};
    const sha = String(entry.sha || "").trim();
    if (!sha) {
      statusLine("MISS", slot, `record with: node scripts/release-evidence.mjs record ${slot}`);
      continue;
    }
    if (sha === head) {
      statusLine("OK", slot, `exact at ${sha.slice(0, 12)}`);
      continue;
    }
    if (!isAncestor(sha, head)) {
      statusLine("FAIL", slot, `${sha.slice(0, 12)} is not an ancestor of HEAD`);
      continue;
    }
    const approved =
      approval.approved_head === head
      && typeof approval.evidence?.[slot] === "string"
      && approval.evidence[slot].trim() === sha;
    const commits = countCommits(sha, head);
    if (approved) {
      statusLine("OK", slot, `approved ancestor ${sha.slice(0, 12)} (+${commits} commit${commits === "1" ? "" : "s"})`);
      continue;
    }
    statusLine("HOLD", slot, `behind HEAD at ${sha.slice(0, 12)} (+${commits}); rerun or approve delta`);
  }
}

function summarizeCompatibility(head, version, compat, approval) {
  console.log("\nCompatibility:");
  const matrix = Array.isArray(compat.matrix) ? compat.matrix : [];
  for (const host of REQUIRED_HOSTS) {
    const promoted = matrix.find(
      (entry) =>
        String(entry.host || "").trim() === host
        && String(entry.status || "").trim() === "compatible"
        && String(entry.quaid_range || "").trim() === version
    );
    if (promoted) {
      const validatedSha = String(promoted.validated_sha || "").trim();
      if (!validatedSha) {
        statusLine("WARN", host, `promoted ${version} row has no validated_sha`);
        continue;
      }
      if (validatedSha === head) {
        statusLine("OK", host, `promoted for ${version} from ${validatedSha.slice(0, 12)}`);
        continue;
      }
      if (!isAncestor(validatedSha, head)) {
        statusLine("FAIL", host, `validated_sha ${validatedSha.slice(0, 12)} is not an ancestor of HEAD`);
        continue;
      }
      const approved =
        approval.approved_head === head
        && typeof approval.compatibility?.[host] === "string"
        && approval.compatibility[host].trim() === validatedSha;
      const commits = countCommits(validatedSha, head);
      if (approved) {
        statusLine("OK", host, `promoted ancestor ${validatedSha.slice(0, 12)} approved (+${commits})`);
        continue;
      }
      statusLine("HOLD", host, `promoted row validated at ${validatedSha.slice(0, 12)} (+${commits}); rerun or approve delta`);
      continue;
    }

    const pending = matrix.find(
      (entry) =>
        String(entry.host || "").trim() === host
        && String(entry.status || "").trim() === "compatible"
        && (entry.pending_release === true || isSha(String(entry.quaid_range || "").trim()))
    );
    if (!pending) {
      statusLine("MISS", host, `record with: node scripts/record-compatibility-clear.mjs --host ${host} --host-version <version> --install-verified true`);
      continue;
    }
    const clearSha = String(pending.quaid_range || "").trim();
    if (pending.install_verified !== true) {
      statusLine("FAIL", host, `pending clear at ${clearSha.slice(0, 12)} is install_verified=false`);
      continue;
    }
    if (clearSha === head) {
      statusLine("OK", host, `pending clear matches HEAD ${clearSha.slice(0, 12)}; ready for promotion`);
      continue;
    }
    if (!isAncestor(clearSha, head)) {
      statusLine("FAIL", host, `pending clear ${clearSha.slice(0, 12)} is not an ancestor of HEAD`);
      continue;
    }
    const approved =
      approval.approved_head === head
      && typeof approval.compatibility?.[host] === "string"
      && approval.compatibility[host].trim() === clearSha;
    const commits = countCommits(clearSha, head);
    if (approved) {
      statusLine("OK", host, `pending clear ${clearSha.slice(0, 12)} approved (+${commits}); ready for promotion`);
      continue;
    }
    statusLine("HOLD", host, `pending clear ${clearSha.slice(0, 12)} is behind HEAD (+${commits}); rerun or approve delta`);
  }
}

function main() {
  const branch = gitRequired(["rev-parse", "--abbrev-ref", "HEAD"], "git rev-parse --abbrev-ref HEAD");
  const head = gitRequired(["rev-parse", "HEAD"], "git rev-parse HEAD");
  const worktreeStatus = gitRequired(["status", "--short"], "git status --short");

  const pkg = JSON.parse(fs.readFileSync(PKG_PATH, "utf8"));
  const version = String(pkg.version || "").trim();
  const versionFile = fs.readFileSync(VERSION_PATH, "utf8").trim();
  const setupMjsVersion = match(fs.readFileSync(SETUP_MJS_PATH, "utf8"), /const VERSION = "([^"]+)";/, "setup-quaid.mjs VERSION");
  const setupShVersion = match(fs.readFileSync(SETUP_SH_PATH, "utf8"), /QUAID_VERSION="([^"]+)"/, "setup-quaid.sh QUAID_VERSION");
  const readme = fs.readFileSync(README_PATH, "utf8");
  const readmeMarker = match(readme, /Known limitations for \*\*(v[^*]+)\*\*/, "README release marker");
  const releaseNotesRel = `docs/releases/v${version}.md`;
  const releasePostRel = `docs/releases/v${version}-release-post.md`;
  const compat = readJsonFile(COMPAT_PATH, { matrix: [] });
  const latestQuaid = String(compat.latest_quaid || "").trim();
  const evidence = readJsonFile(EVIDENCE_PATH, { evidence: {} });
  const approval = readJsonFile(APPROVAL_PATH, {});

  console.log(`Release status for ${version}`);
  console.log(`HEAD   ${head}`);
  console.log(`Branch ${branch}`);
  statusLine(worktreeStatus ? "DIRTY" : "OK", "worktree", worktreeStatus ? "uncommitted changes present" : "clean");

  console.log("\nVersion sync:");
  statusLine(versionFile === version ? "OK" : "FAIL", "modules/quaid/VERSION", versionFile);
  statusLine(setupMjsVersion === version ? "OK" : "FAIL", "setup-quaid.mjs", setupMjsVersion);
  statusLine(setupShVersion === version ? "OK" : "FAIL", "setup-quaid.sh", setupShVersion);
  statusLine(readmeMarker === `v${version}` ? "OK" : "FAIL", "README marker", readmeMarker);
  statusLine(fs.existsSync(path.join(ROOT, releaseNotesRel)) ? "OK" : "MISS", "release notes", releaseNotesRel);
  statusLine(fs.existsSync(path.join(ROOT, releasePostRel)) ? "OK" : "MISS", "release post", releasePostRel);
  statusLine(latestQuaid === version ? "OK" : "WARN", "compatibility latest_quaid", latestQuaid || "(unset)");

  summarizeEvidence(head, evidence, approval);
  summarizeCompatibility(head, version, compat, approval);

  console.log("\nSuggested next commands:");
  console.log("- node scripts/release-evidence.mjs record unit");
  console.log("- node scripts/release-evidence.mjs record ci");
  console.log("- node scripts/release-evidence.mjs record xp");
  console.log('- node scripts/release-approve-delta.mjs --notes "Maintainer approved the post-clear release delta"');
  console.log("- bash scripts/release-check.sh");
}

main();
