#!/bin/bash
# tmux-mailbox.sh — Queue-backed mailbox for tmux-coordinated agents
#
# Usage:
#   tmux-mailbox.sh post [--kind KIND] [--lane LANE] [--notify 0|1] <target> <message>
#   tmux-mailbox.sh count <target>
#   tmux-mailbox.sh list [--limit N] <target>
#   tmux-mailbox.sh next <target>
#   tmux-mailbox.sh done <target> <message-id>
#   tmux-mailbox.sh reply <target> <message-id> <response>
#   tmux-mailbox.sh ack <target> <message-id>
#   tmux-mailbox.sh targets
#
# Targets use the same address forms as tmux-msg.sh:
#   0-99, self, main:N.0, livetest:NAME, or alias from .tmux-targets.json
#
# Notes:
#   - `post` requires TMUX_MSG_SENDER and uses TMUX_MSG_SOURCE when set.
#   - Mailbox data is stored under tests/livetest/scripts/.tmux-mailbox/ by default.
#   - Routine STATUS/ISSUE traffic should use the mailbox.
#   - Direct tmux messages remain appropriate for URGENT / INTERRUPT-level traffic.
#   - When a queue goes from empty to non-empty, the first mailbox item is delivered inline.
#   - `reply` and `done` both acknowledge the current item and immediately return the next one.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TARGETS_FILE="${TMUX_MSG_TARGETS_FILE:-$SCRIPT_DIR/.tmux-targets.json}"
MAILBOX_ROOT="${TMUX_MAILBOX_ROOT:-$SCRIPT_DIR/.tmux-mailbox}"
TMUX_MSG_SCRIPT="${TMUX_MAILBOX_TMUX_MSG_SCRIPT:-$SCRIPT_DIR/tmux-msg.sh}"
COMMAND="${1:-}"

if [[ -z "$COMMAND" ]]; then
    echo "Usage: tmux-mailbox.sh <post|count|list|next|done|reply|ack|targets> ..." >&2
    exit 1
fi

shift || true

mkdir -p "$MAILBOX_ROOT"

_detected_pane="$(tmux display-message -p '#{session_name}:#{window_index}.#{pane_index}' 2>/dev/null || echo "unknown")"
SENDER="${TMUX_MSG_SENDER:-}"
SENDER_PANE="${TMUX_MSG_SOURCE:-$_detected_pane}"

resolve_target() {
    local target="${1:-}"
    local resolved=""

    case "$target" in
        self)
            resolved="${TMUX_PANE:-$_detected_pane}"
            ;;
        [0-9]|[0-9][0-9])
            resolved="main:${target}.0"
            ;;
        main:*|livetest:*)
            resolved="$target"
            ;;
        *)
            resolved="$(python3 - "$TARGETS_FILE" "$target" <<'PY'
import json
import pathlib
import sys

cfg = pathlib.Path(sys.argv[1])
key = sys.argv[2]
if not cfg.is_file():
    print("")
    raise SystemExit(0)
try:
    data = json.loads(cfg.read_text(encoding="utf-8"))
except Exception:
    print("")
    raise SystemExit(0)
if not isinstance(data, dict):
    print("")
    raise SystemExit(0)
value = data.get(key, "")
print(value if isinstance(value, str) else "")
PY
)"
            if [[ -z "$resolved" ]]; then
                echo "Error: unknown target '$target'" >&2
                echo "Valid: 0-99, self, main:N.0, livetest:NAME, or alias in $TARGETS_FILE" >&2
                exit 1
            fi
            ;;
    esac

    printf '%s\n' "$resolved"
}

mailbox_python() {
    python3 - "$MAILBOX_ROOT" "$@"
}

usage_post() {
    echo "Usage: tmux-mailbox.sh post [--kind KIND] [--lane LANE] [--notify 0|1] <target> <message>" >&2
}

usage_done() {
    echo "Usage: tmux-mailbox.sh done <target> <message-id>" >&2
}

usage_reply() {
    echo "Usage: tmux-mailbox.sh reply <target> <message-id> <response>" >&2
}

sanitize_inline_message() {
    python3 - "$1" <<'PY'
import sys
text = sys.argv[1].replace("\n", " ")
text = " ".join(text.split())
cap = 220
if len(text) > cap:
    text = text[: cap - 3] + "..."
print(text)
PY
}

format_notify_message() {
    local message_id="$1"
    local kind="$2"
    local lane="$3"
    local sender="$4"
    local message="$5"
    local preview
    local lane_prefix=""
    preview="$(sanitize_inline_message "$message")"
    if [[ -n "$lane" ]]; then
        lane_prefix=" lane=${lane}"
    fi
    printf 'MAILBOX id=%s kind=%s%s from=%s: %s' \
        "$message_id" "$kind" "$lane_prefix" "$sender" "$preview"
}

lookup_pending_message() {
    local target="$1"
    local message_id="$2"
    mailbox_python "$target" lookup "$message_id" <<'PY'
import fcntl
import json
import pathlib
import sys

root = pathlib.Path(sys.argv[1])
target = sys.argv[2]
op = sys.argv[3]
message_id = sys.argv[4]
lock_path = root / "mailbox.lock"
messages_path = root / "messages.jsonl"
acks_path = root / "acks.jsonl"
root.mkdir(parents=True, exist_ok=True)
lock_path.touch(exist_ok=True)

def load_jsonl(path):
    if not path.exists():
        return []
    with path.open("r", encoding="utf-8") as handle:
        return [json.loads(raw) for raw in handle if raw.strip()]

with lock_path.open("r+", encoding="utf-8") as lock_handle:
    fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX)
    messages = load_jsonl(messages_path)
    acked = {(row.get("target"), row.get("id")) for row in load_jsonl(acks_path)}
    for row in messages:
        if row.get("target") != target:
            continue
        if row.get("id") != message_id:
            continue
        if (target, message_id) in acked:
            print("Message already acknowledged", file=sys.stderr)
            raise SystemExit(4)
        print(json.dumps(row, ensure_ascii=True))
        raise SystemExit(0)
    print(f"Pending message {message_id} not found for {target}", file=sys.stderr)
    raise SystemExit(3)
PY
}

ack_and_render_next() {
    local target="$1"
    local message_id="$2"
    local actor="${SENDER:-unknown}"
    local actor_source="${SENDER_PANE:-unknown}"
    mailbox_python "$target" done "$message_id" "$actor" "$actor_source" <<'PY'
import fcntl
import json
import pathlib
import sys
import time

root = pathlib.Path(sys.argv[1])
target = sys.argv[2]
op = sys.argv[3]
message_id = sys.argv[4]
sender = sys.argv[5]
source = sys.argv[6]
lock_path = root / "mailbox.lock"
messages_path = root / "messages.jsonl"
acks_path = root / "acks.jsonl"
root.mkdir(parents=True, exist_ok=True)
lock_path.touch(exist_ok=True)

def load_jsonl(path):
    if not path.exists():
        return []
    with path.open("r", encoding="utf-8") as handle:
        return [json.loads(raw) for raw in handle if raw.strip()]

def render(row):
    lane = row.get("lane") or "-"
    return "\n".join([
        f"ID: {row['id']}",
        f"When: {row.get('posted_at', '-')}",
        f"From: {row.get('sender', '-')} @ {row.get('source', '-')}",
        f"Kind: {row.get('kind', '-')}",
        f"Lane: {lane}",
        "Message:",
        str(row.get("message", "")),
    ])

with lock_path.open("r+", encoding="utf-8") as lock_handle:
    fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX)
    messages = load_jsonl(messages_path)
    acked_rows = load_jsonl(acks_path)
    acked = {(row.get("target"), row.get("id")) for row in acked_rows}

    current = None
    for row in messages:
        if row.get("target") == target and row.get("id") == message_id:
            current = row
            break

    if current is None:
        print(f"Pending message {message_id} not found for {target}", file=sys.stderr)
        raise SystemExit(3)
    if (target, message_id) in acked:
        print("Already acknowledged")
        raise SystemExit(0)

    record = {
        "id": message_id,
        "target": target,
        "acked_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "acked_by": sender,
        "acked_source": source,
    }
    with acks_path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, ensure_ascii=True) + "\n")

    acked.add((target, message_id))
    pending = [
        row for row in messages
        if row.get("target") == target and (target, row.get("id")) not in acked
    ]

    print(f"Acknowledged {message_id} for {target}")
    if not pending:
        raise SystemExit(0)
    print("")
    print(render(pending[0]))
PY
}

case "$COMMAND" in
    post)
        if [[ -z "$SENDER" ]]; then
            echo "Error: TMUX_MSG_SENDER is required for mailbox posts" >&2
            exit 1
        fi

        KIND="STATUS"
        LANE=""
        NOTIFY="1"

        while [[ $# -gt 0 ]]; do
            case "$1" in
                --kind)
                    KIND="${2:-}"
                    shift 2
                    ;;
                --lane)
                    LANE="${2:-}"
                    shift 2
                    ;;
                --notify)
                    NOTIFY="${2:-}"
                    shift 2
                    ;;
                --help|-h)
                    usage_post
                    exit 0
                    ;;
                --*)
                    echo "Error: unknown option '$1'" >&2
                    usage_post
                    exit 1
                    ;;
                *)
                    break
                    ;;
            esac
        done

        if [[ $# -lt 2 ]]; then
            usage_post
            exit 1
        fi

        TARGET="$(resolve_target "$1")"
        shift
        MESSAGE="$*"

        RESULT="$(mailbox_python "$TARGET" post "$SENDER" "$SENDER_PANE" "$KIND" "$LANE" "$MESSAGE" <<'PY'
import fcntl
import json
import os
import pathlib
import sys
import time
import uuid

root = pathlib.Path(sys.argv[1])
target = sys.argv[2]
op = sys.argv[3]
sender = sys.argv[4]
source = sys.argv[5]
kind = sys.argv[6]
lane = sys.argv[7]
message = sys.argv[8]

root.mkdir(parents=True, exist_ok=True)
lock_path = root / "mailbox.lock"
lock_path.touch(exist_ok=True)
messages_path = root / "messages.jsonl"
acks_path = root / "acks.jsonl"

def load_jsonl(path):
    if not path.exists():
        return []
    rows = []
    with path.open("r", encoding="utf-8") as handle:
        for raw in handle:
            raw = raw.strip()
            if not raw:
                continue
            rows.append(json.loads(raw))
    return rows

with lock_path.open("r+", encoding="utf-8") as lock_handle:
    fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX)
    messages = load_jsonl(messages_path)
    acks = load_jsonl(acks_path)
    acked_ids = {row["id"] for row in acks if row.get("target") == target}
    pending_before = [
        row for row in messages
        if row.get("target") == target and row.get("id") not in acked_ids
    ]
    record = {
        "id": f"{int(time.time() * 1000)}-{os.getpid()}-{uuid.uuid4().hex[:8]}",
        "posted_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "target": target,
        "sender": sender,
        "source": source,
        "kind": kind,
        "lane": lane,
        "message": message,
    }
    with messages_path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, ensure_ascii=True) + "\n")
    pending_after = pending_before + [record]
    print(json.dumps({
        "record": record,
        "pending_before": len(pending_before),
        "pending_after": len(pending_after),
    }))
PY
)"

        MESSAGE_ID="$(python3 -c 'import json,sys; data=json.loads(sys.stdin.read()); print(data["record"]["id"])' <<<"$RESULT")"
        PENDING_BEFORE="$(python3 -c 'import json,sys; data=json.loads(sys.stdin.read()); print(data["pending_before"])' <<<"$RESULT")"
        PENDING_AFTER="$(python3 -c 'import json,sys; data=json.loads(sys.stdin.read()); print(data["pending_after"])' <<<"$RESULT")"
        FIRST_KIND="$(python3 -c 'import json,sys; data=json.loads(sys.stdin.read()); print(data["record"].get("kind", ""))' <<<"$RESULT")"
        FIRST_LANE="$(python3 -c 'import json,sys; data=json.loads(sys.stdin.read()); print(data["record"].get("lane", ""))' <<<"$RESULT")"
        FIRST_SENDER="$(python3 -c 'import json,sys; data=json.loads(sys.stdin.read()); print(data["record"].get("sender", ""))' <<<"$RESULT")"
        FIRST_MESSAGE="$(python3 -c 'import json,sys; data=json.loads(sys.stdin.read()); print(data["record"].get("message", ""))' <<<"$RESULT")"

        if [[ "$NOTIFY" != "0" ]] && [[ "$PENDING_BEFORE" == "0" ]] && [[ -x "$TMUX_MSG_SCRIPT" ]]; then
            NOTIFY_TEXT="$(format_notify_message "$MESSAGE_ID" "$FIRST_KIND" "$FIRST_LANE" "$FIRST_SENDER" "$FIRST_MESSAGE")"
            TMUX_MSG_SENDER="$SENDER" TMUX_MSG_SOURCE="$SENDER_PANE" "$TMUX_MSG_SCRIPT" "$TARGET" "$NOTIFY_TEXT" >/dev/null 2>&1 || true
        fi

        echo "Queued id=$MESSAGE_ID target=$TARGET pending=$PENDING_AFTER"
        ;;

    count)
        if [[ $# -ne 1 ]]; then
            echo "Usage: tmux-mailbox.sh count <target>" >&2
            exit 1
        fi
        TARGET="$(resolve_target "$1")"
        mailbox_python "$TARGET" count <<'PY'
import fcntl
import json
import pathlib
import sys

root = pathlib.Path(sys.argv[1])
target = sys.argv[2]
lock_path = root / "mailbox.lock"
messages_path = root / "messages.jsonl"
acks_path = root / "acks.jsonl"
root.mkdir(parents=True, exist_ok=True)
lock_path.touch(exist_ok=True)

def load_jsonl(path):
    if not path.exists():
        return []
    with path.open("r", encoding="utf-8") as handle:
        return [json.loads(raw) for raw in handle if raw.strip()]

with lock_path.open("r+", encoding="utf-8") as lock_handle:
    fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX)
    messages = load_jsonl(messages_path)
    acked = {row["id"] for row in load_jsonl(acks_path) if row.get("target") == target}
    pending = [row for row in messages if row.get("target") == target and row.get("id") not in acked]
    print(len(pending))
PY
        ;;

    list)
        LIMIT=""
        while [[ $# -gt 0 ]]; do
            case "$1" in
                --limit)
                    LIMIT="${2:-}"
                    shift 2
                    ;;
                --help|-h)
                    echo "Usage: tmux-mailbox.sh list [--limit N] <target>" >&2
                    exit 0
                    ;;
                --*)
                    echo "Error: unknown option '$1'" >&2
                    exit 1
                    ;;
                *)
                    break
                    ;;
            esac
        done

        if [[ $# -ne 1 ]]; then
            echo "Usage: tmux-mailbox.sh list [--limit N] <target>" >&2
            exit 1
        fi
        TARGET="$(resolve_target "$1")"
        mailbox_python "$TARGET" list "$LIMIT" <<'PY'
import fcntl
import json
import pathlib
import sys

root = pathlib.Path(sys.argv[1])
target = sys.argv[2]
limit_arg = sys.argv[4]
limit = int(limit_arg) if limit_arg else None
lock_path = root / "mailbox.lock"
messages_path = root / "messages.jsonl"
acks_path = root / "acks.jsonl"
root.mkdir(parents=True, exist_ok=True)
lock_path.touch(exist_ok=True)

def load_jsonl(path):
    if not path.exists():
        return []
    with path.open("r", encoding="utf-8") as handle:
        return [json.loads(raw) for raw in handle if raw.strip()]

def render(row):
    lane = row.get("lane") or "-"
    return "\n".join([
        f"ID: {row['id']}",
        f"When: {row.get('posted_at', '-')}",
        f"From: {row.get('sender', '-')} @ {row.get('source', '-')}",
        f"Kind: {row.get('kind', '-')}",
        f"Lane: {lane}",
        "Message:",
        str(row.get("message", "")),
    ])

with lock_path.open("r+", encoding="utf-8") as lock_handle:
    fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX)
    messages = load_jsonl(messages_path)
    acked = {row["id"] for row in load_jsonl(acks_path) if row.get("target") == target}
    pending = [row for row in messages if row.get("target") == target and row.get("id") not in acked]
    if limit is not None:
        pending = pending[:limit]
    if not pending:
        print(f"No pending mailbox items for {target}")
        raise SystemExit(0)
    for idx, row in enumerate(pending, start=1):
        if idx > 1:
            print("\n---\n")
        print(render(row))
PY
        ;;

    next)
        if [[ $# -ne 1 ]]; then
            echo "Usage: tmux-mailbox.sh next <target>" >&2
            exit 1
        fi
        TARGET="$(resolve_target "$1")"
        mailbox_python "$TARGET" next <<'PY'
import fcntl
import json
import pathlib
import sys

root = pathlib.Path(sys.argv[1])
target = sys.argv[2]
lock_path = root / "mailbox.lock"
messages_path = root / "messages.jsonl"
acks_path = root / "acks.jsonl"
root.mkdir(parents=True, exist_ok=True)
lock_path.touch(exist_ok=True)

def load_jsonl(path):
    if not path.exists():
        return []
    with path.open("r", encoding="utf-8") as handle:
        return [json.loads(raw) for raw in handle if raw.strip()]

def render(row):
    lane = row.get("lane") or "-"
    return "\n".join([
        f"ID: {row['id']}",
        f"When: {row.get('posted_at', '-')}",
        f"From: {row.get('sender', '-')} @ {row.get('source', '-')}",
        f"Kind: {row.get('kind', '-')}",
        f"Lane: {lane}",
        "Message:",
        str(row.get("message", "")),
    ])

with lock_path.open("r+", encoding="utf-8") as lock_handle:
    fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX)
    messages = load_jsonl(messages_path)
    acked = {row["id"] for row in load_jsonl(acks_path) if row.get("target") == target}
    pending = [row for row in messages if row.get("target") == target and row.get("id") not in acked]
    if not pending:
        print(f"No pending mailbox items for {target}")
        raise SystemExit(3)
    print(render(pending[0]))
PY
        ;;

    done|ack)
        if [[ $# -ne 2 ]]; then
            usage_done
            exit 1
        fi
        TARGET="$(resolve_target "$1")"
        MESSAGE_ID="$2"
        ack_and_render_next "$TARGET" "$MESSAGE_ID"
        ;;

    reply)
        if [[ -z "$SENDER" ]]; then
            echo "Error: TMUX_MSG_SENDER is required for mailbox replies" >&2
            exit 1
        fi
        if [[ $# -lt 3 ]]; then
            usage_reply
            exit 1
        fi
        TARGET="$(resolve_target "$1")"
        MESSAGE_ID="$2"
        shift 2
        RESPONSE="$*"
        CURRENT_ROW="$(lookup_pending_message "$TARGET" "$MESSAGE_ID")"
        REPLY_TARGET="$(python3 -c 'import json,sys; row=json.loads(sys.stdin.read()); print(row.get("source", ""))' <<<"$CURRENT_ROW")"
        if [[ -z "$REPLY_TARGET" || "$REPLY_TARGET" == "unknown" ]]; then
            echo "Error: mailbox item $MESSAGE_ID has no valid reply target" >&2
            exit 1
        fi
        if [[ ! -x "$TMUX_MSG_SCRIPT" ]]; then
            echo "Error: tmux message script not found at $TMUX_MSG_SCRIPT" >&2
            exit 1
        fi
        TMUX_MSG_SENDER="$SENDER" TMUX_MSG_SOURCE="$SENDER_PANE" "$TMUX_MSG_SCRIPT" "$REPLY_TARGET" "MAILBOX REPLY id=$MESSAGE_ID: $RESPONSE"
        ack_and_render_next "$TARGET" "$MESSAGE_ID"
        ;;

    targets)
        mailbox_python "-" targets <<'PY'
import fcntl
import json
import pathlib
import sys

root = pathlib.Path(sys.argv[1])
lock_path = root / "mailbox.lock"
messages_path = root / "messages.jsonl"
acks_path = root / "acks.jsonl"
root.mkdir(parents=True, exist_ok=True)
lock_path.touch(exist_ok=True)

def load_jsonl(path):
    if not path.exists():
        return []
    with path.open("r", encoding="utf-8") as handle:
        return [json.loads(raw) for raw in handle if raw.strip()]

with lock_path.open("r+", encoding="utf-8") as lock_handle:
    fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX)
    messages = load_jsonl(messages_path)
    acks = load_jsonl(acks_path)
    acked = {(row.get("target"), row.get("id")) for row in acks}
    counts = {}
    for row in messages:
        key = row.get("target")
        if (key, row.get("id")) in acked:
            continue
        counts[key] = counts.get(key, 0) + 1
    if not counts:
        print("No pending mailbox items")
        raise SystemExit(0)
    for target in sorted(counts):
        print(f"{target}\t{counts[target]}")
PY
        ;;

    *)
        echo "Error: unknown command '$COMMAND'" >&2
        echo "Usage: tmux-mailbox.sh <post|count|list|next|done|reply|ack|targets> ..." >&2
        exit 1
        ;;
esac
