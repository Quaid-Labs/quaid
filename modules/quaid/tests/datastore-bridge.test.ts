import { describe, expect, it, vi } from "vitest";
import { createDatastoreBridge } from "../core/datastore-bridge.js";

describe("datastore bridge", () => {
  it("routes commands through provided executor", async () => {
    const exec = vi.fn(async (cmd: string, args: string[] = []) => `${cmd}:${JSON.stringify(args)}`);
    const bridge = createDatastoreBridge(exec);
    const out1 = await bridge.search(["q"]);
    const out2 = await bridge.searchGraphAware(["q", "--json"]);
    const out3 = await bridge.store(["fact"]);
    const out4 = await bridge.createEdge(["a", "rel", "b"]);
    const out5 = await bridge.stats();
    const out6 = await bridge.forget(["x"]);

    expect(exec).toHaveBeenCalledTimes(6);
    expect(exec).toHaveBeenNthCalledWith(1, "search", ["q"]);
    expect(exec).toHaveBeenNthCalledWith(2, "search-graph-aware", ["q", "--json"]);
    expect(exec).toHaveBeenNthCalledWith(3, "store", ["fact"]);
    expect(exec).toHaveBeenNthCalledWith(4, "create-edge", ["a", "rel", "b"]);
    expect(exec).toHaveBeenNthCalledWith(5, "stats", []);
    expect(exec).toHaveBeenNthCalledWith(6, "forget", ["x"]);
    expect(out1).toBe('search:["q"]');
    expect(out2).toBe('search-graph-aware:["q","--json"]');
    expect(out3).toBe('store:["fact"]');
    expect(out4).toBe('create-edge:["a","rel","b"]');
    expect(out5).toBe("stats:[]");
    expect(out6).toBe('forget:["x"]');
  });
});
