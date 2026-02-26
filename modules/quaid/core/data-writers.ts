export type DataStoreKey = "vector" | "graph" | "journal" | "project" | (string & {});
export type DataWriteAction = string;

export type DataWriterActionSpec = {
  key: DataWriteAction;
  description: string;
};

export type DataWriterSpec = {
  datastore: DataStoreKey;
  description: string;
  actions: DataWriterActionSpec[];
};

export type DataWriteEnvelope<TPayload = unknown> = {
  datastore: DataStoreKey;
  action: DataWriteAction;
  payload: TPayload;
  meta?: {
    source?: string;
    ownerId?: string;
    sessionId?: string;
    idempotencyKey?: string;
    timestamp?: string;
  };
};

export type DataWriteResult = {
  status: "created" | "updated" | "duplicate" | "skipped" | "failed";
  id?: string;
  details?: Record<string, unknown>;
  error?: string;
};

export type DataWriter<TPayload = unknown> = {
  spec: DataWriterSpec;
  write: (envelope: DataWriteEnvelope<TPayload>) => Promise<DataWriteResult>;
};

type DataWriteEngineOptions = {
  writers?: DataWriter[];
};

function cloneWriterSpec(spec: DataWriterSpec): DataWriterSpec {
  return {
    datastore: spec.datastore,
    description: spec.description,
    actions: spec.actions.map((action) => ({ ...action })),
  };
}

export function createDataWriteEngine(opts: DataWriteEngineOptions = {}) {
  const writerMap = new Map<DataStoreKey, DataWriter>();

  function registerDataWriter(writer: DataWriter): void {
    writerMap.set(writer.spec.datastore, writer);
  }

  for (const writer of opts.writers || []) {
    registerDataWriter(writer);
  }

  async function writeData<TPayload = unknown>(
    envelope: DataWriteEnvelope<TPayload>
  ): Promise<DataWriteResult> {
    const writer = writerMap.get(envelope.datastore);
    if (!writer) {
      return {
        status: "failed",
        error: `No DataWriter registered for datastore "${envelope.datastore}"`,
      };
    }

    const allowedActions = new Set(writer.spec.actions.map((a) => a.key));
    if (!allowedActions.has(envelope.action)) {
      return {
        status: "failed",
        error: `Action "${envelope.action}" is not supported for datastore "${envelope.datastore}"`,
      };
    }

    try {
      return await writer.write(envelope as DataWriteEnvelope<unknown>);
    } catch (err: unknown) {
      const errObj = err as Error;
      const errType = errObj?.name || typeof err || "UnknownError";
      return {
        status: "failed",
        error: String(errObj?.message || err || "Unknown DataWriter error"),
        details: {
          error_type: errType,
        },
      };
    }
  }

  async function writeDataBatch<TPayload = unknown>(
    envelopes: Array<DataWriteEnvelope<TPayload>>
  ): Promise<DataWriteResult[]> {
    const results: DataWriteResult[] = [];
    for (const envelope of envelopes || []) {
      results.push(await writeData(envelope));
    }
    return results;
  }

  function getDataWriterRegistry(): DataWriterSpec[] {
    return Array.from(writerMap.values()).map((w) => cloneWriterSpec(w.spec));
  }

  function renderDataWriterGuidanceForAgents(): string {
    const lines: string[] = ["Knowledge write paths (DataWriters):"];
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
    renderDataWriterGuidanceForAgents,
  };
}
