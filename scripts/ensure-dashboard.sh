#!/usr/bin/env bash
# Idempotently ensure the claude-usage-monitor dashboard is running.
#
# Designed to be called from a Claude Code SessionStart hook (async). If the
# dashboard is already serving, it exits immediately; otherwise it refreshes the
# transcript index and starts the Streamlit server detached, so a single server
# is shared across all Claude sessions.
#
# Env overrides:
#   CLAUDE_USAGE_DASHBOARD_PORT  (default 8501)
#   CLAUDE_USAGE_MONITOR_DIR     (default $HOME/claude-usage-monitor)
set -uo pipefail

PORT="${CLAUDE_USAGE_DASHBOARD_PORT:-8501}"
REPO="${CLAUDE_USAGE_MONITOR_DIR:-$HOME/claude-usage-monitor}"
LOG="$HOME/.claude/usage-dashboard.log"
HEALTH="http://127.0.0.1:${PORT}/_stcore/health"

# Already running? Nothing to do (this is what keeps it to one server).
if curl -fsS --max-time 2 "$HEALTH" >/dev/null 2>&1; then
  exit 0
fi

if [ ! -d "$REPO" ]; then
  echo "$(date -Is) ensure-dashboard: repo not found at $REPO" >>"$LOG"
  exit 0
fi

# Start fully detached (new session) so it outlives this short-lived hook.
setsid bash -c "
  cd '$REPO' || exit 0
  uv run --extra dashboard claude-usage-index >>'$LOG' 2>&1 || true
  exec uv run --extra dashboard streamlit run src/claude_usage_monitor/dashboard/app.py \
    --server.headless true \
    --server.port '$PORT' \
    --server.address 127.0.0.1 \
    --browser.gatherUsageStats false
" >>"$LOG" 2>&1 </dev/null &
disown 2>/dev/null || true

echo "$(date -Is) ensure-dashboard: starting dashboard on http://127.0.0.1:$PORT" >>"$LOG"
exit 0
