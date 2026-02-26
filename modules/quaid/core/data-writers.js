function cloneWriterSpec(spec) {
  return {
    datastore: spec.datastore,
    description: spec.description,
    actions: spec.actions.map((action) => ({ ...action }))
  };
}
function createDataWriteEngine(opts = {}) {
  const writerMap = /* @__PURE__ */ new Map();
  function registerDataWriter(writer) {
    writerMap.set(writer.spec.datastore, writer);
  }
  for (const writer of opts.writers || []) {
    registerDataWriter(writer);
  }
  async function writeData(envelope) {
    const writer = writerMap.get(envelope.datastore);
    if (!writer) {
      return {
        status: "failed",
        error: `No DataWriter registered for datastore "${envelope.datastore}"`
      };
    }
    const allowedActions = new Set(writer.spec.actions.map((a) => a.key));
    if (!allowedActions.has(envelope.action)) {
      return {
        status: "failed",
        error: `Action "${envelope.action}" is not supported for datastore "${envelope.datastore}"`
      };
    }
    try {
      return await writer.write(envelope);
    } catch (err) {
      const errObj = err;
      const errType = errObj?.name || typeof err;
      return {
        status: "failed",
        error: String(errObj?.message || err || "Unknown DataWriter error"),
        details: {
          error_type: errType
        }
      };
    }
  }
  async function writeDataBatch(envelopes) {
    const results = [];
    for (const envelope of envelopes || []) {
      results.push(await writeData(envelope));
    }
    return results;
  }
  function getDataWriterRegistry() {
    return Array.from(writerMap.values()).map((w) => cloneWriterSpec(w.spec));
  }
  function renderDataWriterGuidanceForAgents() {
    const lines = ["Knowledge write paths (DataWriters):"];
    for (const spec of getDataWriterRegistry()) {
      const actions = spec.actions.map((action) => action.key).join(", ");
      lines.push(`- ${spec.datastore}: ${spec.description} Actions: ${actions}.`);
    }
    return lines.join("\n");
  }
  return {
    registerDataWriter,
    writeData,
    writeDataBatch,
    getDataWriterRegistry,
    renderDataWriterGuidanceForAgents
  };
}
export {
  createDataWriteEngine
};
