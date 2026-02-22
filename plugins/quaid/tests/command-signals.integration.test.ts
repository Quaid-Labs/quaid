import { describe, it, expect } from "vitest";
import {
  extractCommandName,
  normalizeCommandName,
  signalLabelForCommand,
} from "../adapters/openclaw/command-signals.js";

describe("openclaw command signal integration", () => {
  it("normalizes slash and plain command names", () => {
    expect(normalizeCommandName("/reset")).toBe("reset");
    expect(normalizeCommandName("new")).toBe("new");
    expect(normalizeCommandName("")).toBe("");
  });

  it("extracts command from structured command events", () => {
    expect(extractCommandName({ command: "reset" })).toBe("reset");
    expect(extractCommandName({ name: "/new" })).toBe("new");
  });

  it("extracts command from slash text when command field is missing", () => {
    expect(extractCommandName({ text: "/compact now" })).toBe("compact");
    expect(extractCommandName({ input: "/restart" })).toBe("restart");
    expect(extractCommandName({ raw: "hello" })).toBe("");
  });

  it("maps command names to extraction signals", () => {
    expect(signalLabelForCommand("compact")).toBe("CompactionSignal");
    expect(signalLabelForCommand("new")).toBe("NewSignal");
    expect(signalLabelForCommand("reset")).toBe("ResetSignal");
    expect(signalLabelForCommand("restart")).toBe("ResetSignal");
    expect(signalLabelForCommand("unknown")).toBeNull();
  });
});
