#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LPTC="$SCRIPT_DIR/../.venv/bin/link-project-to-chat"
RUN_DIR="/tmp/link-project-to-chat-manager"
PID_FILE="$RUN_DIR/pid"
LOG_FILE="$RUN_DIR/log"

mkdir -p "$RUN_DIR"

nohup bash -c "
    sleep 2
    '$SCRIPT_DIR/stop.sh'
    nohup '$LPTC' start-manager >> '$LOG_FILE' 2>&1 &
    echo \$! > '$PID_FILE'
" > /dev/null 2>&1 &
disown

echo "Restart scheduled."
