import { describe, expect, it } from "vitest";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";

import {
  captureOpenClawMatrixConfig,
  restoreOpenClawMatrixConfig,
} from "../../../lib/openclaw-matrix-config.mjs";

function writeJson(filePath: string, value: unknown): void {
  fs.mkdirSync(path.dirname(filePath), { recursive: true });
  fs.writeFileSync(filePath, `${JSON.stringify(value, null, 2)}\n`, "utf8");
}

describe("OpenClaw matrix config preservation", () => {
  it("captures and restores matrix plugin/channel state without disturbing unrelated config", () => {
    const root = fs.mkdtempSync(path.join(os.tmpdir(), "quaid-oc-matrix-"));
    const cfgPath = path.join(root, "openclaw.json");
    writeJson(cfgPath, {
      plugins: {
        allow: ["quaid", "matrix"],
        entries: {
          quaid: { enabled: true },
          matrix: { enabled: true },
        },
        slots: { memory: "quaid" },
      },
      channels: {
        matrix: {
          enabled: true,
          homeserver: "http://127.0.0.1:8008",
          accessToken: "secret",
          network: { dangerouslyAllowPrivateNetwork: true },
        },
      },
      gateway: { http: { endpoints: { responses: { enabled: true } } } },
    });

    const snapshot = captureOpenClawMatrixConfig(cfgPath);
    writeJson(cfgPath, {
      plugins: {
        allow: ["quaid"],
        entries: {
          quaid: { enabled: true },
        },
        slots: { memory: "quaid" },
      },
      channels: null,
      gateway: { http: { endpoints: { responses: { enabled: true } } } },
    });

    const result = restoreOpenClawMatrixConfig(cfgPath, snapshot);
    const cfg = JSON.parse(fs.readFileSync(cfgPath, "utf8"));

    expect(result.changed).toBe(true);
    expect(result.restoredAllow).toBe(true);
    expect(result.restoredEntry).toBe(true);
    expect(result.restoredChannel).toBe(true);
    expect(cfg.plugins.allow).toEqual(["quaid", "matrix"]);
    expect(cfg.plugins.entries.matrix.enabled).toBe(true);
    expect(cfg.channels.matrix.homeserver).toBe("http://127.0.0.1:8008");
    expect(cfg.gateway.http.endpoints.responses.enabled).toBe(true);
    fs.rmSync(root, { recursive: true, force: true });
  });

  it("does nothing when there was no matrix state to preserve", () => {
    const root = fs.mkdtempSync(path.join(os.tmpdir(), "quaid-oc-matrix-"));
    const cfgPath = path.join(root, "openclaw.json");
    writeJson(cfgPath, {
      plugins: { allow: ["quaid"], entries: { quaid: { enabled: true } } },
    });

    const snapshot = captureOpenClawMatrixConfig(cfgPath);
    const result = restoreOpenClawMatrixConfig(cfgPath, snapshot);

    expect(snapshot).toBeNull();
    expect(result.changed).toBe(false);
    expect(result.reason).toBe("missing-snapshot");
    fs.rmSync(root, { recursive: true, force: true });
  });
});
