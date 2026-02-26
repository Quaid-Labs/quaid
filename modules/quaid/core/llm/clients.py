#!/usr/bin/env python3
"""Compatibility module alias to canonical ``lib.llm_clients`` implementation."""

import sys

from lib import llm_clients as _impl

sys.modules[__name__] = _impl
