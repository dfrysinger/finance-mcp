#!/usr/bin/env bash
#
# Restart the finance-mcp web UI so it loads the latest code.
#
# The web UI is a long-running process that reads its Python source once at
# startup, so a code change (UI, query logic, server wiring) only takes effect
# after the process is recycled. Data changes — new syncs, categorization rules —
# apply on the next page load with no restart; this script is only needed after
# editing code.
#
# Usage:
#   scripts/dev-web.sh [extra finance-mcp web args...]
#
# Any extra arguments are passed straight through to `finance-mcp web`, e.g. to
# expose the UI on a private hostname:
#   scripts/dev-web.sh --allow-host my-machine.example.ts.net
#
# Overridable via environment:
#   FMCP_WEB_HOST  (default 127.0.0.1)
#   FMCP_WEB_PORT  (default 8770)
#   FMCP_WEB_LOG   (default /tmp/fmcp-web.log)
set -euo pipefail

HOST="${FMCP_WEB_HOST:-127.0.0.1}"
PORT="${FMCP_WEB_PORT:-8770}"
LOG="${FMCP_WEB_LOG:-/tmp/fmcp-web.log}"
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

# Stop whatever currently owns the port (the previous server). Scoping the kill
# to the listening socket avoids guessing at process names and never touches an
# unrelated finance-mcp process (e.g. an MCP stdio server).
existing="$(lsof -nP -iTCP:"$PORT" -sTCP:LISTEN -t 2>/dev/null || true)"
if [ -n "$existing" ]; then
  echo "Stopping server on :$PORT (pid $(echo "$existing" | tr '\n' ' '))"
  # shellcheck disable=SC2086 # word-splitting is intended for multiple pids
  kill $existing 2>/dev/null || true
  freed=""
  for _ in $(seq 1 20); do
    if [ -z "$(lsof -nP -iTCP:"$PORT" -sTCP:LISTEN -t 2>/dev/null || true)" ]; then
      freed=1
      break
    fi
    sleep 0.5
  done
  # Escalate to SIGKILL for anything still holding the port, so a wedged server
  # that ignores SIGTERM still gets recycled instead of blocking every restart.
  if [ -z "$freed" ]; then
    stuck="$(lsof -nP -iTCP:"$PORT" -sTCP:LISTEN -t 2>/dev/null || true)"
    if [ -n "$stuck" ]; then
      echo "Server did not stop on SIGTERM; forcing (pid $(echo "$stuck" | tr '\n' ' '))"
      # shellcheck disable=SC2086 # word-splitting is intended for multiple pids
      kill -KILL $stuck 2>/dev/null || true
      for _ in $(seq 1 20); do
        if [ -z "$(lsof -nP -iTCP:"$PORT" -sTCP:LISTEN -t 2>/dev/null || true)" ]; then
          freed=1
          break
        fi
        sleep 0.5
      done
    fi
  fi
  # Abort only if the port is genuinely still held. Re-check directly rather
  # than trusting $freed: the listener may have exited in the window between a
  # wait loop's last poll and here, which would otherwise abort a free port.
  if [ -n "$(lsof -nP -iTCP:"$PORT" -sTCP:LISTEN -t 2>/dev/null || true)" ]; then
    echo "Port :$PORT still in use after stop attempt; aborting." >&2
    exit 1
  fi
fi

cd "$REPO_ROOT"
echo "Starting finance-mcp web on $HOST:$PORT (log: $LOG)"
# nohup + setsid (when available) so the server survives this script exiting.
if command -v setsid >/dev/null 2>&1; then
  PYTHONPATH=src setsid nohup uv run finance-mcp web \
    --host "$HOST" --port "$PORT" "$@" >"$LOG" 2>&1 &
else
  PYTHONPATH=src nohup uv run finance-mcp web \
    --host "$HOST" --port "$PORT" "$@" >"$LOG" 2>&1 &
fi
disown 2>/dev/null || true

# Wait for the server to answer before reporting success, so a startup failure
# (port bound, import error) surfaces here instead of as a blank page later.
for _ in $(seq 1 30); do
  if curl -s -o /dev/null --max-time 2 "http://$HOST:$PORT/"; then
    echo "Server is up: http://$HOST:$PORT/"
    exit 0
  fi
  sleep 0.5
done

echo "Server did not come up within the timeout; last log lines:" >&2
tail -n 20 "$LOG" >&2 || true
exit 1
