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
- `runtime.python.module`: Python import path for runtime adapter class
- `runtime.python.class`: class name to instantiate for this adapter
- `runtime.python.path` (optional): list of extra import roots (relative to manifest dir or absolute)
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
    "instancePrefix": "agentfoo-",
    "python": {
      "module": "agentfoo_quaid_adapter.runtime",
      "class": "AgentFooAdapter",
      "path": ["./runtime"]
    }
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

### Platform-shared config ownership

Adapter/platform-scoped defaults live under:

`$QUAID_HOME/shared/config/<platform>/memory.json`

For model/provider defaults specifically, installer/runtime treat this as
platform-owned override state. Do not use global shared config for model lanes.

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
- `installer_supported_providers()` to constrain provider choices in guided install
- `installer_default_models(provider)` to provide deep/fast lane defaults per provider
- `get_fast_provider_default()` / `get_deep_provider_default()` for adapter-owned default provider lanes
- `get_fast_model_default(provider)` / `get_deep_model_default(provider)` for adapter-owned model defaults

Model precedence in install flow:
1. Platform-shared config (`shared/config/<platform>/memory.json`)
2. Adapter defaults (`get_*_provider_default`, `get_*_model_default`)
3. Global hardcoded fallback (only if adapter does not provide values)

### Optional instance manager

If your adapter needs project-specific silos, subclass:

- `modules/quaid/lib/instance_manager.py` → `InstanceManager`

and return it from `get_instance_manager()`.

---

## 3) Runtime registration path (current state)

Runtime adapter selection now follows the same manifest registry contract:

1. `config/memory.json` → `adapter.type`
2. Resolve `~/.quaid/adaptors/<adapter-id>/adapter.json`
3. Load `runtime.python.module` + `runtime.python.class` from that manifest

Built-in adapters use this exact path too. Third-party adapters can ship outside
the Quaid repo and point `runtime.python.path` at their own package root.

---

## 4) Minimal bring-up checklist

1. Add installer manifest to `~/.quaid/adaptors/<id>/adapter.json` (or ship via built-in manifests).
2. Add optional pre/post install hooks if host setup is needed.
3. Implement Python adapter class compatible with `QuaidAdapter` methods.
4. Set `runtime.python.module` + `runtime.python.class` in manifest.
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
