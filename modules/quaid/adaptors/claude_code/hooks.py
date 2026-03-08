#!/usr/bin/env python3
"""Backwards-compatible shim — hooks moved to core.interface.hooks."""

import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from core.interface.hooks import main

if __name__ == "__main__":
    main()
