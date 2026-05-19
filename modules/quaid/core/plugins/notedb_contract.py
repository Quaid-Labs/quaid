"""Compatibility shim for legacy ``core.plugins.notedb_contract`` imports.

The EvolutionDB contract implementation lives in
``core.plugins.evolutiondb_contract``. This module aliases the canonical module
object so legacy imports and monkeypatches touch the same functions and globals
during the M10 compatibility window.
"""

from __future__ import annotations

import sys

from core.plugins import evolutiondb_contract as _canonical

sys.modules[__name__] = _canonical
