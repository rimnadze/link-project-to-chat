#!/usr/bin/env bash
set -euo pipefail

PID_FILE="/tmp/link-project-to-chat-manager/pid"

if [ ! -f "$PID_FILE" ]; then
    echo "No PID file found, nothing to stop."
    exit 0
fi

PID=$(cat "$PID_FILE")

if ! kill -0 "$PID" 2>/dev/null; then
    echo "Process $PID not running, cleaning up PID file."
    rm -f "$PID_FILE"
    exit 0
fi

kill "$PID"
echo "Sent SIGTERM to process $PID."

for i in $(seq 1 10); do
    if ! kill -0 "$PID" 2>/dev/null; then
        break
    fi
    sleep 0.5
done

if kill -0 "$PID" 2>/dev/null; then
    kill -9 "$PID"
    echo "Process $PID force-killed."
fi

rm -f "$PID_FILE"
echo "Stopped."
