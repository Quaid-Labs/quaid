function createDatastoreBridge(exec) {
  return {
    recall: (args) => exec("recall", args),
    store: (args) => exec("store", args),
    createEdge: (args) => exec("create-edge", args),
    stats: () => exec("stats", []),
    forget: (args) => exec("forget", args),
    planToolHint: (query) => exec("plan-tool-hint", [query])
  };
}
export {
  createDatastoreBridge
};
