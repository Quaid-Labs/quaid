"""Compatibility alias for canonical memory maintenance routines.

Canonical location:
  datastore/memorydb/maintenance.py
"""

import sys
from datastore.memorydb import maintenance as _impl

sys.modules[__name__] = _impl
