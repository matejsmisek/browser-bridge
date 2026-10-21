#!/usr/bin/env bash
# Install the browser-bridge hub as a user service on the headless host
# (systemd on Linux, launchd on macOS).
#
#   ./install-hub.sh [--port 7788] [--snow] [--uninstall]
#
#   --snow       point ~/.local/bin/snow at wrappers/snow (Snowflake CLI SSO via the bridge)
#   --uninstall  stop and remove the service (config and history are kept)
set -euo pipefail

repo="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
conf="$HOME/.config/browser-bridge"
port=7788
snow=0

if [[ "$(uname)" == Darwin ]]; then
    macos=1
    label=browser-bridge-hub
    plist="$HOME/Library/LaunchAgents/$label.plist"
    log="$HOME/Library/Logs/$label.log"
    domain="gui/$(id -u)"
else
    macos=0
    unit_dir="$HOME/.config/systemd/user"
    unit=browser-bridge-hub.service
fi

# The macOS app does not put its CLI on PATH unless asked to.
tailscale() {
    if type -P tailscale >/dev/null; then
        command tailscale "$@"
    elif [[ -x /Applications/Tailscale.app/Contents/MacOS/Tailscale ]]; then
        /Applications/Tailscale.app/Contents/MacOS/Tailscale "$@"
    else
        return 1
    fi
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --port) port="$2"; shift 2 ;;
        --snow) snow=1; shift ;;
        --uninstall)
            if (( macos )); then
                launchctl bootout "$domain/$label" 2>/dev/null || true
                rm -f "$plist" "$HOME/.local/bin/bb-open"
                echo "Removed $label. Config in $conf was kept."
            else
                systemctl --user disable --now "$unit" 2>/dev/null || true
                rm -f "$unit_dir/$unit" "$HOME/.local/bin/bb-open"
                systemctl --user daemon-reload
                echo "Removed $unit. Config in $conf was kept."
            fi
            exit 0 ;;
        -h|--help) sed -n '2,9p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
        *) echo "unknown option: $1" >&2; exit 1 ;;
    esac
done

python=$(command -v /usr/bin/python3 || command -v python3) || { echo "python3 is required" >&2; exit 1; }
if (( macos )); then
    # A LaunchAgent only comes back after reboot in the GUI domain; log in at the console
    # (or via Screen Sharing) once, then installing over SSH works.
    launchctl print "$domain" >/dev/null 2>&1 || { echo "no logged-in desktop session for $USER (no $domain)" >&2; exit 1; }
else
    command -v systemctl >/dev/null || { echo "systemd is required" >&2; exit 1; }
fi

mkdir -p "$conf" && chmod 700 "$conf"
if [[ ! -s "$conf/token" ]]; then
    (umask 077 && "$python" -c 'import secrets; print(secrets.token_urlsafe(32))' > "$conf/token")
    echo "Generated agent token in $conf/token"
fi

if [[ ! -f "$conf/hub.json" ]]; then
    ts_ip=$(tailscale ip -4 2>/dev/null | head -1 || true)
    listen='"127.0.0.1"'
    [[ -n "$ts_ip" ]] && listen="$listen, \"$ts_ip\""
    printf '{\n  "port": %s,\n  "listen": [%s]\n}\n' "$port" "$listen" > "$conf/hub.json"
    echo "Wrote $conf/hub.json"
    [[ -z "$ts_ip" ]] && echo "warning: no Tailscale IP found, the hub only listens on 127.0.0.1; add an address to $conf/hub.json" >&2
fi

mkdir -p "$HOME/.local/bin"
if (( macos )); then
    mkdir -p "$(dirname "$plist")" "$(dirname "$log")"
    sed -e "s|@REPO@|$repo|g" -e "s|@PYTHON@|$python|g" -e "s|@LOG@|$log|g" \
        "$repo/launchd/$label.plist" > "$plist"
    launchctl bootout "$domain/$label" 2>/dev/null || true
    # bootout returns before the old job is gone; bootstrap fails while it lingers.
    while launchctl print "$domain/$label" >/dev/null 2>&1; do sleep 0.2; done
    launchctl bootstrap "$domain" "$plist"
else
    mkdir -p "$unit_dir"
    sed -e "s|@REPO@|$repo|g" -e "s|@PYTHON@|$python|g" "$repo/systemd/$unit" > "$unit_dir/$unit"
    systemctl --user daemon-reload
    systemctl --user enable "$unit" >/dev/null
    systemctl --user restart "$unit"
fi
ln -sfn "$repo/bin/bb-open" "$HOME/.local/bin/bb-open"

if (( macos )); then
    echo "note: the hub runs while you are logged in. On a headless Mac, keep a session logged in (or enable automatic login)." >&2
elif [[ "$(loginctl show-user "$USER" -p Linger --value 2>/dev/null)" != yes ]]; then
    echo "warning: lingering is off, the hub stops when you log out. Fix: sudo loginctl enable-linger $USER" >&2
fi

if (( snow )); then
    ln -sfn "$repo/wrappers/snow" "$HOME/.local/bin/snow"
    echo "Linked ~/.local/bin/snow -> wrappers/snow"
fi

# Prefer the Tailscale machine name, which is what the PCs resolve.
host=$(tailscale status --json 2>/dev/null | "$python" -c 'import json,sys; print(json.load(sys.stdin)["Self"]["DNSName"].split(".")[0])' 2>/dev/null || hostname -s)

sleep 1
if (( macos )); then
    [[ "$(launchctl print "$domain/$label" 2>/dev/null)" == *"state = running"* ]] && running=1 || running=0
else
    systemctl --user is-active --quiet "$unit" && running=1 || running=0
fi
if (( running )); then
    addrs=$("$python" -c 'import json,sys; c=json.load(open(sys.argv[1])); print(" ".join("http://%s:%s/" % (a, c.get("port", 7788)) for a in c["listen"]))' "$conf/hub.json")
    echo
    echo "Hub is running. Status page: $addrs"
    echo "On each PC:   git clone <this repo> && cd browser-bridge && ./install-agent.sh --hub http://$host:$port"
    echo "Use it:       export BROWSER=$repo/bin/bb-open   (or run: bb-open <url>)"
else
    echo "Hub failed to start:" >&2
    if (( macos )); then tail -n 20 "$log" >&2; else journalctl --user -u "$unit" -n 20 --no-pager >&2; fi
    exit 1
fi
