#!/usr/bin/env python3
"""Compatibility alias for the canonical memory datastore module.

Canonical location:
  datastore/memorydb/memory_graph.py
"""

import sys
from datastore.memorydb import memory_graph as _impl

# Keep legacy import path fully compatible (including monkeypatch behavior)
# by aliasing this module object to the canonical implementation module.
sys.modules[__name__] = _impl
