#!/usr/bin/env python3
"""Compatibility wrapper for docs updater module."""

import runpy
import sys
from core.docs import updater as _impl

if __name__ == "__main__":
    runpy.run_module("core.docs.updater", run_name="__main__")
else:
    sys.modules[__name__] = _impl
