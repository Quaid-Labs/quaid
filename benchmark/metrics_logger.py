#!/usr/bin/env python3
"""
Time-series metrics snapshots — captures system state after each phase
so we can track how metrics evolve over the life of the stress test.
"""
import argparse
import json
import math
import os
import statistics
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional

# Self-path setup for standalone execution
_DIR = Path(__file__).resolve().parent
if str(_DIR) not in sys.path:
    sys.path.insert(0, str(_DIR))
if str(_DIR.parent) not in sys.path:
    sys.path.insert(0, str(_DIR.parent))

# Bootstrap quaid imports
import runner  # noqa: F401
from memory_graph import recall
from lib.database import get_connection
from lib.config import get_db_path
from scenarios import get_recall_queries

# Fixed 10-query probe subset for fast snapshots
_PROBE_QUERIES = get_recall_queries()[:10]


class MetricsLogger:
    """Captures periodic metric snapshots and writes them to JSONL."""

    def __init__(self, output_dir: Path):
        self.snapshots: List[Dict] = []
        self.output_dir = output_dir
        self.output_dir.mkdir(parents=True, exist_ok=True)

    def snapshot(self, label: str, week: Optional[int] = None) -> Dict:
        """Capture current system metrics and append to timeline."""
        snap = {
            "timestamp": datetime.now().isoformat(),
            "label": label,
            "week": week,
        }

        # DB stats
        try:
            with get_connection() as conn:
                snap["nodes"] = conn.execute("SELECT COUNT(*) FROM nodes").fetchone()[0]
                snap["edges"] = conn.execute("SELECT COUNT(*) FROM edges").fetchone()[0]

                # By-status counts
                status_rows = conn.execute(
                    "SELECT status, COUNT(*) FROM nodes GROUP BY status"
                ).fetchall()
                snap["by_status"] = {r[0]: r[1] for r in status_rows}

                # By-category counts
                cat_rows = conn.execute(
                    "SELECT type, COUNT(*) FROM nodes GROUP BY type"
                ).fetchall()
                snap["by_category"] = {r[0]: r[1] for r in cat_rows}

                # Confidence distribution buckets
                snap["confidence_buckets"] = {}
                for low, high, label_name in [
                    (0, 0.3, "0-0.3"), (0.3, 0.5, "0.3-0.5"), (0.5, 0.7, "0.5-0.7"),
                    (0.7, 0.9, "0.7-0.9"), (0.9, 1.01, "0.9-1.0"),
                ]:
                    count = conn.execute(
                        "SELECT COUNT(*) FROM nodes WHERE confidence >= ? AND confidence < ?",
                        (low, high),
                    ).fetchone()[0]
                    snap["confidence_buckets"][label_name] = count

                # Dedup stats
                snap["dedup_log_count"] = conn.execute(
                    "SELECT COUNT(*) FROM dedup_log"
                ).fetchone()[0]
                snap["contradiction_count"] = conn.execute(
                    "SELECT COUNT(*) FROM contradictions"
                ).fetchone()[0]

                # Embedding coverage
                total = snap["nodes"]
                with_emb = conn.execute(
                    "SELECT COUNT(*) FROM nodes WHERE embedding IS NOT NULL"
                ).fetchone()[0]
                snap["embedding_coverage_pct"] = round(
                    (with_emb / total * 100) if total > 0 else 100, 1
                )

                # Storage strength stats
                ss_row = conn.execute(
                    "SELECT AVG(storage_strength), MIN(storage_strength), MAX(storage_strength) "
                    "FROM nodes WHERE storage_strength IS NOT NULL"
                ).fetchone()
                snap["storage_strength"] = {
                    "avg": round(ss_row[0], 3) if ss_row[0] is not None else None,
                    "min": round(ss_row[1], 3) if ss_row[1] is not None else None,
                    "max": round(ss_row[2], 3) if ss_row[2] is not None else None,
                }

                # Project metrics (guarded by table existence)
                try:
                    proj_count = conn.execute(
                        "SELECT COUNT(*) FROM project_definitions WHERE state = 'active'"
                    ).fetchone()[0]
                    snap["projects"] = proj_count
                except Exception:
                    snap["projects"] = 0

                try:
                    reg_count = conn.execute(
                        "SELECT COUNT(*) FROM doc_registry WHERE state = 'active'"
                    ).fetchone()[0]
                    snap["registered_docs"] = reg_count
                except Exception:
                    snap["registered_docs"] = 0

                try:
                    chunk_count = conn.execute(
                        "SELECT COUNT(*) FROM doc_chunks"
                    ).fetchone()[0]
                    snap["rag_chunks"] = chunk_count
                except Exception:
                    snap["rag_chunks"] = 0
        except Exception as e:
            snap["db_error"] = str(e)

        # DB file size
        try:
            db_path = get_db_path()
            snap["db_size_bytes"] = os.path.getsize(db_path)
            size = snap["db_size_bytes"]
            if size < 1024:
                snap["db_size_human"] = f"{size}B"
            elif size < 1024 * 1024:
                snap["db_size_human"] = f"{size / 1024:.1f}KB"
            else:
                snap["db_size_human"] = f"{size / (1024 * 1024):.1f}MB"
        except Exception:
            snap["db_size_bytes"] = 0
            snap["db_size_human"] = "unknown"

        try:
            with get_connection() as conn:
                snap["fts_row_count"] = conn.execute(
                    "SELECT COUNT(*) FROM nodes_fts"
                ).fetchone()[0]
        except Exception:
            snap["fts_row_count"] = 0

        # Recall quality (fast 10-query probe) with MRR and latency percentiles
        try:
            hits = 0
            latencies = []
            mrr_scores = []
            similarities = []
            for query, expected_subs, _ in _PROBE_QUERIES:
                t0 = time.monotonic()
                results = recall(query, limit=5, owner_id="default", use_routing=False)
                latencies.append((time.monotonic() - t0) * 1000)

                # Per-result relevance for MRR
                first_hit_rank = 0
                for i, r in enumerate(results):
                    text_lower = r.get("text", "").lower()
                    if any(sub.lower() in text_lower for sub in expected_subs):
                        if first_hit_rank == 0:
                            first_hit_rank = i + 1
                    # Track similarity scores
                    sim = r.get("similarity")
                    if sim is not None:
                        similarities.append(sim)

                if first_hit_rank > 0:
                    hits += 1
                    mrr_scores.append(1.0 / first_hit_rank)
                else:
                    mrr_scores.append(0.0)

            snap["recall_at_5"] = round(hits / len(_PROBE_QUERIES), 3)
            snap["mrr"] = round(statistics.mean(mrr_scores), 3) if mrr_scores else 0.0
            snap["avg_latency_ms"] = round(statistics.mean(latencies), 1) if latencies else 0
            if latencies:
                latencies.sort()
                n = len(latencies)
                snap["latency_p50_ms"] = round(latencies[n // 2], 1)
                snap["latency_p95_ms"] = round(latencies[min(math.ceil(n * 0.95) - 1, n - 1)], 1)
            else:
                snap["latency_p50_ms"] = 0
                snap["latency_p95_ms"] = 0
            if similarities:
                snap["avg_similarity"] = round(statistics.mean(similarities), 3)
                snap["max_similarity"] = round(max(similarities), 3)
            else:
                snap["avg_similarity"] = 0.0
                snap["max_similarity"] = 0.0
        except Exception as e:
            snap["recall_error"] = str(e)
            snap["recall_at_5"] = 0.0
            snap["mrr"] = 0.0
            snap["avg_latency_ms"] = 0
            snap["latency_p50_ms"] = 0
            snap["latency_p95_ms"] = 0

        self.snapshots.append(snap)
        return snap

    def save(self) -> Path:
        """Write snapshots to metrics-timeline.jsonl."""
        output_path = self.output_dir / "metrics-timeline.jsonl"
        with open(output_path, "w") as f:
            for snap in self.snapshots:
                f.write(json.dumps(snap) + "\n")
        return output_path

    def summary(self) -> Dict:
        """Return comparison: first vs last snapshot + trends."""
        if len(self.snapshots) < 2:
            return {"error": "Need at least 2 snapshots for comparison"}

        first = self.snapshots[0]
        last = self.snapshots[-1]

        def _delta(key: str, fmt: str = "d") -> str:
            f_val = first.get(key, 0) or 0
            l_val = last.get(key, 0) or 0
            diff = l_val - f_val
            sign = "+" if diff >= 0 else ""
            if fmt == "pct":
                return f"{f_val:.0%} → {l_val:.0%} ({sign}{diff:.0%})"
            elif fmt == "f":
                return f"{f_val:.2f} → {l_val:.2f} ({sign}{diff:.2f})"
            else:
                return f"{f_val} → {l_val} ({sign}{diff})"

        result = {
            "snapshots": len(self.snapshots),
            "nodes": _delta("nodes"),
            "edges": _delta("edges"),
            "recall_at_5": _delta("recall_at_5", "pct"),
            "mrr": _delta("mrr", "f"),
            "avg_latency_ms": _delta("avg_latency_ms", "f"),
            "latency_p50_ms": _delta("latency_p50_ms", "f"),
            "latency_p95_ms": _delta("latency_p95_ms", "f"),
            "avg_similarity": _delta("avg_similarity", "f"),
            "db_size_human": f"{first.get('db_size_human', '?')} → {last.get('db_size_human', '?')}",
            "embedding_coverage_pct": _delta("embedding_coverage_pct", "f"),
            "dedup_log_count": _delta("dedup_log_count"),
            "contradiction_count": _delta("contradiction_count"),
        }
        return result

    def print_summary(self):
        """Print human-readable summary to stdout."""
        s = self.summary()
        if "error" in s:
            print(f"  {s['error']}")
            return

        print(f"\nMetrics Timeline ({s['snapshots']} snapshots):")
        print(f"  Nodes:          {s['nodes']}")
        print(f"  Edges:          {s['edges']}")
        print(f"  Recall@5:       {s['recall_at_5']}")
        print(f"  MRR:            {s['mrr']}")
        print(f"  Avg latency:    {s['avg_latency_ms']}ms")
        print(f"  Latency p50:    {s['latency_p50_ms']}ms")
        print(f"  Latency p95:    {s['latency_p95_ms']}ms")
        print(f"  Avg similarity: {s['avg_similarity']}")
        print(f"  DB size:        {s['db_size_human']}")
        print(f"  Embedding cov:  {s['embedding_coverage_pct']}%")
        print(f"  Dedup log:      {s['dedup_log_count']}")
        print(f"  Contradictions: {s['contradiction_count']}")


def main():
    """Take a one-off snapshot and print it."""
    parser = argparse.ArgumentParser(description="Take a metrics snapshot")
    parser.add_argument("--db", help="Path to memory DB (uses MEMORY_DB_PATH env)")
    args = parser.parse_args()

    logger = MetricsLogger(Path("."))
    snap = logger.snapshot("one-off")
    print(json.dumps(snap, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
