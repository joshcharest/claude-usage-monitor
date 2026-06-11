#!/usr/bin/env bash
# Gracefully stop the claude-usage-monitor dashboard server.
#
# Sends SIGTERM to the Streamlit process(es) running the dashboard app, waits
# briefly, then SIGKILLs anything still alive. Safe to run when nothing is up.
set -uo pipefail

# Match the server whether launched with a relative or absolute app path; both
# command lines contain this package-relative tail.
APP="claude_usage_monitor/dashboard/app.py"

# Find matching PIDs, excluding any in this script's own process group (drops a
# caller shell that merely mentions the path). The detached server runs in its
# own session, so it is never in our group.
MY_PGID="$(ps -o pgid= -p $$ | tr -d ' ')"
find_pids() {
  local out=() pid pgid
  while read -r pid; do
    [ -z "$pid" ] && continue
    pgid="$(ps -o pgid= -p "$pid" 2>/dev/null | tr -d ' ')"
    [ -z "$pgid" ] && continue
    [ "$pgid" = "$MY_PGID" ] && continue
    out+=("$pid")
  done < <(pgrep -f "$APP" 2>/dev/null || true)
  printf '%s\n' "${out[@]}"
}

mapfile -t PIDS < <(find_pids)
TMP=(); for p in "${PIDS[@]}"; do [ -n "$p" ] && TMP+=("$p"); done; PIDS=("${TMP[@]}")
if [ "${#PIDS[@]}" -eq 0 ]; then
  echo "dashboard not running"
  exit 0
fi

kill -TERM "${PIDS[@]}" 2>/dev/null || true

# Wait up to ~5s for a graceful exit.
for _ in $(seq 1 20); do
  mapfile -t PIDS < <(find_pids)
  TMP=(); for p in "${PIDS[@]}"; do [ -n "$p" ] && TMP+=("$p"); done; PIDS=("${TMP[@]}")
  [ "${#PIDS[@]}" -eq 0 ] && { echo "dashboard stopped"; exit 0; }
  sleep 0.25
done

# Force-kill stragglers.
kill -KILL "${PIDS[@]}" 2>/dev/null || true
echo "dashboard stopped (forced)"
exit 0
