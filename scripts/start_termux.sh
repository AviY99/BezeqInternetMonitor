#!/data/data/com.termux/files/usr/bin/bash

HOME_DIR="$HOME"
MONITOR="$HOME_DIR/monitor_v5.py"
DASHBOARD="$HOME_DIR/dashboard_v5_rooms.py"
MONITOR_LOG="$HOME_DIR/monitor_v5.log"
DASHBOARD_LOG="$HOME_DIR/dashboard_v5_rooms.log"
URL="http://127.0.0.1:8080"

termux-wake-lock >/dev/null 2>&1 || true

pkill -f "dashboard_v5_rooms.py" 2>/dev/null || true

if ! pgrep -f "python .*monitor_v5.py" >/dev/null 2>&1; then
    nohup python "$MONITOR" >"$MONITOR_LOG" 2>&1 &
fi

if ! pgrep -f "python .*dashboard_v5_rooms.py" >/dev/null 2>&1; then
    nohup python "$DASHBOARD" >"$DASHBOARD_LOG" 2>&1 &
fi

sleep 2
am start -a android.intent.action.VIEW -d "$URL" >/dev/null 2>&1
