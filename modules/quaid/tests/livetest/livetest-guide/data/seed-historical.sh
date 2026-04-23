#!/usr/bin/env bash
# seed-historical.sh — Inject dated historical rows into an instance memory.db
# for M3 Part D date-range recall tests.
#
# The rolling transcript seeds a handful of recent dated facts, but date-range
# recall needs entries spanning a wider window (including rows strictly before
# any date_from we test). This script injects a fixed, known set of rows with
# known `created_at` timestamps so the tester can write deterministic
# assertions.
#
# Usage (run on the remote, inside the instance silo):
#
#   INSTANCE=<instance_id> ~/quaidcode/dev/modules/quaid/tests/livetest/livetest-guide/data/seed-historical.sh
#
# Idempotent: re-running the same script replaces the marker rows by name.

set -euo pipefail

INSTANCE="${INSTANCE:?INSTANCE env var required (e.g. claude-code-private-tmp-cc-livetest)}"
DB="${HOME}/.quaid/instances/${INSTANCE}/data/memory.db"

if [[ ! -f "$DB" ]]; then
    echo "FAIL: memory.db not found at $DB" >&2
    exit 1
fi

python3 - "$DB" <<'PYEOF'
import json
import sqlite3
import sys
import uuid

db_path = sys.argv[1]

# Real nodes schema (relevant columns):
#   id TEXT PK, type TEXT NOT NULL, name TEXT NOT NULL,
#   attributes TEXT (JSON), status TEXT DEFAULT 'approved',
#   created_at TEXT, updated_at TEXT
# There is no body or domain column. Domain metadata lives in attributes JSON.

rows = [
    ("hist-amber-valentine-2023", "2023-02-14T10:00:00Z", "personal",
     "hist-amber-valentine-2023: on 2023-02-14 we had the amber-tinted valentine dinner at the rooftop bistro."),
    ("hist-ironwood-workshop-2023", "2023-05-03T09:30:00Z", "personal",
     "hist-ironwood-workshop-2023: ironwood woodworking workshop ran over three Saturdays starting 2023-05-03."),
    ("hist-meridian-summit-2023", "2023-09-20T14:15:00Z", "technical",
     "hist-meridian-summit-2023: meridian technical summit in early autumn 2023 — session on distributed tracing."),
    ("hist-jasper-retreat-2024", "2024-01-18T08:00:00Z", "work",
     "hist-jasper-retreat-2024: jasper company retreat, week of 2024-01-18 — three nights at the lakeside lodge."),
    ("hist-cobalt-release-2024", "2024-04-07T20:00:00Z", "project",
     "hist-cobalt-release-2024: cobalt-tagged release cut on 2024-04-07, first public install of the rebuilt docs CLI."),
    ("hist-sepia-reading-2024", "2024-08-30T19:45:00Z", "personal",
     "hist-sepia-reading-2024: sepia-series reading group finished its third book on 2024-08-30."),
    ("hist-carbon-audit-2024", "2024-11-11T11:11:00Z", "work",
     "hist-carbon-audit-2024: carbon audit for the year wrapped on 2024-11-11 — all checks cleared."),
]

conn = sqlite3.connect(db_path)
conn.row_factory = sqlite3.Row
inserted = 0
replaced = 0
try:
    cur = conn.cursor()
    # Delete any prior marker rows (idempotency). We delete one at a time
    # so FTS triggers don't trip on an unsafe bulk-delete path.
    for marker, _, _, _ in rows:
        cur.execute(
            "DELETE FROM nodes WHERE name LIKE ?",
            (f"%{marker}%",),
        )
        if cur.rowcount:
            replaced += cur.rowcount
        conn.commit()

    # Insert fresh
    for marker, created_at, domain, name in rows:
        cur.execute(
            """
            INSERT INTO nodes
                (id, type, name, attributes, status, created_at, updated_at)
            VALUES (?, ?, ?, ?, 'approved', ?, ?)
            """,
            (
                str(uuid.uuid4()),
                "Fact",
                name,
                json.dumps({"domains": [domain]}),
                created_at,
                created_at,
            ),
        )
        inserted += 1
        conn.commit()
    print(f"seeded {inserted} historical rows ({replaced} prior markers replaced) into {db_path}")
finally:
    conn.close()
PYEOF
