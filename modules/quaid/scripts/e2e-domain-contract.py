#!/usr/bin/env python3
"""E2E domain contract check for memorydb domain filtering + legacy fallback.

Validates:
1) Legacy rows with attributes.is_technical are backfilled into attrs.domains.
2) node_domains mirror rows are created for legacy rows.
3) recall(domain={"technical": true}) returns legacy technical rows.
4) recall(domain={"personal": true}) excludes legacy technical rows.
"""

from __future__ import annotations

import json
import os
import sqlite3
import sys
import tempfile
import uuid
from pathlib import Path


def main() -> int:
    ws = Path(sys.argv[1]).resolve() if len(sys.argv) > 1 else Path.cwd()
    db_fd, db_path_raw = tempfile.mkstemp(prefix="quaid-e2e-domain-", suffix=".db")
    os.close(db_fd)
    db_path = Path(db_path_raw)

    os.environ["CLAWDBOT_WORKSPACE"] = str(ws)
    os.environ["QUAID_HOME"] = str(ws)
    os.environ["MEMORY_DB_PATH"] = str(db_path)

    module_root = ws / "modules" / "quaid"
    sys.path.insert(0, str(module_root))
    import datastore.memorydb.memory_graph as mg  # noqa: WPS433

    # Keep this check deterministic and offline.
    mg.route_query = lambda q: q
    mg._lib_get_embedding = lambda text: [0.1] * 128

    graph = mg.MemoryGraph(db_path=db_path)
    legacy_id = str(uuid.uuid4())
    legacy_text = "Legacy technical endpoint refactor for benchmark stability"

    with graph._get_conn() as conn:
        conn.execute(
            """
            INSERT INTO nodes (id, type, name, attributes, owner_id, status, confidence, created_at, updated_at, accessed_at)
            VALUES (?, 'Fact', ?, ?, 'quaid', 'active', 0.9, datetime('now'), datetime('now'), datetime('now'))
            """,
            (legacy_id, legacy_text, json.dumps({"is_technical": True})),
        )

    # Re-open triggers init-time backfill.
    graph2 = mg.MemoryGraph(db_path=db_path)

    technical_results = mg.recall(
        "endpoint refactor benchmark",
        owner_id="quaid",
        use_routing=False,
        min_similarity=0.0,
        domain={"technical": True},
    )
    personal_results = mg.recall(
        "endpoint refactor benchmark",
        owner_id="quaid",
        use_routing=False,
        min_similarity=0.0,
        domain={"personal": True},
    )

    with graph2._get_conn() as conn:
        attrs_row = conn.execute("SELECT attributes FROM nodes WHERE id = ?", (legacy_id,)).fetchone()
        nd_rows = conn.execute("SELECT domain FROM node_domains WHERE node_id = ?", (legacy_id,)).fetchall()

    attrs = json.loads(attrs_row["attributes"] if attrs_row else "{}")
    mirrored = sorted([r["domain"] for r in nd_rows])
    tech_ids = {r.get("id") for r in technical_results}
    personal_ids = {r.get("id") for r in personal_results}

    assert "technical" in attrs.get("domains", []), "legacy row missing attrs.domains backfill"
    assert mirrored == ["technical"], f"unexpected node_domains mirror rows: {mirrored}"
    assert legacy_id in tech_ids, "technical recall failed to return legacy technical row"
    assert legacy_id not in personal_ids, "personal recall should exclude legacy technical row"

    print(
        json.dumps(
            {
                "status": "ok",
                "legacy_id": legacy_id,
                "db_path": str(db_path),
                "technical_hits": len(technical_results),
                "personal_hits": len(personal_results),
            },
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
