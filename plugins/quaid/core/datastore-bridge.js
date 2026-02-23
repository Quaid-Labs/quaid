function createDatastoreBridge(exec) {
  return {
    search: (args) => exec("search", args),
    searchGraphAware: (args) => exec("search-graph-aware", args),
    store: (args) => exec("store", args),
    createEdge: (args) => exec("create-edge", args),
    stats: () => exec("stats", []),
    forget: (args) => exec("forget", args)
  };
}
export {
  createDatastoreBridge
};
