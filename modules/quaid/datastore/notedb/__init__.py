"""Compatibility package for the legacy NoteDB runtime name.

EvolutionDB is the canonical runtime package. Keep this shim for installed
alpha compatibility until the M10 removal condition is met.
"""

from datastore.evolutiondb import soul_snippets as soul_snippets

__all__ = ["soul_snippets"]
