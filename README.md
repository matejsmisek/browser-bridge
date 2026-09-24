# browser-bridge

[![CI](https://github.com/matejsmisek/browser-bridge/actions/workflows/ci.yml/badge.svg)](https://github.com/matejsmisek/browser-bridge/actions/workflows/ci.yml)

Open URLs from a headless server in the browser on your desktop, including SSO / OAuth
logins that redirect back to `localhost:<port>` (Snowflake `externalbrowser`, gcloud, gh, …).

```
 server (hub)                                   desktop PC (agent)
 ┌──────────────────────────────┐   Tailscale   ┌──────────────────────────────┐
 │ snow ── $BROWSER=bb-open ──► hub :7788 ◄──long-poll── agent                 │
 │  └ waits on 127.0.0.1:P      │               │   ├ opens URL in Chrome       │
 │      ▲                       │               │   │  profile of your choice   │
 │      └── relay <ts-ip>:P ◄────────────────────── └ forwards localhost:P      │
 └──────────────────────────────┘               └──────────────────────────────┘
```

1. A tool calls `$BROWSER` (`bin/bb-open`) with a URL.
2. The hub finds the port the tool is waiting on for the redirect: from a `localhost:<port>`
   redirect in the URL, or from the listening sockets of the calling process.
3. The hub hands the URL to a connected agent and exposes that port on its Tailscale address.
4. The agent forwards its own `localhost:<port>` to the hub and opens the URL in the browser.
5. You log in (or you are already logged in). The IdP redirects to `localhost:<port>`, which
   the agent tunnels back to the tool on the server.

If no agent is connected, `bb-open` exits non-zero and tools fall back to their usual
"open this URL manually" prompt.

The hub serves a status page at `http://<server>:7788/` showing connected agents, a test
button per agent, and the history of requests with a timeline for each.

Python 3.8+ standard library only. Linux with systemd on both sides.

## Install the hub (server)

```bash
git clone <repo> ~/browser-bridge && cd ~/browser-bridge
./install-hub.sh            # add --snow to route the Snowflake CLI through it
```

This creates `~/.config/browser-bridge/{token,hub.json}`, installs the
`browser-bridge-hub` user service, and links `bb-open` into `~/.local/bin`. The hub listens on
`127.0.0.1` and the Tailscale IP. Keep lingering on (`loginctl enable-linger`) so it runs
without an open session.

Then make tools use it:

```bash
export BROWSER=~/browser-bridge/bin/bb-open   # generic: anything that honours $BROWSER
bb-open https://example.com                   # or open something by hand
```

`--snow` points `~/.local/bin/snow` at `wrappers/snow`, which sets `BROWSER` just for snow.
Keep `client_store_temporary_credential = true` in the Snowflake connection so a login is
only needed when the cached token expires.

## Install an agent (desktop PC)

```bash
git clone <repo> && cd browser-bridge
./install-agent.sh --hub http://<server>:7788
```

The installer fetches the token from the hub over SSH (or asks for it), lets you pick the
browser profile where your SSO session lives (every Chrome / Chromium / Brave / Edge profile
is listed with its account email), checks the hub accepts the token, and installs the
`browser-bridge-agent` user service tied to your graphical session. Then press **Send test**
on the status page.

- `./install-agent.sh --reconfigure` picks a different browser profile.
- `python3 bridge/agent.py profiles` lists the profiles it can see.
- `journalctl --user -u browser-bridge-agent -f` shows the agent's log.

## Several agents

Every agent appears on the status page. Requests go to the one marked **default**, or to the
most recently active connected agent. `bb-open --agent <name> <url>` targets one explicitly.

## Security

- Agents authenticate with the shared token in `~/.config/browser-bridge/token`.
- Open requests are accepted only from the hub machine itself (loopback).
- Agents only open `http(s)` URLs and only forward unprivileged ports, only to the hub.
- The status page and its test/default buttons need no auth. Anyone who can reach the hub
  port (your tailnet) can see request URLs, so don't expose it beyond the tailnet.

## Similar tools

- [VS Code Remote-SSH](https://code.visualstudio.com/docs/remote/ssh) sets `$BROWSER` in its
  integrated terminal and auto-forwards ports that remote processes listen on. It only works
  in a terminal inside a VS Code window, not over plain SSH, in tmux or in background jobs.
- [ssh-open](https://github.com/arthursn/ssh-open) brings the same behaviour to plain SSH:
  a reverse tunnel carries URLs to the desktop, and `localhost` ports found in the URL are
  forwarded back. It needs a live SSH session, and the callback port has to appear in the URL.
- [opener](https://github.com/superbrothers/opener),
  [lemonade](https://github.com/lemonade-command/lemonade) and
  [local-open](https://github.com/suan/local-open) open remote URLs on the desktop, but do not
  forward the login callback.
- Without a tool: forward the callback port by hand with `ssh -L`, or use a device-code login
  (`gcloud --no-browser`, `az login --use-device-code`) where the tool offers one.

browser-bridge needs no SSH session: it runs over Tailscale, so it also works from tmux,
detached jobs and coding agents. It finds the callback port from the sockets the calling
process listens on, so it works when the port is not in the URL, as with Snowflake
`externalbrowser`. It can also route to several desktops and to a chosen browser profile.

## Files

| Path | Where it runs |
| --- | --- |
| `bridge/hub.py`, `bridge/status.html` | server |
| `bin/bb-open`, `wrappers/snow` | server |
| `bridge/agent.py` | desktop PC |
| `install-hub.sh`, `install-agent.sh`, `systemd/` | installers and unit templates |

State: `~/.config/browser-bridge/` (config, token), `~/.local/state/browser-bridge/`
(hub history and default agent).

## Development

CI runs black, ruff and shellcheck, and checks the scripts start on Python 3.8 and 3.13.
To run the same checks locally:

```
pipx run black --check .
pipx run ruff check .
shellcheck install-hub.sh install-agent.sh wrappers/snow
```

## Uninstall

`./install-hub.sh --uninstall` or `./install-agent.sh --uninstall`. Config is kept.

## License

[MIT](LICENSE)
