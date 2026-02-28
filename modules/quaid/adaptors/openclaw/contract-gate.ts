export type ContractSurface =
  | "tools"
  | "events"
  | "api"
  | "ingest_triggers"
  | "auth_requirements"
  | "migrations"
  | "notifications";

export function normalizeDeclaredExports(raw: unknown): Set<string> {
  if (!Array.isArray(raw)) return new Set<string>();
  const out = new Set<string>();
  for (const item of raw) {
    const token = String(item || "").trim();
    if (!token) continue;
    out.add(token);
  }
  return out;
}

export function assertDeclaredRegistration(
  surface: ContractSurface,
  name: string,
  declared: Set<string>,
  strict: boolean,
  warn: (message: string) => void,
): void {
  const token = String(name || "").trim();
  if (!token) {
    const msg = `[quaid][contract] invalid ${surface} registration: empty name`;
    if (strict) {
      throw new Error(msg);
    }
    warn(msg);
    return;
  }
  if (declared.has(token)) return;
  const msg = `[quaid][contract] undeclared ${surface} registration: ${token}`;
  if (strict) {
    throw new Error(msg);
  }
  warn(msg);
}

export function validateApiSurface(
  declaredApi: Set<string>,
  strict: boolean,
  warn: (message: string) => void,
  requiredApi: string = "openclaw_adapter_entry",
): void {
  if (declaredApi.has(requiredApi)) return;
  const msg = `[quaid][contract] api declaration missing required export: ${requiredApi}`;
  if (strict) {
    throw new Error(msg);
  }
  warn(msg);
}

export function validateApiRegistrations(
  declaredApi: Set<string>,
  registeredApi: Set<string>,
  strict: boolean,
  warn: (message: string) => void,
): void {
  for (const declared of declaredApi) {
    if (registeredApi.has(declared)) continue;
    const msg = `[quaid][contract] api declared but not registered at runtime: ${declared}`;
    if (strict) {
      throw new Error(msg);
    }
    warn(msg);
  }
}
