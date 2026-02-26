import { describe, it, expect, vi } from "vitest";
import { createProjectCatalogReader } from "../core/project-catalog.js";

describe("project catalog reader diagnostics", () => {
  it("logs warning when config cannot be parsed", () => {
    const warnSpy = vi.spyOn(console, "warn").mockImplementation(() => {});
    const reader = createProjectCatalogReader({
      workspace: "/tmp/quaid-test",
      fs: {
        readFileSync: () => {
          throw new Error("boom");
        },
      } as any,
      path: {
        join: (...parts: string[]) => parts.join("/"),
      } as any,
    });

    expect(reader.getProjectNames()).toEqual([]);
    expect(warnSpy).toHaveBeenCalled();
    warnSpy.mockRestore();
  });

  it("throws when config cannot be parsed and failHard is enabled", () => {
    const warnSpy = vi.spyOn(console, "warn").mockImplementation(() => {});
    const reader = createProjectCatalogReader({
      workspace: "/tmp/quaid-test",
      fs: {
        readFileSync: () => {
          throw new Error("boom");
        },
      } as any,
      path: {
        join: (...parts: string[]) => parts.join("/"),
      } as any,
      isFailHardEnabled: () => true,
    });

    expect(() => reader.getProjectNames()).toThrow(/failed to load project names/i);
    expect(warnSpy).not.toHaveBeenCalled();
    warnSpy.mockRestore();
  });
});
