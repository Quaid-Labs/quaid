#!/usr/bin/env python3
"""E2E domain contract check for domain-tagged storage and recall filtering."""

from __future__ import annotations

import os
import sys
import tempfile
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
    personal = mg.store(
        text="Quaid prefers espresso drinks after lunch",
        owner_id="quaid",
        skip_dedup=True,
        domains=["personal"],
    )
    technical = mg.store(
        text="Quaid refactored endpoint retries for deployment stability",
        owner_id="quaid",
        skip_dedup=True,
        domains=["technical"],
    )

    with graph._get_conn() as conn:
        p_domains = [r["domain"] for r in conn.execute("SELECT domain FROM node_domains WHERE node_id = ?", (personal["id"],)).fetchall()]
        t_domains = [r["domain"] for r in conn.execute("SELECT domain FROM node_domains WHERE node_id = ?", (technical["id"],)).fetchall()]

    technical_results = mg.recall(
        "endpoint retries deployment",
        owner_id="quaid",
        use_routing=False,
        min_similarity=0.0,
        domain={"technical": True},
    )
    personal_results = mg.recall(
        "espresso drinks",
        owner_id="quaid",
        use_routing=False,
        min_similarity=0.0,
        domain={"personal": True},
    )

    tech_ids = {r.get("id") for r in technical_results}
    personal_ids = {r.get("id") for r in personal_results}

    assert p_domains == ["personal"], f"unexpected personal node_domains: {p_domains}"
    assert t_domains == ["technical"], f"unexpected technical node_domains: {t_domains}"
    assert technical["id"] in tech_ids, "technical recall failed to return technical fact"
    assert personal["id"] not in tech_ids, "technical recall returned personal fact"
    assert personal["id"] in personal_ids, "personal recall failed to return personal fact"
    assert technical["id"] not in personal_ids, "personal recall returned technical fact"

    print(
        "{"
        f"\"status\": \"ok\", "
        f"\"db_path\": \"{db_path}\", "
        f"\"technical_hits\": {len(technical_results)}, "
        f"\"personal_hits\": {len(personal_results)}"
        "}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
