#!/usr/bin/env python3
"""Compatibility wrapper for project updater module."""

import runpy
import sys
from core.docs import project_updater as _impl

if __name__ == "__main__":
    runpy.run_module("core.docs.project_updater", run_name="__main__")
else:
    sys.modules[__name__] = _impl
