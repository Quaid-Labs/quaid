#!/usr/bin/env bash
set -euo pipefail

usage() {
    cat <<'EOF'
Usage:
  openclaw-cli-safe.sh [--timeout <seconds>] [--label <name>] [--on-timeout <cmd>] -- <command ...args>

Examples:
  openclaw-cli-safe.sh --timeout 45 -- openclaw update --yes
  openclaw-cli-safe.sh --label agents-delete -- openclaw agents delete m13test --force
  openclaw-cli-safe.sh --on-timeout "ssh host 'pkill -f openclaw-update'" -- ssh host 'openclaw update --yes'
EOF
}

TIMEOUT_S="${OPENCLAW_CLI_TIMEOUT_S:-60}"
LABEL="openclaw-cli"
ON_TIMEOUT_CMD=""

while [[ $# -gt 0 ]]; do
    case "$1" in
        --timeout)
            TIMEOUT_S="${2:-}"
            shift 2
            ;;
        --label)
            LABEL="${2:-$LABEL}"
            shift 2
            ;;
        --on-timeout)
            ON_TIMEOUT_CMD="${2:-}"
            shift 2
            ;;
        --help|-h)
            usage
            exit 0
            ;;
        --)
            shift
            break
            ;;
        *)
            break
            ;;
    esac
done

if ! [[ "$TIMEOUT_S" =~ ^[0-9]+$ ]] || [[ "$TIMEOUT_S" -le 0 ]]; then
    echo "Invalid timeout: $TIMEOUT_S" >&2
    exit 2
fi

if [[ $# -lt 1 ]]; then
    usage >&2
    exit 2
fi

CMD=("$@")
TMP_OUT="$(mktemp -t openclaw-cli-safe.XXXXXX)"
DONE_TOKEN="__OPENCLAW_CLI_DONE__${RANDOM}${RANDOM}"

_cleanup_tmp() {
    rm -f "$TMP_OUT" 2>/dev/null || true
}
trap _cleanup_tmp EXIT

(
    set +e
    "${CMD[@]}"
    cmd_rc=$?
    printf '%s:%s\n' "$DONE_TOKEN" "$cmd_rc"
    exit 0
) >"$TMP_OUT" 2>&1 &

cmd_pid=$!
elapsed=0

_terminate_process_tree() {
    local pid="$1"
    local pgid
    pgid="$(ps -o pgid= -p "$pid" 2>/dev/null | tr -d ' ' || true)"

    if [[ -n "$pgid" ]]; then
        kill -TERM "-$pgid" 2>/dev/null || true
        sleep 1
        kill -KILL "-$pgid" 2>/dev/null || true
    fi
    kill -TERM "$pid" 2>/dev/null || true
    sleep 1
    kill -KILL "$pid" 2>/dev/null || true

    # Defensive cleanup for known detached OpenClaw worker helpers.
    pkill -f 'openclaw-update' 2>/dev/null || true
    pkill -f 'openclaw-completion' 2>/dev/null || true
    pkill -f 'openclaw-agent' 2>/dev/null || true
    pkill -f 'openclaw-agents' 2>/dev/null || true
}

while kill -0 "$cmd_pid" 2>/dev/null; do
    if (( elapsed >= TIMEOUT_S )); then
        _terminate_process_tree "$cmd_pid"
        wait "$cmd_pid" 2>/dev/null || true
        if [[ -n "$ON_TIMEOUT_CMD" ]]; then
            bash -lc "$ON_TIMEOUT_CMD" >/dev/null 2>&1 || true
        fi
        echo "timeout after ${TIMEOUT_S}s: ${LABEL}" >&2
        grep -v "^${DONE_TOKEN}:" "$TMP_OUT" | tail -n 12 | sed 's/^/  /' >&2 || true
        exit 124
    fi
    sleep 1
    elapsed=$((elapsed + 1))
done

wait "$cmd_pid" 2>/dev/null || true
done_line="$(grep "^${DONE_TOKEN}:" "$TMP_OUT" | tail -1 || true)"
clean_output="$(grep -v "^${DONE_TOKEN}:" "$TMP_OUT" || true)"

if [[ -n "$clean_output" ]]; then
    printf "%s\n" "$clean_output"
fi

if [[ -z "$done_line" ]]; then
    echo "command finished without completion sentinel: ${LABEL}" >&2
    exit 1
fi

cmd_rc="${done_line##*:}"
if ! [[ "$cmd_rc" =~ ^[0-9]+$ ]]; then
    echo "invalid completion sentinel payload: ${done_line}" >&2
    exit 1
fi

exit "$cmd_rc"
