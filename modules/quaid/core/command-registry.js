const COMMAND_REGISTRY = [
  {
    id: "misc_project",
    description:
      "Throwaway, temp, quick, or hello-world files and scripts — anything the user explicitly wants to put somewhere temporary",
    hint: "Throwaway file — write to: {misc_path}",
  },
  {
    id: "create_project",
    description:
      "Durable work that should be tracked: essays, articles, reports, research notes, blog posts, video scripts, screenplays, outlines, travel plans, trip itineraries, project plans, or any multi-file long-lived work",
    hint: "Durable work — create a project first: quaid registry create-project <name>",
  },
  {
    id: "recall",
    description:
      "Searching or recalling memories, facts, preferences, past conversations, or anything the user wants to look up from stored knowledge",
    hint: 'Search memories: quaid recall "your query"',
  },
  {
    id: "store",
    description:
      "Explicitly storing or saving a new fact, preference, decision, or memory for future recall",
    hint: 'Store memory: quaid store "the fact"',
  },
  {
    id: "delete_project",
    description:
      "Deleting, removing, or cleaning up a tracked project by name",
    hint: "Delete project: quaid registry delete <project-name>",
  },
];
export { COMMAND_REGISTRY };
