#!/usr/bin/env node
import fs from "node:fs";
import path from "node:path";
import process from "node:process";

function usage() {
  console.log(`Usage:
  node scripts/replay-compaction-signal.mjs --session-file <path> [--tail <n>]

Description:
  Replays an OpenClaw session JSONL and validates Quaid lifecycle detection for:
  - auto-compaction system notices
  - manual /compact commands

Exit codes:
  0: all expected compact markers detected
  1: one or more expected compact markers were missed
  2: invalid usage / input file errors`);
}

function parseArgs(argv) {
  const out = { sessionFile: "", tail: 0 };
  for (let i = 0; i < argv.length; i++) {
    const arg = argv[i];
    if (arg === "--session-file") {
      out.sessionFile = String(argv[++i] || "");
      continue;
    }
    if (arg === "--tail") {
      const parsed = Number(argv[++i] || "");
      out.tail = Number.isFinite(parsed) && parsed > 0 ? Math.floor(parsed) : 0;
      continue;
    }
    if (arg === "-h" || arg === "--help") {
      usage();
      process.exit(0);
    }
    console.error(`Unknown arg: ${arg}`);
    usage();
    process.exit(2);
  }
  if (!out.sessionFile) {
    console.error("Missing required --session-file");
    usage();
    process.exit(2);
  }
  return out;
}

function getMessageText(msg) {
  const content = msg?.content;
  if (typeof content === "string") return content;
  if (Array.isArray(content)) {
    return content.map((item) => {
      if (typeof item === "string") return item;
      if (item && typeof item === "object" && typeof item.text === "string") return item.text;
      return "";
    }).join("\n");
  }
  return "";
}

function readSessionMessages(jsonlPath) {
  const raw = fs.readFileSync(jsonlPath, "utf8");
  const lines = raw.split("\n").filter(Boolean);
  const out = [];
  for (const line of lines) {
    try {
      const parsed = JSON.parse(line);
      if (parsed?.type === "message" && parsed?.message && typeof parsed.message === "object") {
        out.push(parsed.message);
      } else if (parsed && typeof parsed === "object" && typeof parsed.role === "string") {
        out.push(parsed);
      }
    } catch {
      // ignore malformed lines in replay mode
    }
  }
  return out;
}

function hasCompactionMarker(msg) {
  const text = getMessageText(msg);
  if (!text) return false;
  const normalized = text.toLowerCase();
  if (normalized.includes("/compact")) return true;
  return /\bcompacted\b/.test(normalized) && (/\(\s*[\d.]+k?\s*(?:->|→)\s*[\d.]+k?\s*\)/i.test(text) || /\bcontext\b/i.test(text));
}

function preview(msg) {
  return getMessageText(msg).replace(/\s+/g, " ").trim().slice(0, 140);
}

function detectLifecycleCommandSignal(messages) {
  if (!Array.isArray(messages) || messages.length === 0) return null;
  const recent = messages.slice(-8);

  for (const msg of recent) {
    const text = getMessageText(msg);
    if (!text) continue;
    const normalized = text.trim().toLowerCase();
    const cmdMatch = normalized.match(/(?:^|\s)\/(new|reset|restart|compact)(?=\s|$)/);
    if (!cmdMatch) continue;
    const command = `/${cmdMatch[1]}`;
    if (command === "/new" || command === "/reset" || command === "/restart") return "ResetSignal";
    if (command === "/compact") return "CompactionSignal";
  }

  for (const msg of recent) {
    const text = getMessageText(msg);
    if (!text) continue;
    const normalized = text.trim().toLowerCase();
    const hasCompactedWord = /\bcompacted\b/.test(normalized);
    const hasReductionPattern = /\(\s*[\d.]+k?\s*(?:->|→)\s*[\d.]+k?\s*\)/i.test(text);
    const hasContextHint = /\bcontext\b/i.test(text);
    if (hasCompactedWord && (hasReductionPattern || hasContextHint)) {
      return "CompactionSignal";
    }
  }

  return null;
}

function main() {
  const { sessionFile, tail } = parseArgs(process.argv.slice(2));
  const fp = path.resolve(sessionFile);
  if (!fs.existsSync(fp)) {
    console.error(`Session file not found: ${fp}`);
    process.exit(2);
  }

  let messages = readSessionMessages(fp);
  if (tail > 0 && messages.length > tail) {
    messages = messages.slice(-tail);
  }
  if (!messages.length) {
    console.error("No messages parsed from session JSONL.");
    process.exit(2);
  }

  const markerIndexes = [];
  for (let i = 0; i < messages.length; i++) {
    if (hasCompactionMarker(messages[i])) markerIndexes.push(i);
  }

  if (!markerIndexes.length) {
    console.log("No compact markers found in provided messages.");
    process.exit(0);
  }

  let failures = 0;
  console.log(`Found ${markerIndexes.length} compact marker(s) in ${messages.length} messages:`);
  for (const idx of markerIndexes) {
    const window = messages.slice(Math.max(0, idx - 7), idx + 1);
    const signal = detectLifecycleCommandSignal(window);
    const ok = signal === "CompactionSignal";
    const role = String(messages[idx]?.role || "unknown");
    console.log(`- idx=${idx} role=${role} signal=${signal || "null"} ok=${ok ? "yes" : "no"} text="${preview(messages[idx])}"`);
    if (!ok) failures++;
  }

  if (failures > 0) {
    console.error(`FAILED: ${failures} compact marker(s) were not detected as CompactionSignal.`);
    process.exit(1);
  }
  console.log("PASS: all compact markers detected as CompactionSignal.");
}

main();
