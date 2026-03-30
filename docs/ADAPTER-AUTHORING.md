# Adapter Authoring Guide

This page explains how to add Quaid support for a new host platform (for example, `agentfoo`).

Quaid adapter integration has two layers:

1. **Installer integration** (manifest registry under `~/.quaid/adaptors/`)
2. **Runtime integration** (Python `QuaidAdapter` implementation + factory wiring)

You need both for a complete platform integration.

---

## 1) Installer Registry (`~/.quaid/adaptors`)

Quaid installer discovers adapter choices from:

`~/.quaid/adaptors/<adapter-id>/adapter.json`

Built-in manifests are seeded by installer first, then the same directory is used for third-party adapters.

### Manifest schema

- `schema`: must be `quaid-adapter-install/v1`
- `id`: stable adapter id (lowercase, `[a-z0-9._-]`)
- `name`: human-friendly name
- `install.selectLabel`: label shown in installer platform picker
- `install.selectHint`: short hint shown in installer platform picker
- `install.sortOrder` (optional): lower appears earlier
- `scripts.preinstall` (optional): runs during installer preflight
- `scripts.postinstall` (optional): runs near install completion

### Example `adapter.json`

```json
{
  "schema": "quaid-adapter-install/v1",
  "id": "agentfoo",
  "name": "AgentFoo",
  "install": {
    "selectLabel": "AgentFoo",
    "selectHint": "AgentFoo runtime integration",
    "sortOrder": 40
  },
  "runtime": {
    "instancePrefix": "agentfoo-"
  },
  "scripts": {
    "preinstall": "./hooks/preinstall.sh",
    "postinstall": "./hooks/postinstall.sh"
  }
}
```

### Hook script behavior

- Paths are resolved relative to the manifest folder.
- Current installer supports `.sh` (direct exec), `.mjs/.js/.cjs` (via `node`), `.py` (via `python3`).
- Hook environment includes:
  - `QUAID_HOME`
  - `QUAID_WORKSPACE`
  - `QUAID_ADAPTER_ID`
  - `QUAID_ADAPTER_MANIFEST_PATH`
  - `QUAID_ADAPTER_REGISTRY_DIR`
  - `QUAID_ADAPTER_HOOK` (`preinstall` or `postinstall`)

---

## 2) Runtime Adapter Contract (Python)

Implement a class inheriting:

- `modules/quaid/lib/adapter.py` → `QuaidAdapter`

See existing implementations:

- `modules/quaid/adaptors/openclaw/adapter.py`
- `modules/quaid/adaptors/claude_code/adapter.py`

### Required `QuaidAdapter` methods

`QuaidAdapter` is abstract. Implement at minimum:

- `quaid_home()`
- `get_instance_name()`
- `notify(message, channel_override=None, dry_run=False, force=False)`
- `get_last_channel(session_key="")`
- `get_api_key(env_var_name)`
- `get_sessions_dir()`
- `filter_system_messages(text)`
- `get_llm_provider(model_tier=None)`

### Common overrides (recommended)

- `adapter_id()` (stable id string used by config/runtime)
- `agent_id_prefix()` (instance prefix convention)
- `get_host_info()` (compatibility checks)
- `get_cli_namespace()` / `get_cli_commands()` for adapter-specific CLI
- `get_instance_manager()` if your host supports user-created named silos

### Optional instance manager

If your adapter needs project-specific silos, subclass:

- `modules/quaid/lib/instance_manager.py` → `InstanceManager`

and return it from `get_instance_manager()`.

---

## 3) Runtime registration today (current state)

Installer manifest registration alone does **not** activate runtime loading yet.

Current runtime adapter selection path is:

1. `config/memory.json` → `adapter.type`
2. `modules/quaid/adaptors/factory.py` → `create_adapter(kind)`

So today, for full runtime support you must also:

1. Add your adapter module under `modules/quaid/adaptors/<adapter_id>/`.
2. Register it in `modules/quaid/adaptors/factory.py`.
3. Ensure `adapter.type` resolves to your adapter id.

---

## 4) Minimal bring-up checklist

1. Add installer manifest to `~/.quaid/adaptors/<id>/adapter.json` (or ship via built-in manifests).
2. Add optional pre/post install hooks if host setup is needed.
3. Implement Python adapter class inheriting `QuaidAdapter`.
4. Wire adapter in `adaptors/factory.py`.
5. Validate:
   - `quaid compat status`
   - `quaid doctor`
   - lifecycle extraction + recall on your host
   - janitor run + delayed-notification queue behavior

---

## 5) Versioning and compatibility policy for manifests

- Major schema changes require a new schema id (`.../v2`, etc.).
- Minor additive fields should remain backward-compatible under `v1`.
- Unknown optional fields should be ignored by installer.
- Keep `id` stable forever once published.
