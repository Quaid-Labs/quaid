#!/usr/bin/env python3
"""Compatibility wrapper for ingest docs ingestion module."""

import runpy
import sys
from ingest import docs_ingest as _impl

if __name__ == "__main__":
    runpy.run_module("ingest.docs_ingest", run_name="__main__")
else:
    sys.modules[__name__] = _impl
