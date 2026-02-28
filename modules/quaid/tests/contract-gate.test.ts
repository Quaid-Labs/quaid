import { describe, expect, it, vi } from "vitest";
import {
  assertDeclaredRegistration,
  normalizeDeclaredExports,
  validateApiRegistrations,
  validateApiSurface,
} from "../adaptors/openclaw/contract-gate.js";

describe("contract gate", () => {
  it("normalizes declared exports", () => {
    const set = normalizeDeclaredExports(["memory_recall", " ", null, "projects_search"]);
    expect(Array.from(set).sort()).toEqual(["memory_recall", "projects_search"]);
  });

  it("throws undeclared registration in strict mode", () => {
    const warn = vi.fn();
    expect(() =>
      assertDeclaredRegistration("tools", "memory_store", new Set(["memory_recall"]), true, warn)
    ).toThrow(/undeclared tools registration/);
    expect(warn).not.toHaveBeenCalled();
  });

  it("warns undeclared registration in non-strict mode", () => {
    const warn = vi.fn();
    assertDeclaredRegistration("events", "before_reset", new Set(["agent_end"]), false, warn);
    expect(warn).toHaveBeenCalledTimes(1);
  });

  it("accepts extended declared surfaces", () => {
    const warn = vi.fn();
    assertDeclaredRegistration(
      "notifications",
      "memory_extraction",
      new Set(["memory_extraction"]),
      true,
      warn,
    );
    expect(warn).not.toHaveBeenCalled();
  });

  it("validates required api surface", () => {
    const warn = vi.fn();
    expect(() => validateApiSurface(new Set(), true, warn)).toThrow(/missing required export/);
    validateApiSurface(new Set(), false, warn);
    expect(warn).toHaveBeenCalled();
  });

  it("detects api exports declared but not registered", () => {
    const warn = vi.fn();
    expect(() =>
      validateApiRegistrations(
        new Set(["openclaw_adapter_entry", "/plugins/quaid/llm"]),
        new Set(["openclaw_adapter_entry"]),
        true,
        warn,
      )
    ).toThrow(/declared but not registered/);
  });
});
