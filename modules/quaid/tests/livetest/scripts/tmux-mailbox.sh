#!/bin/bash
# tmux-mailbox.sh — Queue-backed mailbox for tmux-coordinated agents
#
# Usage:
#   tmux-mailbox.sh post [--kind KIND] [--lane LANE] [--notify 0|1] <target> <message>
#   tmux-mailbox.sh count <target>
#   tmux-mailbox.sh list [--limit N] <target>
#   tmux-mailbox.sh status <target>
#   tmux-mailbox.sh next <target>
#   tmux-mailbox.sh done <target> <message-id>
#   tmux-mailbox.sh reply <target> <message-id> <response>
#   tmux-mailbox.sh ack <target> <message-id>
#   tmux-mailbox.sh watch [--interval SEC] [--stale-seconds SEC] [target...]
#   tmux-mailbox.sh start-watch [--interval SEC] [--stale-seconds SEC] [target...]
#   tmux-mailbox.sh stop-watch
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
    echo "Usage: tmux-mailbox.sh <post|count|list|status|next|done|reply|ack|watch|start-watch|stop-watch|targets> ..." >&2
    exit 1
fi

shift || true

mkdir -p "$MAILBOX_ROOT"
WATCHER_PID_FILE="$MAILBOX_ROOT/watch.pid"
WATCHER_LOG_FILE="$MAILBOX_ROOT/watch.log"
TEST_PRE_STALE_CHECK_SLEEP_MS="${TMUX_MAILBOX_TEST_PRE_STALE_CHECK_SLEEP_MS:-0}"
TEST_WATCH_SINGLE_PASS="${TMUX_MAILBOX_TEST_WATCH_SINGLE_PASS:-0}"

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

usage_watch() {
    echo "Usage: tmux-mailbox.sh watch [--interval SEC] [--stale-seconds SEC] [target...]" >&2
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

format_stale_notify_message() {
    local target="$1"
    local message_id="$2"
    local age_seconds="$3"
    local pending_count="$4"
    local age_minutes=$(( age_seconds / 60 ))
    printf 'MAILBOX STALLED target=%s pending=%s oldest_id=%s oldest_age_min=%s. You have uncleared mailbox. Do not tail mailbox files. Use: tests/livetest/scripts/tmux-mailbox.sh next \"%s\" then tests/livetest/scripts/tmux-mailbox.sh done \"%s\" %s or tests/livetest/scripts/tmux-mailbox.sh reply \"%s\" %s \"<response>\"' \
        "$target" "$pending_count" "$message_id" "$age_minutes" "$target" "$target" "$message_id" "$target" "$message_id"
}

mark_notified() {
    local target="$1"
    local message_id="$2"
    mailbox_python "$target" mark-notified "$message_id" <<'PY'
import fcntl
import json
import pathlib
import sys
import time

root = pathlib.Path(sys.argv[1])
target = sys.argv[2]
op = sys.argv[3]
message_id = sys.argv[4]
lock_path = root / "mailbox.lock"
state_path = root / "notify-state.json"
root.mkdir(parents=True, exist_ok=True)
lock_path.touch(exist_ok=True)

def load_state(path):
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}

with lock_path.open("r+", encoding="utf-8") as lock_handle:
    fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX)
    state = load_state(state_path)
    target_state = state.get(target)
    if not isinstance(target_state, dict):
        target_state = {}
    target_state["needs_nudge"] = False
    target_state["last_notified_id"] = message_id
    target_state["last_notified_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    state[target] = target_state
    state_path.write_text(json.dumps(state, ensure_ascii=True, indent=2, sort_keys=True) + "\n", encoding="utf-8")
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
state_path = root / "notify-state.json"
root.mkdir(parents=True, exist_ok=True)
lock_path.touch(exist_ok=True)

def load_jsonl(path):
    if not path.exists():
        return []
    with path.open("r", encoding="utf-8") as handle:
        return [json.loads(raw) for raw in handle if raw.strip()]

def load_state(path):
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}

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
state_path = root / "notify-state.json"
root.mkdir(parents=True, exist_ok=True)
lock_path.touch(exist_ok=True)

def load_jsonl(path):
    if not path.exists():
        return []
    with path.open("r", encoding="utf-8") as handle:
        return [json.loads(raw) for raw in handle if raw.strip()]

def load_state(path):
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}

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
    state = load_state(state_path)
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
    target_state = state.get(target)
    if not isinstance(target_state, dict):
        target_state = {}
    if pending:
        target_state["pending_head_id"] = pending[0]["id"]
    else:
        target_state["needs_nudge"] = False
        target_state["pending_head_id"] = ""
    state[target] = target_state
    state_path.write_text(json.dumps(state, ensure_ascii=True, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    print(f"Acknowledged {message_id} for {target}")
    if not pending:
        raise SystemExit(0)
    print("")
    print(render(pending[0]))
PY
}

mailbox_status() {
    local target="$1"
    mailbox_python "$target" status <<'PY'
import fcntl
import json
import pathlib
import sys
import time

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

def parse_iso8601(ts: str) -> float:
    if not ts:
        return 0.0
    try:
        return time.mktime(time.strptime(ts, "%Y-%m-%dT%H:%M:%SZ"))
    except Exception:
        return 0.0

with lock_path.open("r+", encoding="utf-8") as lock_handle:
    fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX)
    messages = load_jsonl(messages_path)
    acked = {row["id"] for row in load_jsonl(acks_path) if row.get("target") == target}
    pending = [row for row in messages if row.get("target") == target and row.get("id") not in acked]
    if not pending:
        print(json.dumps({"target": target, "pending": 0}))
        raise SystemExit(0)
    oldest = pending[0]
    oldest_epoch = parse_iso8601(str(oldest.get("posted_at") or ""))
    age_seconds = max(0, int(time.time() - oldest_epoch)) if oldest_epoch else 0
    print(json.dumps({
        "target": target,
        "pending": len(pending),
        "head_id": oldest.get("id", ""),
        "head_kind": oldest.get("kind", ""),
        "head_lane": oldest.get("lane", ""),
        "head_posted_at": oldest.get("posted_at", ""),
        "head_age_seconds": age_seconds,
    }))
PY
}

watch_mailbox_loop() {
    local interval="${1:-60}"
    local stale_seconds="${2:-600}"
    shift 2 || true
    local explicit_targets=("$@")
    while true; do
        local targets_output
        if [[ ${#explicit_targets[@]} -gt 0 ]]; then
            targets_output="$(printf '%s\n' "${explicit_targets[@]}")"
        else
            targets_output="$("$0" targets 2>/dev/null | awk '{print $1}')"
        fi
        while IFS= read -r raw_target; do
            [[ -z "$raw_target" ]] && continue
            local target
            if ! target="$(resolve_target "$raw_target" 2>/dev/null)"; then
                continue
            fi
            local status_json
            if ! status_json="$(mailbox_status "$target" 2>/dev/null)"; then
                continue
            fi
            local pending head_id age_seconds
            pending="$(python3 -c 'import json,sys; data=json.loads(sys.stdin.read()); print(data.get("pending", 0))' <<<"$status_json" 2>/dev/null || echo 0)"
            if [[ "$pending" == "0" ]]; then
                continue
            fi
            head_id="$(python3 -c 'import json,sys; data=json.loads(sys.stdin.read()); print(data.get("head_id", ""))' <<<"$status_json" 2>/dev/null || true)"
            age_seconds="$(python3 -c 'import json,sys; data=json.loads(sys.stdin.read()); print(data.get("head_age_seconds", 0))' <<<"$status_json" 2>/dev/null || echo 0)"
            if [[ -z "$head_id" || "$age_seconds" -lt "$stale_seconds" ]]; then
                continue
            fi
            if [[ "$TEST_PRE_STALE_CHECK_SLEEP_MS" =~ ^[0-9]+$ ]] && [[ "$TEST_PRE_STALE_CHECK_SLEEP_MS" -gt 0 ]]; then
                python3 - "$TEST_PRE_STALE_CHECK_SLEEP_MS" <<'PY'
import sys
import time

time.sleep(max(0.0, int(sys.argv[1]) / 1000.0))
PY
            fi
            local stale_result
            stale_result="$(mailbox_python "$target" stale-check "$head_id" "$age_seconds" <<'PY'
import fcntl
import json
import pathlib
import sys
import time

root = pathlib.Path(sys.argv[1])
target = sys.argv[2]
op = sys.argv[3]
head_id = sys.argv[4]
age_seconds = int(sys.argv[5])
lock_path = root / "mailbox.lock"
messages_path = root / "messages.jsonl"
acks_path = root / "acks.jsonl"
state_path = root / "notify-state.json"
root.mkdir(parents=True, exist_ok=True)
lock_path.touch(exist_ok=True)

def load_jsonl(path):
    if not path.exists():
        return []
    with path.open("r", encoding="utf-8") as handle:
        return [json.loads(raw) for raw in handle if raw.strip()]

def load_state(path):
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}

with lock_path.open("r+", encoding="utf-8") as lock_handle:
    fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX)
    messages = load_jsonl(messages_path)
    acked = {
        row["id"]
        for row in load_jsonl(acks_path)
        if row.get("target") == target and row.get("id")
    }
    pending = [
        row for row in messages
        if row.get("target") == target and row.get("id") not in acked
    ]
    if not pending or str(pending[0].get("id", "")) != head_id:
        print(json.dumps({"should_nudge": False}))
        raise SystemExit(0)
    state = load_state(state_path)
    target_state = state.get(target)
    if not isinstance(target_state, dict):
        target_state = {}
    last_stale_id = str(target_state.get("last_stale_nudge_id", "") or "")
    last_stale_at = str(target_state.get("last_stale_nudged_at", "") or "")
    now = time.time()
    stale_epoch = 0.0
    if last_stale_at:
        try:
            stale_epoch = time.mktime(time.strptime(last_stale_at, "%Y-%m-%dT%H:%M:%SZ"))
        except Exception:
            stale_epoch = 0.0
    should_nudge = not (last_stale_id == head_id and stale_epoch and (now - stale_epoch) < age_seconds)
    if should_nudge:
        target_state["last_stale_nudge_id"] = head_id
        target_state["last_stale_nudged_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now))
        state[target] = target_state
        state_path.write_text(json.dumps(state, ensure_ascii=True, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps({"should_nudge": should_nudge}))
PY
)"
            local should_nudge
            should_nudge="$(python3 -c 'import json,sys; data=json.loads(sys.stdin.read()); print("1" if data.get("should_nudge") else "0")' <<<"$stale_result" 2>/dev/null || echo 0)"
            if [[ "$should_nudge" != "1" ]]; then
                continue
            fi
            if [[ -x "$TMUX_MSG_SCRIPT" ]]; then
                local notice
                notice="$(format_stale_notify_message "$target" "$head_id" "$age_seconds" "$pending")"
                TMUX_MSG_SENDER="${TMUX_MSG_SENDER:-mailbox-watcher}" \
                TMUX_MSG_SOURCE="${TMUX_MSG_SOURCE:-mailbox-watch}" \
                    "$TMUX_MSG_SCRIPT" "$target" "$notice" >/dev/null 2>&1 || true
            fi
        done <<<"$targets_output"
        if [[ "$TEST_WATCH_SINGLE_PASS" == "1" ]]; then
            break
        fi
        sleep "$interval"
    done
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
state_path = root / "notify-state.json"

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

def load_state(path):
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}

with lock_path.open("r+", encoding="utf-8") as lock_handle:
    fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX)
    messages = load_jsonl(messages_path)
    acks = load_jsonl(acks_path)
    state = load_state(state_path)
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
    target_state = state.get(target)
    if not isinstance(target_state, dict):
        target_state = {}
    if not pending_before:
        target_state["needs_nudge"] = True
    else:
        target_state["needs_nudge"] = bool(target_state.get("needs_nudge", False))
    target_state["pending_head_id"] = pending_after[0]["id"] if pending_after else ""
    state[target] = target_state
    state_path.write_text(json.dumps(state, ensure_ascii=True, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps({
        "record": record,
        "pending_before": len(pending_before),
        "pending_after": len(pending_after),
        "should_notify": bool(target_state.get("needs_nudge", False)),
        "notify_candidate": pending_after[0] if pending_after else record,
    }))
PY
)"

        MESSAGE_ID="$(python3 -c 'import json,sys; data=json.loads(sys.stdin.read()); print(data["record"]["id"])' <<<"$RESULT")"
        PENDING_BEFORE="$(python3 -c 'import json,sys; data=json.loads(sys.stdin.read()); print(data["pending_before"])' <<<"$RESULT")"
        PENDING_AFTER="$(python3 -c 'import json,sys; data=json.loads(sys.stdin.read()); print(data["pending_after"])' <<<"$RESULT")"
        SHOULD_NOTIFY="$(python3 -c 'import json,sys; data=json.loads(sys.stdin.read()); print("1" if data.get("should_notify") else "0")' <<<"$RESULT")"
        NOTIFY_ID="$(python3 -c 'import json,sys; data=json.loads(sys.stdin.read()); print(data["notify_candidate"].get("id", ""))' <<<"$RESULT")"
        NOTIFY_KIND="$(python3 -c 'import json,sys; data=json.loads(sys.stdin.read()); print(data["notify_candidate"].get("kind", ""))' <<<"$RESULT")"
        NOTIFY_LANE="$(python3 -c 'import json,sys; data=json.loads(sys.stdin.read()); print(data["notify_candidate"].get("lane", ""))' <<<"$RESULT")"
        NOTIFY_SENDER="$(python3 -c 'import json,sys; data=json.loads(sys.stdin.read()); print(data["notify_candidate"].get("sender", ""))' <<<"$RESULT")"
        NOTIFY_MESSAGE="$(python3 -c 'import json,sys; data=json.loads(sys.stdin.read()); print(data["notify_candidate"].get("message", ""))' <<<"$RESULT")"

        if [[ "$NOTIFY" != "0" ]] && [[ "$SHOULD_NOTIFY" == "1" ]] && [[ -x "$TMUX_MSG_SCRIPT" ]]; then
            NOTIFY_TEXT="$(format_notify_message "$NOTIFY_ID" "$NOTIFY_KIND" "$NOTIFY_LANE" "$NOTIFY_SENDER" "$NOTIFY_MESSAGE")"
            if TMUX_MSG_SENDER="$SENDER" TMUX_MSG_SOURCE="$SENDER_PANE" "$TMUX_MSG_SCRIPT" "$TARGET" "$NOTIFY_TEXT" >/dev/null 2>&1; then
                mark_notified "$TARGET" "$NOTIFY_ID"
            fi
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

    status)
        if [[ $# -ne 1 ]]; then
            echo "Usage: tmux-mailbox.sh status <target>" >&2
            exit 1
        fi
        TARGET="$(resolve_target "$1")"
        STATUS_JSON="$(mailbox_status "$TARGET")"
        STATUS_JSON="$STATUS_JSON" python3 - "$TARGET" <<'PY'
import json
import os
import sys

target = sys.argv[1]
data = json.loads(os.environ.get("STATUS_JSON", "{}") or "{}")
pending = int(data.get("pending", 0) or 0)
if pending <= 0:
    print(f"No pending mailbox items for {target}")
    raise SystemExit(0)
print(f"Target: {target}")
print(f"Pending: {pending}")
print(f"Head ID: {data.get('head_id', '-')}")
print(f"Head Kind: {data.get('head_kind', '-')}")
print(f"Head Lane: {data.get('head_lane', '-') or '-'}")
print(f"Head When: {data.get('head_posted_at', '-')}")
print(f"Head Age Seconds: {data.get('head_age_seconds', 0)}")
print()
print(f"Read: tests/livetest/scripts/tmux-mailbox.sh next \"{target}\"")
print(f"Done: tests/livetest/scripts/tmux-mailbox.sh done \"{target}\" {data.get('head_id', '<id>')}")
print(f"Reply: tests/livetest/scripts/tmux-mailbox.sh reply \"{target}\" {data.get('head_id', '<id>')} \"<response>\"")
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

    watch)
        INTERVAL="60"
        STALE_SECONDS="600"
        while [[ $# -gt 0 ]]; do
            case "$1" in
                --interval)
                    INTERVAL="${2:-}"
                    shift 2
                    ;;
                --stale-seconds)
                    STALE_SECONDS="${2:-}"
                    shift 2
                    ;;
                --help|-h)
                    usage_watch
                    exit 0
                    ;;
                --*)
                    echo "Error: unknown option '$1'" >&2
                    usage_watch
                    exit 1
                    ;;
                *)
                    break
                    ;;
            esac
        done
        watch_mailbox_loop "$INTERVAL" "$STALE_SECONDS" "$@"
        ;;

    start-watch)
        INTERVAL="60"
        STALE_SECONDS="600"
        while [[ $# -gt 0 ]]; do
            case "$1" in
                --interval)
                    INTERVAL="${2:-}"
                    shift 2
                    ;;
                --stale-seconds)
                    STALE_SECONDS="${2:-}"
                    shift 2
                    ;;
                --help|-h)
                    usage_watch
                    exit 0
                    ;;
                --*)
                    echo "Error: unknown option '$1'" >&2
                    usage_watch
                    exit 1
                    ;;
                *)
                    break
                    ;;
            esac
        done
        if [[ -f "$WATCHER_PID_FILE" ]]; then
            EXISTING_PID="$(cat "$WATCHER_PID_FILE" 2>/dev/null || true)"
            if [[ -n "$EXISTING_PID" ]] && kill -0 "$EXISTING_PID" 2>/dev/null; then
                echo "Mailbox watcher already running: pid=$EXISTING_PID"
                exit 0
            fi
            rm -f "$WATCHER_PID_FILE"
        fi
        nohup "$0" watch --interval "$INTERVAL" --stale-seconds "$STALE_SECONDS" "$@" >"$WATCHER_LOG_FILE" 2>&1 &
        WATCH_PID=$!
        printf '%s\n' "$WATCH_PID" >"$WATCHER_PID_FILE"
        echo "Mailbox watcher started: pid=$WATCH_PID log=$WATCHER_LOG_FILE"
        ;;

    stop-watch)
        if [[ ! -f "$WATCHER_PID_FILE" ]]; then
            echo "Mailbox watcher not running"
            exit 0
        fi
        WATCH_PID="$(cat "$WATCHER_PID_FILE" 2>/dev/null || true)"
        if [[ -n "$WATCH_PID" ]] && kill -0 "$WATCH_PID" 2>/dev/null; then
            kill "$WATCH_PID"
            echo "Mailbox watcher stopped: pid=$WATCH_PID"
        else
            echo "Mailbox watcher pid file was stale"
        fi
        rm -f "$WATCHER_PID_FILE"
        ;;

    *)
        echo "Error: unknown command '$COMMAND'" >&2
        echo "Usage: tmux-mailbox.sh <post|count|list|status|next|done|reply|ack|watch|start-watch|stop-watch|targets> ..." >&2
        exit 1
        ;;
esac
