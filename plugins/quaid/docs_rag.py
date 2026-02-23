#!/usr/bin/env python3
"""Compatibility wrapper for docs RAG module."""

import runpy
import sys
from core.docs import rag as _impl

if __name__ == "__main__":
    runpy.run_module("core.docs.rag", run_name="__main__")
else:
    sys.modules[__name__] = _impl
