import { describe, expect, it } from "vitest";
import path from "node:path";
import { mkdtempSync, mkdirSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";

import { __test } from "../scripts/update-quaid.mjs";

describe("quaid update helpers", () => {
  it("parses update args", () => {
    const parsed = __test.parseArgs([
      "--check",
      "--repo",
      "Quaid-Labs/quaid",
      "--tag=v0.2.16-alpha",
      "--dry-run",
      "--workspace",
      "/tmp/quaid-home",
    ]);
    expect(parsed.check).toBe(true);
    expect(parsed.repo).toBe("Quaid-Labs/quaid");
    expect(parsed.tag).toBe("v0.2.16-alpha");
    expect(parsed.dryRun).toBe(true);
    expect(parsed.workspace).toBe("/tmp/quaid-home");
  });

  it("compares versions with prerelease awareness", () => {
    expect(__test.isNewerVersion("0.2.16-alpha", "0.2.15-alpha")).toBe(true);
    expect(__test.isNewerVersion("0.2.15", "0.2.15-alpha")).toBe(true);
    expect(__test.isNewerVersion("0.2.15-alpha", "0.2.15")).toBe(false);
    expect(__test.isNewerVersion("0.2.15-alpha", "0.2.15-alpha")).toBe(false);
  });

  it("builds codeload tarball URL", () => {
    const url = __test.buildTarballUrl("quaid-labs/quaid", "v0.2.16-alpha");
    expect(url).toBe("https://codeload.github.com/quaid-labs/quaid/tar.gz/v0.2.16-alpha");
  });

  it("finds extracted source root by setup script and modules/quaid", () => {
    const base = mkdtempSync(path.join(tmpdir(), "quaid-update-test-"));
    const extracted = path.join(base, "extract");
    const repoRoot = path.join(extracted, "quaid-labs-quaid-abc123");
    mkdirSync(path.join(repoRoot, "modules", "quaid"), { recursive: true });
    writeFileSync(path.join(repoRoot, "setup-quaid.mjs"), "console.log('ok');\n", "utf8");

    expect(__test.findExtractedRoot(extracted)).toBe(repoRoot);
  });
});
