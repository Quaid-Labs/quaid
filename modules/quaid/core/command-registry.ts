/**
 * Command registry — structured routing entries for the tool hint planner.
 *
 * Each entry describes a category of user intent and the actionable hint to
 * surface. Hint templates may contain {misc_path} and {instance}, which are
 * resolved at runtime by the facade before being passed to the planner.
 *
 * Eventually this will be built programmatically from datastore plugin
 * contracts. For now it is a static list.
 */

export type CommandEntry = {
  /** Stable identifier for this command category. */
  id: string;
  /** Natural-language description of what messages trigger this entry. */
  description: string;
  /** One-line hint template shown to the agent. Supports {misc_path}, {instance}. */
  hint: string;
};

export const COMMAND_REGISTRY: CommandEntry[] = [
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
      "Searching or recalling memories, facts, preferences, relationships, project history, codebase details, architecture, tests, schemas, or anything the user wants to look up from stored knowledge.",
    hint: 'Search memories: quaid recall "<query>"',
  },
  {
    id: "store",
    description:
      "Explicitly storing or saving a new fact, preference, decision, or memory for future recall",
    hint: 'Store memory: quaid store "the fact"',
  },
];
