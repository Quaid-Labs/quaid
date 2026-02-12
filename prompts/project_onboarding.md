# Project Onboarding — Agent Instructions

When the user asks to set up projects, when you receive a MOVE_TO_PROJECT notification,
or when the projects system has no registered projects, follow this workflow to discover
and register them.

## When to Trigger

- User says "set up projects", "find my projects", "onboard projects"
- You receive a **[Quaid] Content Moved to Project** notification (automatic after janitor)
- The `quaid stats` output shows 0 registered projects
- First conversation after a fresh Quaid install

## Post-Janitor Project Review

When the janitor's workspace audit moves content to a project (you'll get a notification),
walk the user through the decision:

1. **Summarize what happened**: "The janitor found project specs in your TOOLS.md and
   moved them to `projects/<name>/`. Here's what it did..."
2. **Ask about naming**: "It named the project `<name>` — does that work, or would you
   prefer a different name?"
3. **Ask about organization**: "Should any other files be moved into this project? Are
   there related configs, scripts, or docs that belong together?"
4. **Ask about external files**: "Are there files outside the workspace that this project
   should track? For example, a separate git repo, a shared config, or API docs?"
5. **Suggest next steps**: "Want me to create a PROJECT.md with an overview? I can also
   set up TOOLS.md and AGENTS.md for project-specific instructions."

If the user disagrees with the janitor's decision, help them move content back or
reassign it to the correct project.

## Discovery Steps

### 1. Check Memory for Known Projects

Search the memory graph for any previously mentioned projects:

```bash
cd plugins/quaid && python3 memory_graph.py search "project" --owner <OWNER> --limit 10
```

If results mention specific project names or paths, note them.

### 2. Scan the Workspace Filesystem

Look for project-like directories. Common indicators:
- Has a `README.md`, `PROJECT.md`, or similar documentation
- Has source code files (`.py`, `.ts`, `.js`, `.rs`, `.go`, etc.)
- Has a `package.json`, `pyproject.toml`, `Cargo.toml`, `go.mod`, etc.
- Has a `.git` directory (standalone repo) or is a subfolder with clear boundaries
- Has a `docs/` or `doc/` directory

Scan the workspace:

```bash
# Look for project indicators in the workspace root
ls -la $CLAWDBOT_WORKSPACE/

# Check for existing projects/ directory
ls $CLAWDBOT_WORKSPACE/projects/ 2>/dev/null

# Find directories with README or project files
find $CLAWDBOT_WORKSPACE -maxdepth 3 -name "README.md" -o -name "PROJECT.md" -o -name "package.json" -o -name "pyproject.toml" 2>/dev/null
```

### 3. Present Findings to the User

Format your discoveries as a friendly summary:

> "Hey! From scanning your workspace and memories, I found these potential projects:
>
> 1. **project-name** — `/path/to/project` (has README.md, 12 Python files)
> 2. **another-project** — `/path/to/other` (has package.json, looks like a Node.js app)
> 3. **docs-collection** — `/path/to/docs` (markdown files about X topic)
>
> Want me to register any of these? I can also help organize them."

### 4. Ask Clarifying Questions

For each project the user wants to register:

- **Name**: Short identifier (lowercase, hyphens). Suggest based on directory name.
- **Label**: Human-readable name. e.g., "Personal Website" for `personal-site/`
- **Description**: One sentence about what this project is.
- **Source roots**: Directories containing source code to monitor for changes.
- **Doc patterns**: Which files to index for RAG search (default: `*.md`)
- **Exclude patterns**: Files to skip (default: `*.db`, `*.log`, `__pycache__/`)

### 5. Register Projects

For each confirmed project:

```bash
cd plugins/quaid

# Register the project
python3 docs_registry.py create-project <name> \
  --label "<Label>" \
  --home-dir "<relative/path>" \
  --description "<description>"

# If the project has docs to index
python3 docs_registry.py register <relative/path/to/doc.md> --project <name>
```

### 6. Create PROJECT.md (Optional)

If the project doesn't have a `PROJECT.md`, offer to create one:

```markdown
# <Project Name>

<One paragraph description>

## Key Files
- `file1.py` — Description
- `file2.ts` — Description

## Architecture
<Brief overview of how the project is structured>
```

The janitor's doc auto-update system will keep this file current as the project evolves.

### 7. Confirm Setup

After registering, verify:

```bash
python3 docs_registry.py list --project <name>
```

Tell the user what was registered and that the system will now:
- Auto-track changes to registered files
- Update project docs when source code changes
- Include project docs in RAG search results

## Project Directory Convention

The recommended layout for Quaid-managed projects:

```
projects/
├── my-app/
│   ├── PROJECT.md      # Auto-maintained project overview
│   ├── TOOLS.md        # Tools/APIs this project uses (loaded by bot)
│   ├── AGENTS.md       # Agent-specific instructions (loaded by bot)
│   └── design-doc.md   # Additional tracked documentation
├── another-project/
│   └── PROJECT.md
└── staging/            # Reserved for Quaid internal use
```

Projects in `projects/` are auto-discovered by the janitor's RAG indexing task.
Projects elsewhere in the workspace need manual registration via `docs_registry.py`.
