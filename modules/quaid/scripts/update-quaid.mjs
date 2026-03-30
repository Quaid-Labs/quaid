#!/usr/bin/env node
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { spawnSync } from "node:child_process";
import { fileURLToPath, pathToFileURL } from "node:url";

const __filename = fileURLToPath(import.meta.url);
const __dirname = path.dirname(__filename);
const PLUGIN_ROOT = path.resolve(__dirname, "..");
const DEFAULT_REPO = "quaid-labs/quaid";

function parseArgs(argv) {
  const out = {
    check: false,
    dryRun: false,
    repo: "",
    tag: "",
    workspace: "",
    keepTemp: false,
    force: false,
    help: false,
  };
  for (let i = 0; i < argv.length; i++) {
    const arg = String(argv[i] || "");
    if (arg === "--check") { out.check = true; continue; }
    if (arg === "--dry-run") { out.dryRun = true; continue; }
    if (arg === "--keep-temp") { out.keepTemp = true; continue; }
    if (arg === "--force") { out.force = true; continue; }
    if (arg === "-h" || arg === "--help") { out.help = true; continue; }
    if (arg === "--repo") { out.repo = String(argv[++i] || "").trim(); continue; }
    if (arg.startsWith("--repo=")) { out.repo = arg.slice("--repo=".length).trim(); continue; }
    if (arg === "--tag") { out.tag = String(argv[++i] || "").trim(); continue; }
    if (arg.startsWith("--tag=")) { out.tag = arg.slice("--tag=".length).trim(); continue; }
    if (arg === "--workspace") { out.workspace = String(argv[++i] || "").trim(); continue; }
    if (arg.startsWith("--workspace=")) { out.workspace = arg.slice("--workspace=".length).trim(); continue; }
    throw new Error(`Unknown option: ${arg}`);
  }
  return out;
}

function printHelp() {
  console.log(`Usage: quaid update [options]

Options:
  --check              Check latest release without installing
  --tag <tag>          Update to a specific git tag/ref (default: latest release)
  --repo <owner/repo>  GitHub repo (default: ${DEFAULT_REPO})
  --workspace <path>   Override QUAID_HOME/CLAWDBOT_WORKSPACE during update
  --dry-run            Show actions without executing install
  --force              Install even when versions match
  --keep-temp          Keep extracted temporary files (debug)
  -h, --help           Show this help
`);
}

function readCurrentVersion(pluginRoot = PLUGIN_ROOT) {
  const pkgPath = path.join(pluginRoot, "package.json");
  if (!fs.existsSync(pkgPath)) return "";
  try {
    const raw = JSON.parse(fs.readFileSync(pkgPath, "utf8"));
    return String(raw?.version || "").trim();
  } catch {
    return "";
  }
}

function normalizeTag(tag) {
  return String(tag || "").trim().replace(/^v/, "");
}

function parseSemver(v) {
  const m = String(v || "").trim().match(/^(\d+)\.(\d+)\.(\d+)(?:-([0-9A-Za-z.-]+))?$/);
  if (!m) return null;
  return {
    major: Number(m[1]),
    minor: Number(m[2]),
    patch: Number(m[3]),
    pre: m[4] || "",
  };
}

function isNewerVersion(latest, current) {
  const l = parseSemver(normalizeTag(latest));
  const c = parseSemver(normalizeTag(current));
  if (!l || !c) return normalizeTag(latest) !== normalizeTag(current);
  if (l.major !== c.major) return l.major > c.major;
  if (l.minor !== c.minor) return l.minor > c.minor;
  if (l.patch !== c.patch) return l.patch > c.patch;
  if (!l.pre && c.pre) return true;      // stable > prerelease
  if (l.pre && !c.pre) return false;     // prerelease < stable
  return l.pre > c.pre;
}

function resolveWorkspace(explicitWorkspace = "") {
  const explicit = String(explicitWorkspace || "").trim();
  if (explicit) return explicit;
  const envHome = String(process.env.QUAID_HOME || "").trim();
  if (envHome) return envHome;
  const envWorkspace = String(process.env.CLAWDBOT_WORKSPACE || "").trim();
  if (envWorkspace) return envWorkspace;
  return path.resolve(PLUGIN_ROOT, "..", "..");
}

function buildTarballUrl(repo, tag) {
  const safeRepo = String(repo || DEFAULT_REPO).trim();
  const safeTag = String(tag || "main").trim();
  return `https://codeload.github.com/${safeRepo}/tar.gz/${safeTag}`;
}

async function fetchLatestRelease(repo) {
  const targetRepo = String(repo || DEFAULT_REPO).trim();
  const url = `https://api.github.com/repos/${targetRepo}/releases/latest`;
  const resp = await fetch(url, {
    headers: {
      Accept: "application/vnd.github+json",
      "User-Agent": "quaid-updater",
    },
  });
  if (!resp.ok) {
    throw new Error(`GitHub releases/latest failed: ${resp.status} ${resp.statusText}`);
  }
  const body = await resp.json();
  if (!body || typeof body !== "object") {
    throw new Error("GitHub releases/latest returned non-object payload");
  }
  const tagName = String(body.tag_name || "").trim();
  if (!tagName) {
    throw new Error("GitHub releases/latest missing tag_name");
  }
  return {
    tag: tagName,
    version: normalizeTag(tagName),
    htmlUrl: String(body.html_url || ""),
    tarballUrl: buildTarballUrl(targetRepo, tagName),
  };
}

async function downloadFile(url, outPath) {
  const resp = await fetch(url, { headers: { "User-Agent": "quaid-updater" } });
  if (!resp.ok) {
    throw new Error(`download failed: ${resp.status} ${resp.statusText}`);
  }
  const arr = new Uint8Array(await resp.arrayBuffer());
  fs.writeFileSync(outPath, arr);
}

function extractTarGz(archivePath, extractDir) {
  const res = spawnSync("tar", ["-xzf", archivePath, "-C", extractDir], {
    stdio: "pipe",
    encoding: "utf8",
  });
  if (res.status !== 0) {
    const detail = `${res.stderr || ""}\n${res.stdout || ""}`.trim();
    throw new Error(`tar extract failed: ${detail || "unknown error"}`);
  }
}

function findExtractedRoot(extractDir) {
  const entries = fs.readdirSync(extractDir, { withFileTypes: true });
  for (const ent of entries) {
    if (!ent.isDirectory()) continue;
    const candidate = path.join(extractDir, ent.name);
    const setupPath = path.join(candidate, "setup-quaid.mjs");
    const pluginPath = path.join(candidate, "modules", "quaid");
    if (fs.existsSync(setupPath) && fs.existsSync(pluginPath)) return candidate;
  }
  return "";
}

function runInstallerFromExtracted(sourceRoot, opts) {
  const setupPath = path.join(sourceRoot, "setup-quaid.mjs");
  if (!fs.existsSync(setupPath)) {
    throw new Error(`setup-quaid.mjs missing from extracted source: ${sourceRoot}`);
  }
  const workspace = resolveWorkspace(opts.workspace);
  const args = [setupPath, "--source", "local", "--workspace", workspace, "--agent"];
  const instance = String(process.env.QUAID_INSTANCE || "").trim();
  if (instance.startsWith("claude-code")) {
    args.push("--claude-code");
  }
  if (opts.dryRun) {
    console.log(`[dry-run] node ${args.map((a) => JSON.stringify(a)).join(" ")}`);
    return 0;
  }
  const res = spawnSync("node", args, {
    stdio: "inherit",
    env: {
      ...process.env,
      QUAID_INSTALL_AGENT: "1",
    },
  });
  return Number(res.status || 0);
}

async function main(argv = process.argv.slice(2)) {
  const opts = parseArgs(argv);
  if (opts.help) {
    printHelp();
    return 0;
  }

  const repo = opts.repo || process.env.QUAID_UPDATE_REPO || DEFAULT_REPO;
  const current = readCurrentVersion();
  const release = opts.tag
    ? {
        tag: opts.tag,
        version: normalizeTag(opts.tag),
        htmlUrl: `https://github.com/${repo}/releases`,
        tarballUrl: buildTarballUrl(repo, opts.tag),
      }
    : await fetchLatestRelease(repo);

  const newer = isNewerVersion(release.version, current);
  if (opts.check) {
    if (!current) {
      console.log(`latest=${release.version} tag=${release.tag} repo=${repo}`);
      return 0;
    }
    const state = newer ? "update-available" : "up-to-date";
    console.log(`${state}: current=${current} latest=${release.version} tag=${release.tag}`);
    if (release.htmlUrl) console.log(`release=${release.htmlUrl}`);
    return newer ? 10 : 0;
  }

  if (!opts.force && current && !newer) {
    console.log(`Already up to date (current=${current}, latest=${release.version}).`);
    return 0;
  }

  const tmpBase = fs.mkdtempSync(path.join(os.tmpdir(), "quaid-update-"));
  const archivePath = path.join(tmpBase, "source.tar.gz");
  const extractDir = path.join(tmpBase, "extract");
  fs.mkdirSync(extractDir, { recursive: true });

  try {
    console.log(`Downloading ${release.tag} from ${repo}...`);
    await downloadFile(release.tarballUrl, archivePath);
    extractTarGz(archivePath, extractDir);
    const sourceRoot = findExtractedRoot(extractDir);
    if (!sourceRoot) {
      throw new Error(`Could not locate extracted source root in ${extractDir}`);
    }
    const code = runInstallerFromExtracted(sourceRoot, opts);
    if (code !== 0) {
      throw new Error(`Installer exited with status ${code}`);
    }
    console.log(`Update complete: ${current || "unknown"} -> ${release.version}`);
    if (release.htmlUrl) console.log(`Release notes: ${release.htmlUrl}`);
    return 0;
  } finally {
    if (opts.keepTemp) {
      console.log(`Kept temp files: ${tmpBase}`);
    } else {
      fs.rmSync(tmpBase, { recursive: true, force: true });
    }
  }
}

export const __test = {
  parseArgs,
  readCurrentVersion,
  normalizeTag,
  parseSemver,
  isNewerVersion,
  resolveWorkspace,
  buildTarballUrl,
  findExtractedRoot,
};

if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href) {
  main().then((code) => {
    process.exit(code);
  }).catch((err) => {
    console.error(`[quaid-update] ${String(err?.message || err)}`);
    process.exit(1);
  });
}
