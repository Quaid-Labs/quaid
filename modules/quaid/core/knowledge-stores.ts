export type KnowledgeDatastore = "vector" | "vector_basic" | "vector_technical" | "graph" | "journal" | "project" | "session_chunks";
export type DomainFilter = Record<string, boolean>;
export type SourceType = "user" | "assistant" | "both" | "tool" | "import";
export type RecallIntent = "general" | "agent_actions" | "relationship" | "technical";

export type KnowledgeDatastoreOption = {
  key: string;
  description: string;
  valueType: "string" | "number" | "boolean" | "string_array" | "enum";
  enumValues?: string[];
};

export type KnowledgeDatastoreSpec = {
  key: KnowledgeDatastore;
  description: string;
  defaultWhenExpandGraph: boolean;
  defaultWhenFlatRecall: boolean;
  routable: boolean;
  bridgeEligible: boolean;
  handlerStore: "vector" | "docs" | "graph" | "journal" | "session_chunks";
  aliases: string[];
  defaultDomain?: DomainFilter;
  usesCandidatePool: boolean;
  options: KnowledgeDatastoreOption[];
};

const STORE_REGISTRY: KnowledgeDatastoreSpec[] = [
  {
    key: "vector",
    description: "Combined vector recall across domain-tagged facts.",
    defaultWhenExpandGraph: false,
    defaultWhenFlatRecall: false,
    routable: false,
    bridgeEligible: true,
    handlerStore: "vector",
    aliases: [],
    usesCandidatePool: false,
    options: [
      {
        key: "domain",
        description: "Optional domain filter map JSON for this store (e.g. {\"all\":true} or {\"technical\":true}).",
        valueType: "string",
      },
      {
        key: "project",
        description: "Optional project/domain label filter for technical recall.",
        valueType: "string",
      },
    ],
  },
  {
    key: "vector_basic",
    description: "Personal facts, preferences, and relationship-adjacent memory facts.",
    defaultWhenExpandGraph: true,
    defaultWhenFlatRecall: true,
    routable: true,
    bridgeEligible: true,
    handlerStore: "vector",
    aliases: [],
    defaultDomain: { personal: true },
    usesCandidatePool: false,
    options: [],
  },
  {
    key: "vector_technical",
    description: "Technical and project-state facts (bugs, tests, versions, architecture changes).",
    defaultWhenExpandGraph: false,
    defaultWhenFlatRecall: false,
    routable: true,
    bridgeEligible: true,
    handlerStore: "vector",
    aliases: [],
    defaultDomain: { technical: true },
    usesCandidatePool: false,
    options: [],
  },
  {
    key: "graph",
    description: "Relationship and entity graph traversal (multi-hop links).",
    defaultWhenExpandGraph: true,
    defaultWhenFlatRecall: false,
    routable: true,
    bridgeEligible: true,
    handlerStore: "graph",
    aliases: [],
    usesCandidatePool: true,
    options: [
      {
        key: "depth",
        description: "Traversal depth for this store.",
        valueType: "number",
      },
      {
        key: "domain",
        description: "Optional domain filter map JSON for this store (e.g. {\"all\":true} or {\"technical\":true}).",
        valueType: "string",
      },
      {
        key: "project",
        description: "Optional project/domain label filter for technical recall.",
        valueType: "string",
      },
    ],
  },
  {
    key: "journal",
    description: "Distilled reflective context from journal files.",
    defaultWhenExpandGraph: true,
    defaultWhenFlatRecall: true,
    routable: true,
    bridgeEligible: false,
    handlerStore: "journal",
    aliases: [],
    usesCandidatePool: false,
    options: [],
  },
  {
    key: "project",
    description: "Project documentation recall from docs index.",
    defaultWhenExpandGraph: true,
    defaultWhenFlatRecall: true,
    routable: true,
    bridgeEligible: true,
    handlerStore: "docs",
    aliases: [],
    usesCandidatePool: false,
    options: [
      {
        key: "project",
        description: "Project name filter.",
        valueType: "string",
      },
      {
        key: "docs",
        description: "Doc path/name filters to restrict project recall.",
        valueType: "string_array",
      },
    ],
  },
  {
    key: "session_chunks",
    description: "Opt-in session transcript chunk recall for exact wording and evidence.",
    defaultWhenExpandGraph: false,
    defaultWhenFlatRecall: false,
    routable: false,
    bridgeEligible: true,
    handlerStore: "session_chunks",
    aliases: ["source_chunks"],
    usesCandidatePool: false,
    options: [
      {
        key: "max_chunk_tokens",
        description: "Maximum output tokens per returned session chunk.",
        valueType: "number",
      },
      {
        key: "max_total_chunk_tokens",
        description: "Maximum aggregate output tokens across returned session chunks.",
        valueType: "number",
      },
    ],
  },
];

export function getKnowledgeDatastoreRegistry(): KnowledgeDatastoreSpec[] {
  return STORE_REGISTRY.map((store) => ({
    ...store,
    aliases: [...store.aliases],
    defaultDomain: store.defaultDomain ? { ...store.defaultDomain } : undefined,
    options: store.options.map((opt) => ({ ...opt, enumValues: opt.enumValues ? [...opt.enumValues] : undefined })),
  }));
}

export function getKnowledgeDatastoreKeys(): KnowledgeDatastore[] {
  return STORE_REGISTRY.map((s) => s.key);
}

export function getRoutableDatastoreKeys(): KnowledgeDatastore[] {
  return STORE_REGISTRY.filter((s) => s.routable).map((s) => s.key);
}

export function getBridgeEligibleDatastoreKeys(): KnowledgeDatastore[] {
  return STORE_REGISTRY.filter((s) => s.bridgeEligible).map((s) => s.key);
}

export function getKnowledgeDatastoreSpec(key: KnowledgeDatastore): KnowledgeDatastoreSpec | undefined {
  const spec = STORE_REGISTRY.find((s) => s.key === key);
  if (!spec) return undefined;
  return {
    ...spec,
    aliases: [...spec.aliases],
    defaultDomain: spec.defaultDomain ? { ...spec.defaultDomain } : undefined,
    options: spec.options.map((opt) => ({ ...opt, enumValues: opt.enumValues ? [...opt.enumValues] : undefined })),
  };
}

export function normalizeKnowledgeDatastoreKey(value: unknown): KnowledgeDatastore | undefined {
  const rawValue = String(value || "").trim().toLowerCase();
  if (!rawValue) return undefined;
  for (const store of STORE_REGISTRY) {
    if (store.key === rawValue || store.aliases.includes(rawValue)) return store.key;
  }
  return undefined;
}

export function getHandlerStoreForKnowledgeDatastore(store: KnowledgeDatastore | string): string {
  const normalized = normalizeKnowledgeDatastoreKey(store);
  return STORE_REGISTRY.find((s) => s.key === normalized)?.handlerStore || String(store || "");
}

export function getDefaultDomainForKnowledgeDatastore(store: KnowledgeDatastore | string): DomainFilter | undefined {
  const normalized = normalizeKnowledgeDatastoreKey(store);
  const domain = STORE_REGISTRY.find((s) => s.key === normalized)?.defaultDomain;
  return domain ? { ...domain } : undefined;
}

export function datastoreUsesCandidatePool(store: KnowledgeDatastore | string): boolean {
  const normalized = normalizeKnowledgeDatastoreKey(store);
  return STORE_REGISTRY.find((s) => s.key === normalized)?.usesCandidatePool === true;
}

export function isVectorKnowledgeDatastore(store: KnowledgeDatastore | string): boolean {
  return getHandlerStoreForKnowledgeDatastore(store) === "vector";
}

export function renderRoutableKnowledgeDatastoreRouterGuidance(): string {
  const routable = new Set(getRoutableDatastoreKeys());
  const lines: string[] = ["Stores:"];
  for (const store of STORE_REGISTRY) {
    if (!routable.has(store.key)) continue;
    lines.push(`- ${store.key}: ${store.description}`);
  }
  lines.push(
    "Cost/latency priority:",
    "1) vector_basic first (cheap; use liberally)",
    "2) vector_technical/graph",
    "3) project/journal when needed for precision",
    "4) broader historical/session retrieval only when prior stores are insufficient",
  );
  return lines.join("\n");
}

export function normalizeKnowledgeDatastores(datastores: unknown, expandGraph: boolean): KnowledgeDatastore[] {
  const allowed = new Set(getKnowledgeDatastoreKeys());
  const defaults = STORE_REGISTRY
    .filter((s) => (expandGraph ? s.defaultWhenExpandGraph : s.defaultWhenFlatRecall))
    .map((s) => s.key);

  if (!Array.isArray(datastores) || datastores.length === 0) return defaults;

  const normalized: KnowledgeDatastore[] = [];
  for (const raw of datastores) {
    const value = normalizeKnowledgeDatastoreKey(raw);
    if (!value || !allowed.has(value) || normalized.includes(value)) continue;
    normalized.push(value);
  }

  return normalized.length ? normalized : defaults;
}

export function renderKnowledgeDatastoreGuidanceForAgents(): string {
  const lines: string[] = ["Knowledge datastores:"];
  for (const store of STORE_REGISTRY) {
    const optionSummary = store.options.length
      ? ` Options: ${store.options.map((o) => {
        if (o.valueType === "enum" && o.enumValues?.length) {
          return `${o.key} (${o.enumValues.join("|")})`;
        }
        return `${o.key} (${o.valueType})`;
      }).join(", ")}.`
      : "";
    lines.push(`- ${store.key}: ${store.description}${optionSummary}`);
  }
  return lines.join("\n");
}
