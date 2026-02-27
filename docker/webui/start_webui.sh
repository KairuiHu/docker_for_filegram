#!/bin/bash
# HippoCamp WebUI Startup Script
#
# This script starts the WebUI server and sets up terminal sync.
#
# Usage:
#   start_webui             # Start WebUI in background (daemon mode)
#   start_webui 8080        # Start WebUI on custom port
#   start_webui --fg        # Start in foreground (for debugging)

# Use HIPPOCAMP_PORT env var or default to 8080
PORT=${HIPPOCAMP_PORT:-8080}
FOREGROUND=false
RUNTIME_DIR=/hippocamp/output/.webui
LOG_FILE=$RUNTIME_DIR/webui.log
PID_FILE=$RUNTIME_DIR/webui.pid
PIPE=$RUNTIME_DIR/hippocamp_commands
IS_TTY=1
if [ ! -t 1 ]; then
    IS_TTY=0
fi

emit_tty() {
    if [ "$IS_TTY" -eq 1 ]; then
        echo "$@"
    fi
}

emit_json() {
    if [ "$IS_TTY" -eq 0 ]; then
        echo "$1"
    fi
}

# Parse arguments
for arg in "$@"; do
    case $arg in
        --fg|--foreground)
            FOREGROUND=true
            ;;
        [0-9]*)
            PORT=$arg
            ;;
    esac
done

# If no env/arg provided, try runtime config
if [ -z "$HIPPOCAMP_PORT" ] && [ "$PORT" = "8080" ]; then
    CFG_PATH="${HIPPOCAMP_RUNTIME_CONFIG:-/hippocamp/runtime_config.py}"
    if [ -f "$CFG_PATH" ]; then
        CFG_PORT=$(python3 - <<PY
import os
cfg = {}
path = os.environ.get("HIPPOCAMP_RUNTIME_CONFIG", "/hippocamp/runtime_config.py")
try:
    with open(path, "r", encoding="utf-8") as f:
        code = f.read()
    exec(compile(code, path, "exec"), {}, cfg)
    port = cfg.get("PORT")
    if port:
        print(port)
except Exception:
    pass
PY
)
        if [ -n "$CFG_PORT" ]; then
            PORT="$CFG_PORT"
        fi
    fi
fi

# Check if already running
if [ -f "$PID_FILE" ]; then
    OLD_PID=$(cat "$PID_FILE")
    if kill -0 "$OLD_PID" 2>/dev/null; then
        emit_tty "WebUI is already running (PID: $OLD_PID)"
        emit_tty "Stop it first with: webui_stop"
        emit_json "{\"success\":false,\"data\":\"WebUI is already running\",\"pid\":\"$OLD_PID\",\"error\":\"already_running\"}"
        exit 1
    fi
fi

emit_tty "╔══════════════════════════════════════════════════════════════╗"
emit_tty "║                   Starting HippoCamp WebUI                   ║"
emit_tty "╚══════════════════════════════════════════════════════════════╝"

# Create FIFO pipe for terminal sync
mkdir -p "$RUNTIME_DIR"
if [ -p "$PIPE" ]; then
    rm "$PIPE"
fi
mkfifo "$PIPE" 2>/dev/null

# Export port for the app
export WEBUI_PORT=$PORT

cd /hippocamp/webui
PY_BIN="/opt/venv/bin/python3"
if [ ! -x "$PY_BIN" ]; then
    PY_BIN="python3"
fi

if [ "$FOREGROUND" = true ]; then
    # Foreground mode - for debugging
    emit_tty ""
    emit_tty "Starting WebUI in foreground mode..."
    emit_tty "Press Ctrl+C to stop."
    emit_tty ""
    "$PY_BIN" app.py
else
    # Background (daemon) mode - default
    "$PY_BIN" app.py > "$LOG_FILE" 2>&1 &
    WEBUI_PID=$!

    # Write PID file
    echo $WEBUI_PID > "$PID_FILE"

    # Wait a moment to check if it started successfully
    sleep 1
    if kill -0 "$WEBUI_PID" 2>/dev/null; then
        emit_tty ""
        emit_tty "WebUI started successfully!"
        emit_tty "  PID:    $WEBUI_PID"
        emit_tty "  URL:    http://localhost:$PORT"
        emit_tty "  Log:    $LOG_FILE"
        emit_tty ""
        emit_tty "Commands:"
        emit_tty "  webui_stop    - Stop the WebUI"
        emit_tty "  webui_status  - Check if running"
        emit_tty "  tail -f $LOG_FILE  - View logs"
        emit_tty ""
        emit_json "{\"success\":true,\"data\":\"WebUI started\",\"pid\":\"$WEBUI_PID\",\"url\":\"http://localhost:$PORT\",\"log\":\"$LOG_FILE\",\"error\":null}"
    else
        emit_tty "Failed to start WebUI. Check logs: $LOG_FILE"
        if [ "$IS_TTY" -eq 1 ]; then
            cat "$LOG_FILE"
        fi
        emit_json "{\"success\":false,\"data\":\"Failed to start WebUI\",\"log\":\"$LOG_FILE\",\"error\":\"startup_failed\"}"
        exit 1
    fi
fi
