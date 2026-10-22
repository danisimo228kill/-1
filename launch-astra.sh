#!/bin/sh
set -eu
appdir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
cd "$appdir"
export DISPLAY="${DISPLAY:-:0}"
export XAUTHORITY="${XAUTHORITY:-$HOME/.Xauthority}"
mkdir -p data
if [ "${1:-}" = "--foreground" ]; then
    exec /usr/bin/python3 "$appdir/main.py"
fi
export XDG_RUNTIME_DIR="${XDG_RUNTIME_DIR:-/run/user/$(id -u)}"
export DBUS_SESSION_BUS_ADDRESS="${DBUS_SESSION_BUS_ADDRESS:-unix:path=$XDG_RUNTIME_DIR/bus}"
if command -v systemd-run >/dev/null 2>&1 && systemctl --user show-environment >/dev/null 2>&1; then
    if systemctl --user is-active --quiet kontur-app.service; then
        printf 'Kontur is already running.\n'
        exit 0
    fi
    exec systemd-run --user --collect --unit=kontur-app \
        --property="WorkingDirectory=$appdir" \
        /usr/bin/env "DISPLAY=$DISPLAY" "XAUTHORITY=$XAUTHORITY" \
        "YANDEX_MAPS_API_KEY=${YANDEX_MAPS_API_KEY:-}" \
        /usr/bin/python3 "$appdir/main.py"
fi
if [ -f data/app.pid ]; then
    oldpid=$(cat data/app.pid)
    if kill -0 "$oldpid" 2>/dev/null && [ -r "/proc/$oldpid/cmdline" ]; then
        if tr '\000' '\n' < "/proc/$oldpid/cmdline" | grep -Fxq "$appdir/main.py"; then
            printf 'Kontur is already running (PID %s).\n' "$oldpid"
            exit 0
        fi
    fi
fi
nohup python3 "$appdir/main.py" > data/app.log 2>&1 < /dev/null &
pid=$!
printf '%s\n' "$pid" > data/app.pid
sleep 1
if ! kill -0 "$pid" 2>/dev/null; then
    cat data/app.log
    exit 1
fi
printf 'Kontur started (PID %s).\n' "$pid"
