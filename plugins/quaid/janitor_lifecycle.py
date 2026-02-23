"""Compatibility wrapper for lifecycle registry module."""

import sys
from core.lifecycle import janitor_lifecycle as _impl

sys.modules[__name__] = _impl
