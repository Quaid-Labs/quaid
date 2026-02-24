import { describe, expect, it, vi } from "vitest";
import { createDatastoreBridge } from "../core/datastore-bridge.js";

describe("datastore bridge", () => {
  it("routes commands through provided executor", async () => {
    const exec = vi.fn(async (_cmd: string, _args: string[] = []) => "ok");
    const bridge = createDatastoreBridge(exec);
    await bridge.search(["q"]);
    await bridge.searchGraphAware(["q", "--json"]);
    await bridge.store(["fact"]);
    await bridge.createEdge(["a", "rel", "b"]);
    await bridge.stats();
    await bridge.forget(["x"]);

    expect(exec).toHaveBeenCalledTimes(6);
    expect(exec).toHaveBeenNthCalledWith(1, "search", ["q"]);
    expect(exec).toHaveBeenNthCalledWith(2, "search-graph-aware", ["q", "--json"]);
    expect(exec).toHaveBeenNthCalledWith(3, "store", ["fact"]);
    expect(exec).toHaveBeenNthCalledWith(4, "create-edge", ["a", "rel", "b"]);
    expect(exec).toHaveBeenNthCalledWith(5, "stats", []);
    expect(exec).toHaveBeenNthCalledWith(6, "forget", ["x"]);
  });
});

