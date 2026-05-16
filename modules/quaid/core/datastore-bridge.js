function createDatastoreBridge(exec) {
  return {
    recall: (args) => exec("recall", args),
    recallDocsRequest: (args) => exec("recall-docs-request", args),
    recallMemoryRequest: (args) => exec("recall-memory-request", args),
    store: (args) => exec("store", args),
    createEdge: (args) => exec("create-edge", args),
    addEdge: (args) => exec("create-edge", args),
    stats: () => exec("stats", []),
    forget: (args) => exec("forget", args)
  };
}
export {
  createDatastoreBridge
};
