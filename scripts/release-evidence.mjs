#!/usr/bin/env node
import fs from "node:fs";
import path from "node:path";
import { spawnSync } from "node:child_process";
import { fileURLToPath } from "node:url";

const __filename = fileURLToPath(import.meta.url);
const ROOT = path.resolve(path.dirname(__filename), "..");
const EVIDENCE_PATH = process.env.QUAID_RELEASE_EVIDENCE_PATH || path.join(ROOT, "release-evidence.json");
const VALID_SLOTS = new Set(["unit", "ci", "xp"]);

function usage() {
  console.log(`Usage:
  node scripts/release-evidence.mjs show
  node scripts/release-evidence.mjs check [--require unit,ci,xp]
  node scripts/release-evidence.mjs record <unit|ci|xp> [--sha <commit>] [--notes <text>]
`);
}

function die(message, code = 1) {
  console.error(`[release-evidence] ${message}`);
  process.exit(code);
}

function git(args) {
  const res = spawnSync("git", args, {
    cwd: ROOT,
    env: process.env,
    encoding: "utf8",
  });
  return res;
}

function gitRequired(args, label) {
  const res = git(args);
  if (res.status !== 0) {
    die(`${label} failed: ${(res.stderr || res.stdout || "").trim()}`);
  }
  return (res.stdout || "").trim();
}

function nowIso() {
  return new Date().toISOString();
}

function todayIso() {
  return new Date().toISOString().slice(0, 10);
}

function defaultData() {
  return {
    schema_version: 1,
    updated_at: null,
    evidence: {
      unit: { sha: null, recorded_at: null, notes: "" },
      ci: { sha: null, recorded_at: null, notes: "" },
      xp: { sha: null, recorded_at: null, notes: "" },
    },
  };
}

function loadEvidence() {
  if (!fs.existsSync(EVIDENCE_PATH)) {
    return defaultData();
  }
  const raw = JSON.parse(fs.readFileSync(EVIDENCE_PATH, "utf8"));
  const data = defaultData();
  data.schema_version = Number(raw.schema_version || 1);
  data.updated_at = raw.updated_at || null;
  for (const slot of VALID_SLOTS) {
    const src = raw.evidence?.[slot] || {};
    data.evidence[slot] = {
      sha: typeof src.sha === "string" && src.sha.trim() ? src.sha.trim() : null,
      recorded_at: typeof src.recorded_at === "string" && src.recorded_at.trim() ? src.recorded_at.trim() : null,
      notes: typeof src.notes === "string" ? src.notes : "",
    };
  }
  return data;
}

function atomicWriteJson(targetPath, data) {
  const dir = path.dirname(targetPath);
  fs.mkdirSync(dir, { recursive: true });
  const tmpPath = `${targetPath}.tmp-${process.pid}-${Date.now()}.json`;
  fs.writeFileSync(tmpPath, `${JSON.stringify(data, null, 2)}\n`, "utf8");
  fs.renameSync(tmpPath, targetPath);
}

function parseArgs(argv) {
  const args = [...argv];
  const positionals = [];
  const opts = {};
  while (args.length) {
    const token = args.shift();
    if (!token.startsWith("--")) {
      positionals.push(token);
      continue;
    }
    const key = token.slice(2);
    if (!args.length || args[0].startsWith("--")) {
      opts[key] = true;
      continue;
    }
    opts[key] = args.shift();
  }
  return { positionals, opts };
}

function isAncestor(older, newer) {
  const res = git(["merge-base", "--is-ancestor", older, newer]);
  return res.status === 0;
}

function commitsSince(older, newer) {
  const res = git(["log", "--oneline", `${older}..${newer}`]);
  return (res.stdout || "").trim();
}

function commandShow() {
  const data = loadEvidence();
  console.log(`[release-evidence] path=${path.relative(ROOT, EVIDENCE_PATH)}`);
  console.log(`[release-evidence] updated_at=${data.updated_at || "(unset)"}`);
  for (const slot of VALID_SLOTS) {
    const entry = data.evidence[slot];
    console.log(
      `[release-evidence] ${slot}: sha=${entry.sha || "(missing)"} recorded_at=${entry.recorded_at || "(missing)"} notes=${entry.notes || "(none)"}`
    );
  }
}

function commandRecord(slot, opts) {
  if (!VALID_SLOTS.has(slot)) {
    die(`invalid slot '${slot}' (expected one of: ${Array.from(VALID_SLOTS).join(", ")})`);
  }
  const sha = String(opts.sha || gitRequired(["rev-parse", "HEAD"], "git rev-parse HEAD")).trim();
  if (!sha) {
    die("could not resolve HEAD sha");
  }
  const data = loadEvidence();
  data.evidence[slot] = {
    sha,
    recorded_at: nowIso(),
    notes: String(opts.notes || ""),
  };
  data.updated_at = todayIso();
  atomicWriteJson(EVIDENCE_PATH, data);
  console.log(`[release-evidence] recorded ${slot} sha=${sha}`);
}

function commandCheck(opts) {
  const required = String(opts.require || "unit,ci,xp")
    .split(",")
    .map((item) => item.trim())
    .filter(Boolean);
  for (const slot of required) {
    if (!VALID_SLOTS.has(slot)) {
      die(`invalid required slot '${slot}'`);
    }
  }

  const head = gitRequired(["rev-parse", "HEAD"], "git rev-parse HEAD");
  const data = loadEvidence();
  const failures = [];

  for (const slot of required) {
    const entry = data.evidence[slot];
    if (!entry?.sha) {
      failures.push(`${slot}: missing evidence sha`);
      continue;
    }
    if (entry.sha === head) {
      console.log(`[release-evidence] ${slot}: exact match ${head}`);
      continue;
    }
    if (!isAncestor(entry.sha, head)) {
      failures.push(`${slot}: evidence sha ${entry.sha} is not an ancestor of HEAD ${head}`);
      continue;
    }
    const delta = commitsSince(entry.sha, head);
    const rendered = delta ? `\n${delta}` : "\n(no commits listed)";
    failures.push(`${slot}: evidence sha ${entry.sha} is behind HEAD ${head}; commits since clear:${rendered}`);
  }

  if (failures.length) {
    console.error("[release-evidence] CHECK FAILED");
    for (const failure of failures) {
      console.error(`- ${failure}`);
    }
    process.exit(1);
  }

  console.log("[release-evidence] CHECK PASS");
}

const { positionals, opts } = parseArgs(process.argv.slice(2));
const command = positionals[0];

if (!command || command === "-h" || command === "--help") {
  usage();
  process.exit(command ? 0 : 1);
}

if (command === "show") {
  commandShow();
} else if (command === "record") {
  const slot = positionals[1];
  if (!slot) {
    die("record requires a slot");
  }
  commandRecord(slot, opts);
} else if (command === "check") {
  commandCheck(opts);
} else {
  usage();
  die(`unknown command '${command}'`);
}
