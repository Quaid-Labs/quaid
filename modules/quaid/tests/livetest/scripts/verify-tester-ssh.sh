#!/usr/bin/env bash
# verify-tester-ssh.sh — check that a tester's SUT pane is ssh'd to the VM
#
# Coordinator safeguard against the "tester pane silently drops to local zsh"
# contamination class. If pane.1 of the given tmux window has no ssh-to-VM
# child process, the tester's tmux send-keys would hit the LOCAL dev box.
#
# Usage:
#   verify-tester-ssh.sh <window>            # checks <window>.1
#   verify-tester-ssh.sh livetest:0          # CC SUT pane
#   verify-tester-ssh.sh livetest:1          # OC SUT pane
#   verify-tester-ssh.sh livetest:2          # CDX SUT pane
#   verify-tester-ssh.sh --all               # check all 3 livetest lanes
#
# Exit codes:
#   0  pane.1 is ssh-connected to the configured VM IP
#   1  pane.1 dropped to local (CONTAMINATION RISK)
#   2  usage error or pane not found
#
# Env:
#   VM_IP   VM IP to expect in ssh child (required)

set -euo pipefail

VM_IP="${VM_IP:-}"

usage() {
  echo "Usage: $0 <window> | --all" >&2
  echo "  Checks pane.1 of the given tmux window has an ssh-to-VM child" >&2
  echo "  Set VM_IP to the current run VM address." >&2
  exit 2
}

if [[ -z "$VM_IP" ]]; then
  echo "[verify-tester-ssh] FAIL: VM_IP is required" >&2
  usage
fi

check_pane() {
  local window="$1"
  local pane="${window}.1"

  if ! tmux has-session -t "$window" 2>/dev/null; then
    echo "[verify-tester-ssh] FAIL ${pane}: window does not exist" >&2
    return 1
  fi

  local pid
  pid=$(tmux display -t "$pane" -p '#{pane_pid}' 2>/dev/null) || {
    echo "[verify-tester-ssh] FAIL ${pane}: cannot read pane_pid" >&2
    return 1
  }

  # Walk children for an ssh process targeting the VM
  local child_pids
  child_pids=$(pgrep -P "$pid" 2>/dev/null || true)
  if [[ -z "$child_pids" ]]; then
    echo "[verify-tester-ssh] FAIL ${pane}: no child processes — likely local zsh idle" >&2
    return 1
  fi

  local found=0
  while IFS= read -r child; do
    [[ -z "$child" ]] && continue
    local cmd
    cmd=$(ps -p "$child" -o command= 2>/dev/null || true)
    if echo "$cmd" | grep -qE "ssh\b.*admin@${VM_IP}\b"; then
      found=1
      echo "[verify-tester-ssh] OK   ${pane}: ssh admin@${VM_IP} (child pid=${child})"
      break
    fi
  done <<< "$child_pids"

  if [[ "$found" -eq 0 ]]; then
    echo "[verify-tester-ssh] FAIL ${pane}: no ssh-to-${VM_IP} child found — pane has dropped local; tester sends will contaminate dev box" >&2
    return 1
  fi
  return 0
}

if [[ $# -lt 1 ]]; then
  usage
fi

if [[ "$1" == "--all" ]]; then
  rc=0
  for w in livetest:0 livetest:1 livetest:2; do
    check_pane "$w" || rc=1
  done
  exit "$rc"
fi

check_pane "$1"
