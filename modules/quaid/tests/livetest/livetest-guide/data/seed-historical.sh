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
# Each row has a distinctive keyword so the tester can find it with an FTS
# MATCH / name LIKE query.

set -euo pipefail

INSTANCE="${INSTANCE:?INSTANCE env var required (e.g. claude-code-private-tmp-cc-livetest)}"
DB="${HOME}/.quaid/instances/${INSTANCE}/data/memory.db"

if [[ ! -f "$DB" ]]; then
    echo "FAIL: memory.db not found at $DB" >&2
    exit 1
fi

# Real schema: nodes(id TEXT PK, type TEXT NOT NULL, name TEXT NOT NULL,
# attributes TEXT, status TEXT DEFAULT 'approved', created_at, updated_at,
# ...). There is no `body` / `domain` column. Extracted text lives in
# `name`. Domain metadata lives in `attributes` JSON.

insert_row() {
    local marker="$1"
    local created_at="$2"
    local name="$3"
    local domain="${4:-personal}"
    local id
    id="$(python3 -c 'import uuid; print(uuid.uuid4())')"
    local name_esc="${name//\'/\'\'}"
    sqlite3 "$DB" <<SQL
DELETE FROM nodes WHERE name LIKE '%${marker}%';
INSERT INTO nodes (id, type, name, attributes, status, created_at, updated_at)
VALUES ('$id', 'Fact', '${name_esc}', json_object('domains', json_array('$domain')),
        'approved', '$created_at', '$created_at');
SQL
}

# Historical rows span 2023-Q1 through 2024-Q4 so probes at 2023-06-01,
# 2024-01-01, 2024-06-01, and 2025-01-01 produce different result sets.
# Keep the distinctive keyword in each row's name text so FTS / name LIKE
# queries find them.

insert_row "hist-amber-valentine-2023" "2023-02-14T10:00:00Z" \
  "hist-amber-valentine-2023: on 2023-02-14 we had the amber-tinted valentine dinner at the rooftop bistro." \
  "personal"

insert_row "hist-ironwood-workshop-2023" "2023-05-03T09:30:00Z" \
  "hist-ironwood-workshop-2023: ironwood woodworking workshop ran over three Saturdays starting 2023-05-03." \
  "personal"

insert_row "hist-meridian-summit-2023" "2023-09-20T14:15:00Z" \
  "hist-meridian-summit-2023: meridian technical summit in early autumn 2023 — session on distributed tracing." \
  "technical"

insert_row "hist-jasper-retreat-2024" "2024-01-18T08:00:00Z" \
  "hist-jasper-retreat-2024: jasper company retreat, week of 2024-01-18 — three nights at the lakeside lodge." \
  "work"

insert_row "hist-cobalt-release-2024" "2024-04-07T20:00:00Z" \
  "hist-cobalt-release-2024: cobalt-tagged release cut on 2024-04-07, first public install of the rebuilt docs CLI." \
  "project"

insert_row "hist-sepia-reading-2024" "2024-08-30T19:45:00Z" \
  "hist-sepia-reading-2024: sepia-series reading group finished its third book on 2024-08-30." \
  "personal"

insert_row "hist-carbon-audit-2024" "2024-11-11T11:11:00Z" \
  "hist-carbon-audit-2024: carbon audit for the year wrapped on 2024-11-11 — all checks cleared." \
  "work"

echo "seeded 7 historical rows into $DB"
