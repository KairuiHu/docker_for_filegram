#!/bin/bash

PID_FILE="/hippocamp/output/.webui/webui.pid"
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

if [ -f "$PID_FILE" ]; then
    PID=$(cat "$PID_FILE")
    if [ -n "$PID" ] && kill -0 "$PID" 2>/dev/null; then
        emit_tty "WebUI is running"
        emit_json "{\"success\":true,\"data\":\"WebUI is running\",\"pid\":\"$PID\",\"error\":null}"
        exit 0
    fi
fi

emit_tty "WebUI is not running"
emit_json "{\"success\":false,\"data\":\"WebUI is not running\",\"error\":\"not_running\"}"
exit 1
