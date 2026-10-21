#!/usr/bin/env bash
# Install the browser-bridge agent as a user service on a desktop PC
# (systemd on Linux, launchd on macOS).
#
#   ./install-agent.sh [--hub http://server:7788] [--name NAME] [--reconfigure] [--uninstall]
#
#   --reconfigure  pick the browser profile again
#   --uninstall    stop and remove the service (config is kept)
set -euo pipefail

repo="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
conf="$HOME/.config/browser-bridge"
hub=""
name=""
keep_browser=(--keep-browser)

if [[ "$(uname)" == Darwin ]]; then
    macos=1
    label=browser-bridge-agent
    plist="$HOME/Library/LaunchAgents/$label.plist"
    log="$HOME/Library/Logs/$label.log"
    domain="gui/$(id -u)"
else
    macos=0
    unit_dir="$HOME/.config/systemd/user"
    unit=browser-bridge-agent.service
fi

while [[ $# -gt 0 ]]; do
    case "$1" in
        --hub) hub="$2"; shift 2 ;;
        --name) name="$2"; shift 2 ;;
        --reconfigure) keep_browser=(); shift ;;
        --uninstall)
            if (( macos )); then
                launchctl bootout "$domain/$label" 2>/dev/null || true
                rm -f "$plist"
                echo "Removed $label. Config in $conf was kept."
            else
                systemctl --user disable --now "$unit" 2>/dev/null || true
                rm -f "$unit_dir/$unit"
                systemctl --user daemon-reload
                echo "Removed $unit. Config in $conf was kept."
            fi
            exit 0 ;;
        -h|--help) sed -n '2,8p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
        *) echo "unknown option: $1" >&2; exit 1 ;;
    esac
done

python=$(command -v /usr/bin/python3 || command -v python3) || { echo "python3 is required" >&2; exit 1; }
if (( macos )); then
    launchctl print "$domain" >/dev/null 2>&1 || { echo "run this from a logged-in desktop session (no $domain)" >&2; exit 1; }
else
    command -v systemctl >/dev/null || { echo "systemd is required" >&2; exit 1; }
fi

# ${a[@]+...} keeps an empty array from tripping `set -u` on bash 3.2 (macOS /bin/bash).
args=(configure ${keep_browser[@]+"${keep_browser[@]}"})
[[ -n "$hub" ]] && args+=(--hub "$hub")
[[ -n "$name" ]] && args+=(--name "$name")

# Fetch the token over SSH when possible, so it never has to be pasted.
if [[ ! -s "$conf/token" && -n "$hub" ]]; then
    host=$("$python" -c 'import sys; from urllib.parse import urlsplit; print(urlsplit(sys.argv[1]).hostname or "")' "$hub")
    if token=$(ssh -o BatchMode=yes -o ConnectTimeout=5 "$host" cat .config/browser-bridge/token 2>/dev/null) && [[ -n "$token" ]]; then
        echo "Fetched token from $host over SSH"
        args+=(--token "$token")
    fi
fi

"$python" "$repo/bridge/agent.py" "${args[@]}"
"$python" "$repo/bridge/agent.py" ping || echo "warning: continuing, the agent keeps retrying in the background" >&2

if (( macos )); then
    mkdir -p "$(dirname "$plist")" "$(dirname "$log")"
    sed -e "s|@REPO@|$repo|g" -e "s|@PYTHON@|$python|g" -e "s|@LOG@|$log|g" \
        "$repo/launchd/$label.plist" > "$plist"
    launchctl bootout "$domain/$label" 2>/dev/null || true
    # bootout returns before the old job is gone; bootstrap fails while it lingers.
    while launchctl print "$domain/$label" >/dev/null 2>&1; do sleep 0.2; done
    launchctl bootstrap "$domain" "$plist"
else
    # Browsers need the graphical session's environment (DISPLAY / WAYLAND_DISPLAY).
    target=graphical-session.target
    if ! systemctl --user is-active --quiet "$target"; then
        target=default.target
        systemctl --user import-environment DISPLAY WAYLAND_DISPLAY XAUTHORITY DBUS_SESSION_BUS_ADDRESS XDG_RUNTIME_DIR 2>/dev/null || true
        echo "note: graphical-session.target is not active, using default.target with the current display environment" >&2
    fi

    mkdir -p "$unit_dir"
    sed -e "s|@REPO@|$repo|g" -e "s|@PYTHON@|$python|g" -e "s|@TARGET@|$target|g" "$repo/systemd/$unit" > "$unit_dir/$unit"
    systemctl --user daemon-reload
    systemctl --user enable "$unit" >/dev/null
    systemctl --user restart "$unit"
fi

sleep 2
if (( macos )); then
    [[ "$(launchctl print "$domain/$label" 2>/dev/null)" == *"state = running"* ]] && running=1 || running=0
    logs="tail -f $log"
else
    systemctl --user is-active --quiet "$unit" && running=1 || running=0
    logs="journalctl --user -u $unit -f"
fi
if (( running )); then
    echo
    echo "Agent is running. Logs: $logs"
    echo "Try it: open the hub status page and press \"Send test\"."
else
    echo "Agent failed to start:" >&2
    if (( macos )); then tail -n 20 "$log" >&2; else journalctl --user -u "$unit" -n 20 --no-pager >&2; fi
    exit 1
fi
