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

Python 3.8+ standard library only. Linux (systemd) or macOS (launchd) on either side.

## Install the hub (server)

```bash
git clone <repo> ~/browser-bridge && cd ~/browser-bridge
./install-hub.sh            # add --snow to route the Snowflake CLI through it
```

This creates `~/.config/browser-bridge/{token,hub.json}`, installs the
`browser-bridge-hub` user service, and links `bb-open` into `~/.local/bin`. The hub listens on
`127.0.0.1` and the Tailscale IP. On Linux, keep lingering on (`loginctl enable-linger`) so it
runs without an open session. On macOS it is a LaunchAgent, so it runs while you are logged in.

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
- `journalctl --user -u browser-bridge-agent -f` (Linux) or
  `tail -f ~/Library/Logs/browser-bridge-agent.log` (macOS) shows the agent's log.

## Several agents

Every agent appears on the status page. Requests go to the one marked **default**, or to the
most recently active connected agent. `bb-open --agent <name> <url>` targets one explicitly.

## Phone (iOS)

A phone cannot run an agent: iOS suspends an app that waits on `localhost` as soon as you
switch to the browser. Instead, the hub sends the URL to the phone as an
[ntfy](https://ntfy.sh) push, and you give the redirect back to the hub by hand:

1. Install the ntfy app on the phone and subscribe to a topic with a long random name.
2. Add the topic and the phone's Tailscale IP to `~/.config/browser-bridge/hub.json` and
   restart the hub: `"ntfy": "https://ntfy.sh/<topic>", "phone_ip": "100.x.y.z"`. Without
   `phone_ip`, any tailnet node can finish a phone login with a token of its own.
3. In Shortcuts, make a share-sheet shortcut that accepts URLs and text (clipboard when there
   is no input), replaces the regular expression `^https?://(localhost|127\.0\.0\.1):` with
   `http://<hub>:7788/finish/`, and opens the result.

When no agent is connected, requests go to the phone. `bb-open --agent phone`, or
`BROWSER_BRIDGE_AGENT=phone` for tools that run `$BROWSER`, sends them there anyway. Tap
the notification and log in. The browser then fails to load `http://localhost:<port>/...`;
share that page (or copy its address) to the shortcut. The hub delivers the redirect to the
tool that waits for it. Only a login sent to the phone can be finished this way, and only
once. The phone must be on the tailnet.

With `ntfy` set, `bb-open` no longer fails when no agent is connected, so tools do not fall
back to printing the URL: every `$BROWSER` URL goes to the phone. The URLs go through the ntfy
server, and anyone who knows the topic can send you notifications. Use a long topic name, or
your own ntfy server.

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
| `install-hub.sh`, `install-agent.sh`, `systemd/`, `launchd/` | installers and service templates |

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
