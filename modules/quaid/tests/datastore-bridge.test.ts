import { describe, expect, it, vi } from "vitest";
import { createDatastoreBridge } from "../core/datastore-bridge.js";

describe("datastore bridge", () => {
  it("routes commands through provided executor", async () => {
    const exec = vi.fn(async (cmd: string, args: string[] = []) => `${cmd}:${JSON.stringify(args)}`);
    const bridge = createDatastoreBridge(exec);
    const out1 = await bridge.recall(["q", "--stores", "vector_basic"]);
    const out2 = await bridge.store(["fact"]);
    const out3 = await bridge.createEdge(["a", "rel", "b"]);
    const out4 = await bridge.addEdge(["c", "knows", "d"]);
    const out5 = await bridge.stats();
    const out6 = await bridge.forget(["x"]);

    expect(exec).toHaveBeenCalledTimes(6);
    expect(exec).toHaveBeenNthCalledWith(1, "recall", ["q", "--stores", "vector_basic"]);
    expect(exec).toHaveBeenNthCalledWith(2, "store", ["fact"]);
    expect(exec).toHaveBeenNthCalledWith(3, "create-edge", ["a", "rel", "b"]);
    expect(exec).toHaveBeenNthCalledWith(4, "create-edge", ["c", "knows", "d"]);
    expect(exec).toHaveBeenNthCalledWith(5, "stats", []);
    expect(exec).toHaveBeenNthCalledWith(6, "forget", ["x"]);
    expect(out1).toBe('recall:["q","--stores","vector_basic"]');
    expect(out2).toBe('store:["fact"]');
    expect(out3).toBe('create-edge:["a","rel","b"]');
    expect(out4).toBe('create-edge:["c","knows","d"]');
    expect(out5).toBe("stats:[]");
    expect(out6).toBe('forget:["x"]');
  });
});
