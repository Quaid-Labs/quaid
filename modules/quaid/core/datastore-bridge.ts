export type PythonBridgeExec = (command: string, args?: string[]) => Promise<string>;

export function createDatastoreBridge(exec: PythonBridgeExec) {
  return {
    recall: (args: string[]) => exec("recall", args),
    store: (args: string[]) => exec("store", args),
    createEdge: (args: string[]) => exec("create-edge", args),
    stats: () => exec("stats", []),
    forget: (args: string[]) => exec("forget", args),
  };
}

