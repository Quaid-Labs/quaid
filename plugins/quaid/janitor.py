#!/usr/bin/env python3
"""Compatibility wrapper for lifecycle janitor module."""

import runpy
import sys
from core.lifecycle import janitor as _impl

if __name__ == "__main__":
    runpy.run_module("core.lifecycle.janitor", run_name="__main__")
else:
    sys.modules[__name__] = _impl
