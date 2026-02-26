import sqlite3

from datastore.memorydb.maintenance_ops import JanitorMetrics, resolve_contradictions_with_opus


class _Graph:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def _get_conn(self):
        return self._conn


def test_contradictions_carryover_counts_pending_status(monkeypatch):
    conn = sqlite3.connect(":memory:")
    try:
        conn.execute(
            """
            CREATE TABLE contradictions (
                id TEXT PRIMARY KEY,
                status TEXT
            )
            """
        )
        conn.executemany(
            "INSERT INTO contradictions (id, status) VALUES (?, ?)",
            [
                ("c1", "pending"),
                ("c2", "pending"),
                ("c3", "pending"),
                ("c4", "resolved"),
            ],
        )
        conn.commit()

        # Keep this test in the carryover-count path only (no LLM/deep resolution).
        monkeypatch.setattr(
            "datastore.memorydb.maintenance_ops.get_pending_contradictions",
            lambda limit=50: [],
        )

        out = resolve_contradictions_with_opus(
            graph=_Graph(conn),
            metrics=JanitorMetrics(),
            dry_run=True,
            max_items=2,
        )

        assert out["carryover"] == 3
    finally:
        conn.close()
