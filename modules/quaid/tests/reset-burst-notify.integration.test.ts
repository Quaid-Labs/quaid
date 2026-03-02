import { describe, expect, it } from "vitest";
import { __test } from "../adaptors/openclaw/adapter.js";

describe("reset burst notification guard", () => {
  it("suppresses duplicate extraction notifications for repeated reset signals on same session", () => {
    __test.clearExtractionNotifyHistory();
    const t0 = Date.now();
    const key = "done:session-burst:reset:1:0:0";
    const first = __test.shouldEmitExtractionNotify(key, t0);
    const second = __test.shouldEmitExtractionNotify(key, t0 + 250);
    const third = __test.shouldEmitExtractionNotify(key, t0 + 500);
    expect(first).toBe(true);
    expect(second).toBe(false);
    expect(third).toBe(false);
  });

  it("allows notifications for different sessions during a burst", () => {
    __test.clearExtractionNotifyHistory();
    const t0 = Date.now();
    const a = __test.shouldEmitExtractionNotify("done:session-a:reset:1:0:0", t0);
    const b = __test.shouldEmitExtractionNotify("done:session-b:reset:1:0:0", t0 + 10);
    const c = __test.shouldEmitExtractionNotify("done:session-c:reset:1:0:0", t0 + 20);
    expect(a).toBe(true);
    expect(b).toBe(true);
    expect(c).toBe(true);
  });
});
