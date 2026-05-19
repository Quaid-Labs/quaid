"""Compatibility shim for legacy ``datastore.notedb.soul_snippets`` imports.

The implementation lives in ``datastore.evolutiondb.soul_snippets``. This module
aliases the canonical module object so legacy monkeypatches and imports touch the
same functions and globals during the alpha compatibility window.
"""

from __future__ import annotations

import sys

from datastore.evolutiondb import soul_snippets as _canonical

sys.modules[__name__] = _canonical
