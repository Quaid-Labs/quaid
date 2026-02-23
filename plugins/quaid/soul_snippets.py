"""Compatibility wrapper for lifecycle soul snippets module."""

import runpy
import sys
from core.lifecycle import soul_snippets as _impl

if __name__ == "__main__":
    runpy.run_module("core.lifecycle.soul_snippets", run_name="__main__")
else:
    sys.modules[__name__] = _impl
