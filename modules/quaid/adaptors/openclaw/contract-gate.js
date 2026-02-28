export function normalizeDeclaredExports(raw) {
  if (!Array.isArray(raw)) return /* @__PURE__ */ new Set();
  const out = /* @__PURE__ */ new Set();
  for (const item of raw) {
    const token = String(item || "").trim();
    if (!token) continue;
    out.add(token);
  }
  return out;
}
export function assertDeclaredRegistration(surface, name, declared, strict, warn) {
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
export function validateApiSurface(declaredApi, strict, warn, requiredApi = "openclaw_adapter_entry") {
  if (declaredApi.has(requiredApi)) return;
  const msg = `[quaid][contract] api declaration missing required export: ${requiredApi}`;
  if (strict) {
    throw new Error(msg);
  }
  warn(msg);
}
export function validateApiRegistrations(declaredApi, registeredApi, strict, warn) {
  for (const declared of declaredApi) {
    if (registeredApi.has(declared)) continue;
    const msg = `[quaid][contract] api declared but not registered at runtime: ${declared}`;
    if (strict) {
      throw new Error(msg);
    }
    warn(msg);
  }
}
