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

if [ -f "$PID_FILE" ]; then
    PID=$(cat "$PID_FILE")
    if [ -n "$PID" ]; then
        kill "$PID" 2>/dev/null
        rm -f "$PID_FILE"
        emit_tty "WebUI stopped"
        if [ "$IS_TTY" -eq 0 ]; then
            echo "{\"success\":true,\"data\":\"WebUI stopped\",\"error\":null}"
        fi
        exit 0
    fi
fi

emit_tty "WebUI not running"
if [ "$IS_TTY" -eq 0 ]; then
    echo "{\"success\":false,\"data\":\"WebUI not running\",\"error\":\"WebUI not running\"}"
fi
exit 1
