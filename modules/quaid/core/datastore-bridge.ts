export type PythonBridgeExec = (command: string, args?: string[]) => Promise<string>;

export function createDatastoreBridge(exec: PythonBridgeExec) {
  return {
    search: (args: string[]) => exec("search", args),
    searchGraphAware: (args: string[]) => exec("search-graph-aware", args),
    store: (args: string[]) => exec("store", args),
    createEdge: (args: string[]) => exec("create-edge", args),
    stats: () => exec("stats", []),
    forget: (args: string[]) => exec("forget", args),
  };
}

