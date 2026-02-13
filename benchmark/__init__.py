"""
Stress test runner package — path bootstrap for Quaid imports.

All runners import this first so they can use `from memory_graph import store, recall`
etc. without installing the plugin as a package.
"""
import os
import sys
from pathlib import Path

# Resolve the quaid plugin directory from env or default
QUAID_DIR = Path(os.environ.get("QUAID_WORKSPACE", Path.home() / "quaid")) / "plugins" / "quaid"

if str(QUAID_DIR) not in sys.path:
    sys.path.insert(0, str(QUAID_DIR))
