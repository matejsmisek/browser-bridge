#!/usr/bin/env bash
# Install the browser-bridge agent as a systemd user service on a desktop PC.
#
#   ./install-agent.sh [--hub http://server:7788] [--name NAME] [--reconfigure] [--uninstall]
#
#   --reconfigure  pick the browser profile again
#   --uninstall    stop and remove the service (config is kept)
set -euo pipefail

repo="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
conf="$HOME/.config/browser-bridge"
unit_dir="$HOME/.config/systemd/user"
unit=browser-bridge-agent.service
hub=""
name=""
keep_browser=(--keep-browser)

while [[ $# -gt 0 ]]; do
    case "$1" in
        --hub) hub="$2"; shift 2 ;;
        --name) name="$2"; shift 2 ;;
        --reconfigure) keep_browser=(); shift ;;
        --uninstall)
            systemctl --user disable --now "$unit" 2>/dev/null || true
            rm -f "$unit_dir/$unit"
            systemctl --user daemon-reload
            echo "Removed $unit. Config in $conf was kept."
            exit 0 ;;
        -h|--help) sed -n '2,7p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
        *) echo "unknown option: $1" >&2; exit 1 ;;
    esac
done

python=$(command -v /usr/bin/python3 || command -v python3) || { echo "python3 is required" >&2; exit 1; }
command -v systemctl >/dev/null || { echo "systemd is required (Linux only for now)" >&2; exit 1; }

args=(configure "${keep_browser[@]}")
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

sleep 2
if systemctl --user is-active --quiet "$unit"; then
    echo
    echo "Agent is running. Logs: journalctl --user -u $unit -f"
    echo "Try it: open the hub status page and press \"Send test\"."
else
    echo "Agent failed to start:" >&2
    journalctl --user -u "$unit" -n 20 --no-pager >&2
    exit 1
fi
