#!/usr/bin/env python3
"""Compatibility wrapper for ingest extraction module."""

import runpy
import sys
from ingest import extract as _impl

if __name__ == "__main__":
    runpy.run_module("ingest.extract", run_name="__main__")
else:
    sys.modules[__name__] = _impl
