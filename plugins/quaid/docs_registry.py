#!/usr/bin/env python3
"""Compatibility wrapper for docs registry module."""

import runpy
import sys
from core.docs import registry as _impl

if __name__ == "__main__":
    runpy.run_module("core.docs.registry", run_name="__main__")
else:
    sys.modules[__name__] = _impl
