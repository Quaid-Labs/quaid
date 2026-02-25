#!/usr/bin/env python3
"""Core project updater facade.

Canonical implementation lives in ``datastore.docsdb.project_updater``.
This module exists only as a compatibility import/entrypoint shim.
"""

import runpy

from datastore.docsdb.project_updater import *  # noqa: F401,F403


if __name__ == "__main__":
    runpy.run_module("datastore.docsdb.project_updater", run_name="__main__")
