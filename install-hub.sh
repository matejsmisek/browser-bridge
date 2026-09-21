#!/usr/bin/env bash
# Install the browser-bridge hub as a systemd user service on the headless host.
#
#   ./install-hub.sh [--port 7788] [--snow] [--uninstall]
#
#   --snow       point ~/.local/bin/snow at wrappers/snow (Snowflake CLI SSO via the bridge)
#   --uninstall  stop and remove the service (config and history are kept)
set -euo pipefail

repo="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
conf="$HOME/.config/browser-bridge"
unit_dir="$HOME/.config/systemd/user"
unit=browser-bridge-hub.service
port=7788
snow=0

while [[ $# -gt 0 ]]; do
    case "$1" in
        --port) port="$2"; shift 2 ;;
        --snow) snow=1; shift ;;
        --uninstall)
            systemctl --user disable --now "$unit" 2>/dev/null || true
            rm -f "$unit_dir/$unit" "$HOME/.local/bin/bb-open"
            systemctl --user daemon-reload
            echo "Removed $unit. Config in $conf was kept."
            exit 0 ;;
        -h|--help) sed -n '2,8p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
        *) echo "unknown option: $1" >&2; exit 1 ;;
    esac
done

python=$(command -v /usr/bin/python3 || command -v python3) || { echo "python3 is required" >&2; exit 1; }
command -v systemctl >/dev/null || { echo "systemd is required" >&2; exit 1; }

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

mkdir -p "$unit_dir" "$HOME/.local/bin"
sed -e "s|@REPO@|$repo|g" -e "s|@PYTHON@|$python|g" "$repo/systemd/$unit" > "$unit_dir/$unit"
systemctl --user daemon-reload
systemctl --user enable "$unit" >/dev/null
systemctl --user restart "$unit"
ln -sfn "$repo/bin/bb-open" "$HOME/.local/bin/bb-open"

if [[ "$(loginctl show-user "$USER" -p Linger --value 2>/dev/null)" != yes ]]; then
    echo "warning: lingering is off, the hub stops when you log out. Fix: sudo loginctl enable-linger $USER" >&2
fi

if (( snow )); then
    ln -sfn "$repo/wrappers/snow" "$HOME/.local/bin/snow"
    echo "Linked ~/.local/bin/snow -> wrappers/snow"
fi

# Prefer the Tailscale machine name, which is what the PCs resolve.
host=$(tailscale status --json 2>/dev/null | "$python" -c 'import json,sys; print(json.load(sys.stdin)["Self"]["DNSName"].split(".")[0])' 2>/dev/null || hostname -s)

sleep 1
if systemctl --user is-active --quiet "$unit"; then
    addrs=$("$python" -c 'import json,sys; c=json.load(open(sys.argv[1])); print(" ".join("http://%s:%s/" % (a, c.get("port", 7788)) for a in c["listen"]))' "$conf/hub.json")
    echo
    echo "Hub is running. Status page: $addrs"
    echo "On each PC:   git clone <this repo> && cd browser-bridge && ./install-agent.sh --hub http://$host:$port"
    echo "Use it:       export BROWSER=$repo/bin/bb-open   (or run: bb-open <url>)"
else
    echo "Hub failed to start:" >&2
    journalctl --user -u "$unit" -n 20 --no-pager >&2
    exit 1
fi
