import { describe, expect, it, vi } from "vitest";
import { createDataWriteEngine } from "../core/data-writers.js";

describe("data writers", () => {
  it("writes through registered DataWriter", async () => {
    const write = vi.fn(async () => ({ status: "created" as const, id: "abc-123" }));
    const engine = createDataWriteEngine({
      writers: [{
        spec: {
          datastore: "vector",
          description: "Vector facts",
          actions: [{ key: "store_fact", description: "Store a fact" }],
        },
        write,
      }],
    });

    const out = await engine.writeData({
      datastore: "vector",
      action: "store_fact",
      payload: { text: "Quaid prefers tea" },
    });

    expect(write).toHaveBeenCalledTimes(1);
    expect(out.status).toBe("created");
    expect(out.id).toBe("abc-123");
  });

  it("fails when no writer is registered", async () => {
    const engine = createDataWriteEngine();
    const out = await engine.writeData({
      datastore: "graph",
      action: "create_edge",
      payload: { subject: "A", relation: "knows", object: "B" },
    });
    expect(out.status).toBe("failed");
    expect(out.error).toContain("No DataWriter registered");
  });

  it("fails unsupported action for registered writer", async () => {
    const engine = createDataWriteEngine({
      writers: [{
        spec: {
          datastore: "graph",
          description: "Graph edges",
          actions: [{ key: "create_edge", description: "Create a graph edge" }],
        },
        write: vi.fn(async () => ({ status: "created" as const })),
      }],
    });

    const out = await engine.writeData({
      datastore: "graph",
      action: "delete_edge",
      payload: { id: "edge-1" },
    });

    expect(out.status).toBe("failed");
    expect(out.error).toContain("is not supported");
  });

  it("returns cloned writer registry specs", () => {
    const engine = createDataWriteEngine({
      writers: [{
        spec: {
          datastore: "journal",
          description: "Journal updates",
          actions: [{ key: "append_entry", description: "Append journal text" }],
        },
        write: vi.fn(async () => ({ status: "created" as const })),
      }],
    });

    const registry = engine.getDataWriterRegistry();
    expect(registry.length).toBe(1);
    registry[0].actions[0].key = "mutated";

    const registry2 = engine.getDataWriterRegistry();
    expect(registry2[0].actions[0].key).toBe("append_entry");
  });
});
