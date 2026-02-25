#!/usr/bin/env python3
"""Core docs updater facade.

Canonical implementation lives in ``datastore.docsdb.updater``.
This module exists only as a compatibility import/entrypoint shim.
"""

import runpy

from datastore.docsdb.updater import *  # noqa: F401,F403


if __name__ == "__main__":
    runpy.run_module("datastore.docsdb.updater", run_name="__main__")
