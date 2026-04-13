import fs from "node:fs";
import path from "node:path";

export const SHARED_AUTH_KIND_LABELS = {
  anthropic_oauth: "Anthropic OAuth token",
  anthropic_api: "Anthropic API key",
  codex_oauth: "Codex OAuth token",
  openai_api: "OpenAI API key",
};

export function sharedAuthRegistryPath(workspace) {
  return path.join(String(workspace || "").trim(), "shared", "auth", "credentials.json");
}

export function readSharedAuthRegistry(workspace) {
  const registryPath = sharedAuthRegistryPath(workspace);
  try {
    const raw = fs.readFileSync(registryPath, "utf8");
    const data = JSON.parse(raw);
    return data && typeof data === "object" ? data : {};
  } catch {
    return {};
  }
}

export function getSharedAuthCredential(workspace, kinds = []) {
  const data = readSharedAuthRegistry(workspace);
  const creds = data && typeof data.credentials === "object" ? data.credentials : {};
  for (const kind of kinds) {
    const payload = creds?.[kind];
    if (payload && typeof payload === "object") {
      const token = String(payload.token || "").trim();
      if (token) return { kind, token };
    } else if (typeof payload === "string" && payload.trim()) {
      return { kind, token: payload.trim() };
    }
  }
  return null;
}

export function writeSharedAuthCredential(workspace, kind, token) {
  const normalizedKind = String(kind || "").trim();
  const value = String(token || "").trim();
  if (!normalizedKind) throw new Error("auth credential kind is required");
  if (!value) throw new Error("auth credential token is required");
  const registryPath = sharedAuthRegistryPath(workspace);
  const data = readSharedAuthRegistry(workspace);
  const creds = data && typeof data.credentials === "object" ? data.credentials : {};
  creds[normalizedKind] = { token: value };
  data.credentials = creds;
  fs.mkdirSync(path.dirname(registryPath), { recursive: true });
  fs.writeFileSync(registryPath, JSON.stringify(data, null, 2) + "\n", { encoding: "utf8", mode: 0o600 });
  fs.chmodSync(registryPath, 0o600);
  return registryPath;
}

export function inferSharedAuthKind(token) {
  const value = String(token || "").trim();
  if (!value) return "";
  if (value.startsWith("sk-ant-oat")) return "anthropic_oauth";
  if (value.startsWith("sk-ant-")) return "anthropic_api";
  if ((value.match(/\./g) || []).length >= 2) return "codex_oauth";
  if (value.startsWith("sk-")) return "openai_api";
  return "";
}
