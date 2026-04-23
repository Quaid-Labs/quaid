#!/usr/bin/env bash
# seed-historical.sh — Inject dated historical rows into an instance memory.db
# for M3 Part D date-range recall tests.
#
# The rolling transcript seeds a handful of recent dated facts, but date-range
# recall needs entries spanning a wider window (including rows strictly before
# any date_from we test). This script injects a fixed, known set of rows with
# known `created_at` timestamps so the tester can write deterministic assertions.
#
# Usage (run on the remote, inside the instance silo):
#
#   INSTANCE=<instance_id> ~/quaidcode/dev/modules/quaid/tests/livetest/livetest-guide/data/seed-historical.sh
#
# Idempotent: re-running the same script rewrites the marker rows with the same
# content and timestamps. Each row has a distinctive keyword so the tester can
# find it with an FTS MATCH.

set -euo pipefail

INSTANCE="${INSTANCE:?INSTANCE env var required (e.g. claude-code-private-tmp-cc-livetest)}"
DB="${HOME}/.quaid/instances/${INSTANCE}/data/memory.db"

if [[ ! -f "$DB" ]]; then
    echo "FAIL: memory.db not found at $DB" >&2
    exit 1
fi

insert_row() {
    local created_at="$1"
    local name="$2"
    local body="$3"
    local domain="${4:-personal}"
    sqlite3 "$DB" <<SQL
INSERT INTO nodes (name, body, domain, created_at, updated_at, status)
VALUES ('${name//\'/\'\'}', '${body//\'/\'\'}', '$domain', '$created_at', '$created_at', 'approved')
ON CONFLICT(name) DO UPDATE SET
    body = excluded.body,
    created_at = excluded.created_at,
    updated_at = excluded.updated_at;
SQL
}

# Historical rows span from 2023-Q1 through 2024-Q4 so probes at 2023-06-01,
# 2024-01-01, 2024-06-01, and 2025-01-01 produce different result sets.
insert_row "2023-02-14T10:00:00Z" "hist-amber-valentine-2023" \
    "On 2023-02-14 we had the amber-tinted valentine dinner at the rooftop bistro." \
    "personal"

insert_row "2023-05-03T09:30:00Z" "hist-ironwood-workshop-2023" \
    "Ironwood woodworking workshop ran over three Saturdays starting 2023-05-03." \
    "personal"

insert_row "2023-09-20T14:15:00Z" "hist-meridian-summit-2023" \
    "Meridian technical summit in early autumn 2023 — session on distributed tracing." \
    "technical"

insert_row "2024-01-18T08:00:00Z" "hist-jasper-retreat-2024" \
    "Jasper company retreat, week of 2024-01-18 — three nights at the lakeside lodge." \
    "work"

insert_row "2024-04-07T20:00:00Z" "hist-cobalt-release-2024" \
    "Cobalt-tagged release cut on 2024-04-07, first public install of the rebuilt docs CLI." \
    "project"

insert_row "2024-08-30T19:45:00Z" "hist-sepia-reading-2024" \
    "Sepia-series reading group finished its third book on 2024-08-30." \
    "personal"

insert_row "2024-11-11T11:11:00Z" "hist-carbon-audit-2024" \
    "Carbon audit for the year wrapped on 2024-11-11 — all checks cleared." \
    "work"

echo "seeded 7 historical rows into $DB"
