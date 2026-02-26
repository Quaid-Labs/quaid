const STORE_REGISTRY = [
  {
    key: "vector",
    description: "Combined vector recall (basic + technical) with optional technical-scope override.",
    defaultWhenExpandGraph: false,
    defaultWhenFlatRecall: false,
    options: [
      {
        key: "technicalScope",
        description: "Override scope for this store.",
        valueType: "enum",
        enumValues: ["personal", "technical", "any"]
      },
      {
        key: "project",
        description: "Optional project/domain label filter for technical recall.",
        valueType: "string"
      }
    ]
  },
  {
    key: "vector_basic",
    description: "Personal facts, preferences, and relationship-adjacent memory facts.",
    defaultWhenExpandGraph: true,
    defaultWhenFlatRecall: true,
    options: []
  },
  {
    key: "vector_technical",
    description: "Technical and project-state facts (bugs, tests, versions, architecture changes).",
    defaultWhenExpandGraph: false,
    defaultWhenFlatRecall: false,
    options: []
  },
  {
    key: "graph",
    description: "Relationship and entity graph traversal (multi-hop links).",
    defaultWhenExpandGraph: true,
    defaultWhenFlatRecall: false,
    options: [
      {
        key: "depth",
        description: "Traversal depth for this store.",
        valueType: "number"
      },
      {
        key: "technicalScope",
        description: "Filter graph-backed recalls by personal/technical/all facts.",
        valueType: "enum",
        enumValues: ["personal", "technical", "any"]
      },
      {
        key: "project",
        description: "Optional project/domain label filter for technical recall.",
        valueType: "string"
      }
    ]
  },
  {
    key: "journal",
    description: "Distilled reflective context from journal files.",
    defaultWhenExpandGraph: true,
    defaultWhenFlatRecall: true,
    options: []
  },
  {
    key: "project",
    description: "Project documentation recall from docs index.",
    defaultWhenExpandGraph: true,
    defaultWhenFlatRecall: true,
    options: [
      {
        key: "project",
        description: "Project name filter.",
        valueType: "string"
      },
      {
        key: "docs",
        description: "Doc path/name filters to restrict project recall.",
        valueType: "string_array"
      }
    ]
  }
];
function getKnowledgeDatastoreRegistry() {
  return STORE_REGISTRY.map((store) => ({
    ...store,
    options: store.options.map((opt) => ({ ...opt, enumValues: opt.enumValues ? [...opt.enumValues] : void 0 }))
  }));
}
function getKnowledgeDatastoreKeys() {
  return STORE_REGISTRY.map((s) => s.key);
}
function getRoutableDatastoreKeys() {
  return STORE_REGISTRY.map((s) => s.key).filter((k) => k !== "vector");
}
function normalizeKnowledgeDatastores(datastores, expandGraph) {
  const allowed = new Set(getKnowledgeDatastoreKeys());
  const defaults = STORE_REGISTRY.filter((s) => expandGraph ? s.defaultWhenExpandGraph : s.defaultWhenFlatRecall).map((s) => s.key);
  if (!Array.isArray(datastores) || datastores.length === 0) return defaults;
  const normalized = [];
  for (const raw of datastores) {
    const value = String(raw || "").trim().toLowerCase();
    if (!allowed.has(value) || normalized.includes(value)) continue;
    normalized.push(value);
  }
  return normalized.length ? normalized : defaults;
}
function renderKnowledgeDatastoreGuidanceForAgents() {
  const lines = ["Knowledge datastores:"];
  for (const store of STORE_REGISTRY) {
    const optionSummary = store.options.length ? ` Options: ${store.options.map((o) => {
      if (o.valueType === "enum" && o.enumValues?.length) {
        return `${o.key} (${o.enumValues.join("|")})`;
      }
      return `${o.key} (${o.valueType})`;
    }).join(", ")}.` : "";
    lines.push(`- ${store.key}: ${store.description}${optionSummary}`);
  }
  return lines.join("\n");
}
export {
  getKnowledgeDatastoreKeys,
  getKnowledgeDatastoreRegistry,
  getRoutableDatastoreKeys,
  normalizeKnowledgeDatastores,
  renderKnowledgeDatastoreGuidanceForAgents
};
