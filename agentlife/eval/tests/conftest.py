"""Conftest: mock heavy dependencies so harness modules can be imported in tests."""

import sys
import types
from pathlib import Path
from unittest.mock import MagicMock

import pytest

ROOT = Path(__file__).resolve().parents[1]

# ---------------------------------------------------------------------------
# Mock the 'dataset' module that run_production_benchmark.py imports.
# The real dataset.py lives in the benchmark repo, not in dev/.
# We provide stubs for the symbols the harness expects.
# ---------------------------------------------------------------------------

_dataset_mod = types.ModuleType("dataset")
_dataset_mod.load_all_reviews = MagicMock(return_value=[])
_dataset_mod.get_all_eval_queries = MagicMock(return_value=[])
_dataset_mod.format_transcript_for_extraction = MagicMock(return_value="")
_dataset_mod.SESSION_DATES = {
    1: "2026-03-01", 2: "2026-03-03", 3: "2026-03-05",
    4: "2026-03-10", 5: "2026-03-12", 6: "2026-03-14",
    7: "2026-03-15", 8: "2026-03-17", 9: "2026-03-15",
    10: "2026-03-17", 11: "2026-03-22", 12: "2026-03-24",
    13: "2026-04-01", 14: "2026-04-03", 15: "2026-04-08",
    16: "2026-04-10", 17: "2026-04-15", 18: "2026-04-17",
    19: "2026-04-25", 20: "2026-05-01",
}
_dataset_mod.SESSION_TRACKS = {i: 1 for i in range(1, 21)}
_dataset_mod.get_tier5_queries = MagicMock(return_value=[])

sys.modules.setdefault("dataset", _dataset_mod)

# ---------------------------------------------------------------------------
# Mock the 'metrics' module (lives in benchmark repo, not dev/).
# ---------------------------------------------------------------------------

_metrics_mod = types.ModuleType("metrics")
_metrics_mod.score_results = MagicMock(return_value={})
_metrics_mod.retrieval_metrics = MagicMock(return_value={})
_metrics_mod.format_report = MagicMock(return_value="")
sys.modules.setdefault("metrics", _metrics_mod)

# Ensure the eval/ directory is on sys.path for `import run_production_benchmark`
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
