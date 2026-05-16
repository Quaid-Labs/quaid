export type PythonBridgeExec = (command: string, args?: string[]) => Promise<string>;

export function createDatastoreBridge(exec: PythonBridgeExec) {
  return {
    recall: (args: string[]) => exec("recall", args),
    recallDocsRequest: (args: string[]) => exec("recall-docs-request", args),
    recallMemoryRequest: (args: string[]) => exec("recall-memory-request", args),
    store: (args: string[]) => exec("store", args),
    createEdge: (args: string[]) => exec("create-edge", args),
    addEdge: (args: string[]) => exec("create-edge", args),
    stats: () => exec("stats", []),
    forget: (args: string[]) => exec("forget", args),
  };
}
