"""Compatibility wrapper for lifecycle workspace audit module."""

import runpy
import sys
from core.lifecycle import workspace_audit as _impl

if __name__ == "__main__":
    runpy.run_module("core.lifecycle.workspace_audit", run_name="__main__")
else:
    sys.modules[__name__] = _impl
