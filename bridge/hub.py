#!/usr/bin/env python3
"""browser-bridge hub: runs on the headless host.

Local tools ask the hub (via bin/bb-open, usually as $BROWSER) to open a URL. The hub
hands the request to a connected agent (a desktop PC long-polling the hub over
Tailscale), which opens it in a real browser. If the tool waits for an OAuth/SSO
redirect on localhost:<port>, the hub exposes that port on its Tailscale address and
the agent forwards its own localhost:<port> there, so the redirect lands back here.

Serves a status page with connected agents and request history. Standard library only.
"""

import argparse
import collections
import http.server
import json
import os
import re
import secrets
import socket
import subprocess
import sys
import threading
import time
import urllib.request
from urllib.parse import parse_qs, unquote, urlsplit

VERSION = "0.1.0"
MACOS = sys.platform == "darwin"
CONFIG_DIR = os.path.expanduser("~/.config/browser-bridge")
STATE_DIR = os.path.join(
    os.path.expanduser(os.environ.get("XDG_STATE_HOME", "~/.local/state")), "browser-bridge"
)

AGENT_STALE = 35  # seconds without a poll before an agent counts as disconnected
POLL_WAIT = 25  # how long an agent's long-poll is held open
ACK_WAIT = 15  # how long a request waits for the agent to confirm it opened the URL
REQUEST_TTL = 300  # how long a callback relay stays up
LISTENER_GONE_GRACE = 3  # let a late "callback" event arrive before judging the outcome
HISTORY_KEEP = 500
TERMINAL = {"no_agent", "done", "failed", "expired", "closed"}

cv = threading.Condition()  # guards agents, requests, state
agents = {}  # name -> agent dict
requests = collections.OrderedDict()  # id -> request dict
relays = {}  # request id -> Relay
state = {"default_agent": None}
config = {}


# ---------------------------------------------------------------- process & socket helpers
# Linux reads /proc; macOS asks lsof and ps.


def _decode_addr(hexaddr):
    raw = bytes.fromhex(hexaddr)
    if len(raw) == 4:
        return socket.inet_ntop(socket.AF_INET, raw[::-1])
    raw = b"".join(raw[i : i + 4][::-1] for i in range(0, 16, 4))
    return socket.inet_ntop(socket.AF_INET6, raw)


def listening_sockets():
    """inode -> (addr, port) for every TCP socket in LISTEN state."""
    out = {}
    for path in ("/proc/net/tcp", "/proc/net/tcp6"):
        try:
            with open(path) as f:
                lines = f.read().splitlines()[1:]
        except OSError:
            continue
        for line in lines:
            fields = line.split()
            if fields[3] != "0A":
                continue
            addr, port = fields[1].split(":")
            out[int(fields[9])] = (_decode_addr(addr), int(port, 16))
    return out


def run(cmd):
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=5).stdout
    except (OSError, subprocess.SubprocessError):
        return ""


def lsof_listeners(port=None, pid=None):
    """[(addr, port)] of TCP sockets in LISTEN state, from lsof (macOS has no /proc)."""
    cmd = [
        "/usr/sbin/lsof",
        "-nP",
        "-a",
        "-iTCP" + (":%d" % port if port else ""),
        "-sTCP:LISTEN",
        "-Ftn",
    ]
    if pid:
        cmd += ["-p", str(pid)]
    socks, ipv6 = [], False
    for line in run(cmd).splitlines():
        if line.startswith("t"):
            ipv6 = line == "tIPv6"
        elif line.startswith("n"):
            addr, _, sock_port = line[1:].rpartition(":")
            addr = addr.strip("[]")
            if addr == "*":
                addr = "::" if ipv6 else "0.0.0.0"
            socks.append((addr, int(sock_port)))
    return socks


def all_listeners(port=None):
    if MACOS:
        return lsof_listeners(port=port)
    return [s for s in listening_sockets().values() if port is None or s[1] == port]


def pid_listeners(pid):
    if MACOS:
        return lsof_listeners(pid=pid)
    listeners = listening_sockets()
    return [listeners[i] for i in socket_inodes(pid) if i in listeners]


def socket_inodes(pid):
    inodes = set()
    try:
        fds = os.listdir("/proc/%d/fd" % pid)
    except OSError:
        return inodes
    for fd in fds:
        try:
            link = os.readlink("/proc/%d/fd/%s" % (pid, fd))
        except OSError:
            continue
        if link.startswith("socket:["):
            inodes.add(int(link[8:-1]))
    return inodes


def parent_pid(pid):
    if MACOS:
        out = run(["/bin/ps", "-o", "ppid=", "-p", str(pid)]).strip()
        return int(out) if out.isdigit() else 0
    try:
        with open("/proc/%d/stat" % pid) as f:
            return int(f.read().rsplit(")", 1)[1].split()[1])
    except (OSError, IndexError, ValueError):
        return 0


def process_argv(pid):
    if MACOS:
        # ps joins argv with spaces, so arguments that contain spaces come back split.
        return run(["/bin/ps", "-o", "args=", "-p", str(pid)]).split() or None
    try:
        with open("/proc/%d/cmdline" % pid, "rb") as f:
            return [a.decode(errors="replace") for a in f.read().split(b"\0") if a]
    except OSError:
        return None


def describe_process(pid):
    argv = process_argv(pid)
    if argv is None:
        return None
    if len(argv) > 1 and os.path.basename(argv[0]).lower().startswith("python"):
        argv = argv[1:]
    if argv and os.path.basename(argv[0]) in ("sh", "bash", "zsh", "dash", "fish"):
        return os.path.basename(argv[0]) + " (shell)"
    if argv:
        argv[0] = os.path.basename(argv[0])
    text = " ".join(argv)
    return text[:200] + ("…" if len(text) > 200 else "")


LOCAL_REDIRECT = re.compile(r"https?://(?:localhost|127\.0\.0\.1|\[::1\]):(\d{2,5})")
SNOWFLAKE_PORT = re.compile(r"browser_mode_redirect_port=(\d{2,5})")


def port_from_url(url):
    text = unquote(unquote(url))
    m = LOCAL_REDIRECT.search(text) or SNOWFLAKE_PORT.search(text)
    return int(m.group(1)) if m else None


def find_callback(url, pid, port=None):
    """(port, bind_addr) of the local listener waiting for the browser redirect, or (None, None)."""
    port = port or port_from_url(url)
    if port:
        addrs = [a for a, _ in all_listeners(port)]
        return port, (addrs[0] if addrs else "127.0.0.1")
    # Walk up from the requesting process: the tool itself, or the shell that ran xdg-open.
    for _ in range(3):
        if not pid or pid <= 1:
            break
        mine = pid_listeners(pid)
        if mine:
            addr, port = max(mine, key=lambda sock: sock[1])
            return port, addr
        pid = parent_pid(pid)
    return None, None


def is_listening(addr, port):
    return (addr, port) in all_listeners(port)


# ---------------------------------------------------------------- TCP relay


def pipe(src, dst):
    try:
        while True:
            data = src.recv(65536)
            if not data:
                break
            dst.sendall(data)
    except OSError:
        pass
    finally:
        for s in (src, dst):
            try:
                s.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass


def read_request_head(sock, limit=65536, timeout=5):
    """Read up to the end of the HTTP request head, so it goes upstream in one write.

    Callback listeners such as the Snowflake connector parse the request from a single
    recv(). Over Tailscale a long `GET /?token=...` line arrives in several segments, and
    forwarding them one by one hands the listener a cut-off request line."""
    buf = b""
    sock.settimeout(timeout)
    try:
        while b"\r\n\r\n" not in buf and len(buf) < limit:
            data = sock.recv(65536)
            if not data:
                break
            buf += data
    except OSError:
        pass
    finally:
        sock.settimeout(None)
    return buf


class Relay:
    """Expose a loopback-only listener on the hub's Tailscale address."""

    def __init__(self, req_id, bind_addr, port):
        self.req_id = req_id
        self.upstream = ("::1" if ":" in bind_addr else bind_addr, port)
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            self.sock.bind((config["relay_addr"], port))
        except OSError:
            self.sock.bind((config["relay_addr"], 0))
        self.sock.listen(8)
        self.sock.settimeout(1)
        self.port = self.sock.getsockname()[1]
        self.closed = False
        threading.Thread(target=self._accept, daemon=True).start()

    def _accept(self):
        while not self.closed:
            try:
                client, peer = self.sock.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            threading.Thread(target=self._handle, args=(client, peer), daemon=True).start()

    def _handle(self, client, peer):
        try:
            upstream = socket.create_connection(self.upstream, timeout=5)
            upstream.settimeout(None)
        except OSError:
            client.close()
            return
        mark_callback(self.req_id, "callback relayed from %s" % peer[0])
        head = read_request_head(client)
        if head:
            try:
                upstream.sendall(head)
            except OSError:
                pass
        threading.Thread(target=pipe, args=(client, upstream), daemon=True).start()
        pipe(upstream, client)
        client.close()
        upstream.close()

    def close(self):
        self.closed = True
        self.sock.close()


# ---------------------------------------------------------------- requests & agents


def now():
    return time.time()


def log_event(req, text):
    req["events"].append([now(), text])


def persist(req):
    try:
        os.makedirs(STATE_DIR, exist_ok=True)
        with open(os.path.join(STATE_DIR, "history.jsonl"), "a") as f:
            f.write(json.dumps(req) + "\n")
    except OSError:
        pass


def finish(req, status, text):
    """Move a request to a terminal status. Caller holds cv."""
    if req["status"] in TERMINAL:
        return
    req["status"] = status
    req["finished"] = now()
    log_event(req, text)
    relay = relays.pop(req["id"], None)
    if relay:
        relay.close()
    persist(req)
    cv.notify_all()


def mark_callback(req_id, text):
    with cv:
        req = requests.get(req_id)
        if req and req["status"] not in TERMINAL:
            if req["status"] != "callback":
                log_event(req, text)
            req["status"] = "callback"
            cv.notify_all()


def agent_connected(agent):
    return agent["polling"] > 0 or now() - agent["last_seen"] < AGENT_STALE


def choose_agent(target):
    connected = [a for a in agents.values() if agent_connected(a)]
    if target:
        return next((a for a in connected if a["name"] == target), None)
    default = next((a for a in connected if a["name"] == state["default_agent"]), None)
    if default:
        return default
    return max(connected, key=lambda a: a["last_seen"], default=None)


def new_request(url, source, port, bind_addr, kind="open"):
    req = {
        "id": secrets.token_hex(4),
        "kind": kind,
        "created": now(),
        "finished": None,
        "url": url,
        "source": source,
        "port": port,
        "bind": bind_addr,
        "agent": None,
        "status": "queued",
        "events": [],
        "listener_gone": None,
    }
    requests[req["id"]] = req
    while len(requests) > HISTORY_KEEP:
        requests.popitem(last=False)
    return req


def dispatch(req, target, wait):
    """Hand a request to an agent. Caller holds cv. Returns an HTTP status code."""
    agent = choose_agent(target)
    if not agent and config.get("ntfy") and target in (None, PHONE):
        req["agent"] = PHONE
        log_event(req, "sending to phone")
        threading.Thread(target=notify_phone, args=(req,), daemon=True).start()
        return await_pickup(req, wait)
    if not agent:
        finish(req, "no_agent", "no agent connected" + (" named %s" % target if target else ""))
        return 503
    req["agent"] = agent["name"]
    job = {"id": req["id"], "url": req["url"]}
    if req["port"]:
        loopback = req["bind"].startswith("127.") or req["bind"] == "::1"
        if loopback:
            try:
                relay = Relay(req["id"], req["bind"], req["port"])
            except OSError as e:
                finish(req, "failed", "cannot relay port %d: %s" % (req["port"], e))
                return 500
            relays[req["id"]] = relay
            job.update(port=req["port"], forward_host=config["relay_addr"], forward_port=relay.port)
            log_event(
                req,
                "relaying %s:%d → %s:%d"
                % (config["relay_addr"], relay.port, req["bind"], req["port"]),
            )
        else:
            job.update(
                port=req["port"], forward_host=config["relay_addr"], forward_port=req["port"]
            )
    agent["queue"].append(job)
    log_event(req, "queued for %s" % agent["name"])
    cv.notify_all()

    def cancel():
        if job in agent["queue"]:
            agent["queue"].remove(job)

    return await_pickup(req, wait, cancel)


def await_pickup(req, wait, cancel=None):
    """Wait until the request leaves "queued". Caller holds cv. Returns an HTTP status code."""
    if not wait:
        return 202
    deadline = now() + ACK_WAIT
    while req["status"] == "queued" and now() < deadline:
        cv.wait(deadline - now())
    if req["status"] == "queued":
        if cancel:
            cancel()
        finish(req, "failed", "%s did not pick up the request" % req["agent"])
        return 504
    return 200 if req["status"] not in ("failed", "no_agent") else 502


# ---------------------------------------------------------------- phone
# A phone cannot listen on its own localhost, so it cannot take an agent's place. The hub
# sends the URL as an ntfy push instead. After the login, the browser fails to load the
# redirect to localhost:<port>; the user rewrites it to /finish/<port>/... on the hub
# (an iOS Shortcut does this), and the hub hands it to the tool waiting here.

PHONE = "phone"


def notify_phone(req):
    title = "Login for %s on %s" % (req["source"], socket.gethostname().split(".")[0])
    message = "Tap to open. After the login, share the failed localhost page to the Shortcut."
    headers = {
        # HTTP headers must be latin-1; the source can contain "…".
        "Title": title.encode("ascii", "replace").decode(),
        "Click": req["url"],
        "Tags": "key",
    }
    try:
        post = urllib.request.Request(config["ntfy"], data=message.encode(), headers=headers)
        # Per socket operation, so the whole push can take a few times this: keep it well
        # under ACK_WAIT, or the push arrives for a request that already failed.
        urllib.request.urlopen(post, timeout=5).close()
        ok, text = True, "sent to phone"
    except (OSError, ValueError) as e:
        ok, text = False, "phone notification failed: %s" % e
    with cv:
        if req["status"] in TERMINAL:
            return
        if not ok:
            finish(req, "failed", text)
            return
        req["status"] = "opened"
        log_event(req, text)
        if not req["port"]:
            finish(req, "done", "no redirect expected")
        cv.notify_all()


def connect_addr(bind_addr):
    if bind_addr in ("0.0.0.0", ""):
        return "127.0.0.1"
    return "::1" if ":" in bind_addr else bind_addr


def deliver_redirect(req, target):
    """Send `GET target` to the tool's listener in one write and return its raw response."""
    upstream = socket.create_connection((connect_addr(req["bind"]), req["port"]), timeout=10)
    with upstream:
        upstream.sendall(
            (
                "GET %s HTTP/1.1\r\nHost: localhost:%d\r\nConnection: close\r\n\r\n"
                % (target, req["port"])
            ).encode(
                "latin-1"
            )  # http.server decoded the path as latin-1
        )
        response = b""
        try:
            while True:
                data = upstream.recv(65536)
                if not data:
                    break
                response += data
        except OSError:
            pass  # a timeout or reset after the answer still leaves a usable response
        return response


def watcher():
    """Resolve requests once the waiting tool stops listening, or when they expire."""
    while True:
        time.sleep(1)
        with cv:
            active = [r for r in requests.values() if r["status"] not in TERMINAL]
        for req in active:
            gone = (
                req["port"]
                and req["status"] != "queued"
                and not is_listening(req["bind"], req["port"])
            )
            with cv:
                if req["status"] in TERMINAL:
                    continue
                if time.time() - req["created"] > REQUEST_TTL:
                    finish(req, "expired", "no redirect within %ds" % REQUEST_TTL)
                elif gone:
                    req["listener_gone"] = req["listener_gone"] or now()
                    if req["status"] == "callback":
                        finish(req, "done", "login completed")
                    elif now() - req["listener_gone"] > LISTENER_GONE_GRACE:
                        finish(req, "closed", "tool stopped waiting before the redirect")


# ---------------------------------------------------------------- test page server

TEST_PAGE = """<!doctype html><meta charset=utf-8><title>browser-bridge test</title>
<style>body{font:16px system-ui;display:grid;place-items:center;height:90vh;margin:0}
div{text-align:center}h1{font-size:28px}</style>
<div><h1>✓ browser-bridge works</h1><p>Opened on <b>%s</b>, redirect delivered back to the hub.</p>
<p>You can close this tab.</p></div>"""


def start_test_listener(agent_name):
    """One-shot HTTP server on loopback that mimics a CLI waiting for an OAuth redirect."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    sock.listen(4)
    sock.settimeout(REQUEST_TTL)

    def serve():
        try:
            while True:
                client, _ = sock.accept()
                with client:
                    line = client.recv(4096).split(b"\r\n", 1)[0]
                    if b"favicon" in line:
                        client.sendall(b"HTTP/1.0 404 Not Found\r\nContent-Length: 0\r\n\r\n")
                        continue
                    body = (TEST_PAGE % agent_name).encode()
                    client.sendall(
                        b"HTTP/1.0 200 OK\r\nContent-Type: text/html; charset=utf-8\r\n"
                        b"Content-Length: %d\r\n\r\n%s" % (len(body), body)
                    )
                    return
        except OSError:
            pass
        finally:
            sock.close()

    threading.Thread(target=serve, daemon=True).start()
    return sock.getsockname()[1]


# ---------------------------------------------------------------- HTTP


def public_request(req):
    r = dict(req)
    r.pop("listener_gone", None)
    r.pop("claimed", None)
    return r


def status_snapshot():
    with cv:
        return {
            "hub": {
                "version": VERSION,
                "started": STARTED,
                "listen": config["listen"],
                "port": config["port"],
                "relay_addr": config["relay_addr"],
                "now": now(),
            },
            "agents": [
                {
                    "name": a["name"],
                    "ip": a["ip"],
                    "version": a["version"],
                    "browser": a["browser"],
                    "last_seen": a["last_seen"],
                    "connected": agent_connected(a),
                    "default": a["name"] == state["default_agent"],
                }
                for a in sorted(agents.values(), key=lambda a: a["name"])
            ],
            "requests": [public_request(r) for r in reversed(list(requests.values())[-200:])],
        }


class Handler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "browser-bridge/" + VERSION

    def log_message(self, fmt, *args):
        if not self.path.startswith(("/api/agent/poll", "/api/status")):
            super().log_message(fmt, *args)

    def send(self, code, payload=None, content_type="application/json", body=None):
        if body is None:
            body = json.dumps(payload if payload is not None else {}).encode()
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def body(self):
        length = int(self.headers.get("Content-Length") or 0)
        try:
            data = json.loads(self.rfile.read(length) or b"{}")
        except ValueError:
            return {}
        return data if isinstance(data, dict) else {}

    def is_loopback(self):
        return self.client_address[0] in ("127.0.0.1", "::1")

    def authorized(self):
        return secrets.compare_digest(self.headers.get("X-Token", ""), config["token"])

    def do_GET(self):
        path = urlsplit(self.path)
        if path.path == "/":
            return self.send(
                200, content_type="text/html; charset=utf-8", body=STATUS_PAGE.encode()
            )
        if path.path == "/api/status":
            return self.send(200, status_snapshot())
        if path.path.startswith("/finish/"):
            return self.finish_redirect()
        if path.path == "/api/agent/poll":
            return self.agent_poll(parse_qs(path.query))
        if path.path == "/api/agent/ping":
            if not self.authorized():
                return self.send(403, {"error": "bad token"})
            return self.send(200, {"version": VERSION})
        self.send(404, {"error": "not found"})

    def do_POST(self):
        routes = {
            "/api/open": self.open_url,
            "/api/test": self.test,
            "/api/default": self.set_default,
            "/api/agent/ack": self.agent_ack,
            "/api/agent/event": self.agent_event,
        }
        route = routes.get(urlsplit(self.path).path)
        if not route:
            return self.send(404, {"error": "not found"})
        route(self.body())

    # --- local tools

    def open_url(self, data):
        if not self.is_loopback():
            return self.send(403, {"error": "open requests are only accepted from the hub host"})
        url = str(data.get("url", ""))
        if urlsplit(url).scheme not in ("http", "https"):
            return self.send(400, {"error": "only http(s) URLs can be opened"})
        pid = int(data.get("pid") or 0)
        port, bind_addr = find_callback(url, pid, data.get("port"))
        source = data.get("source") or (describe_process(pid) if pid else None) or "unknown"
        with cv:
            req = new_request(url, source, port, bind_addr)
            log_event(
                req, "requested by %s" % source + (" (callback port %d)" % port if port else "")
            )
            code = dispatch(req, data.get("agent"), wait=True)
            reply = {
                "id": req["id"],
                "status": req["status"],
                "agent": req["agent"],
                "port": port,
                "error": req["events"][-1][1] if code >= 300 else None,
            }
        self.send(code, reply)

    def finish_redirect(self):
        """Phone: /finish/<port>/<path?query> delivers the redirect meant for localhost:<port>."""
        # Without this, any tailnet node (or a page open in a tailnet browser) could hand the
        # waiting tool its own token.
        phone_ip = config.get("phone_ip")
        if phone_ip and self.client_address[0] != phone_ip:
            return self.send(403, {"error": "only %s can finish phone logins" % phone_ip})
        m = re.match(r"/finish/(\d{2,5})(?=[/?]|$)(.*)$", self.path)
        if not m:
            return self.send(400, {"error": "expected /finish/<port>/..."})
        port, target = int(m.group(1)), m.group(2)
        if not target.startswith("/"):
            target = "/" + target
        with cv:
            req = next(
                (
                    r
                    for r in reversed(list(requests.values()))
                    if r["agent"] == PHONE
                    and r["port"] == port
                    and r["status"] == "opened"
                    and not r.get("claimed")
                ),
                None,
            )
            if req:
                req["claimed"] = True  # one delivery per login
        if not req:
            return self.send(404, {"error": "no phone login is waiting on port %d" % port})
        error = None
        try:
            response = deliver_redirect(req, target)
            if not response:
                error = "tool on port %d sent no response" % port
        except OSError as e:
            error = "tool on port %d unreachable: %s" % (port, e)
        if error:
            with cv:
                req["claimed"] = False
                log_event(req, "phone redirect failed: %s" % error)
            return self.send(502, {"error": error})
        mark_callback(req["id"], "redirect delivered from phone %s" % self.client_address[0])
        self.close_connection = True
        self.wfile.write(response)

    # --- status page actions

    def test(self, data):
        target = data.get("agent")
        with cv:
            agent = choose_agent(target)
            name = agent["name"] if agent else (target or "?")
        port = start_test_listener(name)
        with cv:
            req = new_request(
                "http://localhost:%d/" % port, "status page test", port, "127.0.0.1", kind="test"
            )
            log_event(req, "test requested from %s" % self.client_address[0])
            code = dispatch(req, target, wait=False)
        self.send(code if code >= 300 else 200, {"id": req["id"], "status": req["status"]})

    def set_default(self, data):
        with cv:
            state["default_agent"] = data.get("agent") or None
            save_state()
        self.send(200, {"default_agent": state["default_agent"]})

    # --- agents

    def agent_poll(self, query):
        if not self.authorized():
            return self.send(403, {"error": "bad token"})
        name = (query.get("name") or [""])[0][:64]
        if not name:
            return self.send(400, {"error": "name required"})
        with cv:
            agent = agents.setdefault(
                name,
                {
                    "name": name,
                    "queue": collections.deque(),
                    "polling": 0,
                    "last_seen": 0,
                    "ip": None,
                    "version": None,
                    "browser": None,
                },
            )
            agent.update(
                ip=self.client_address[0],
                version=self.headers.get("X-Agent-Version"),
                browser=self.headers.get("X-Agent-Browser"),
                last_seen=now(),
            )
            agent["polling"] += 1
            deadline = now() + POLL_WAIT
            try:
                while not agent["queue"] and now() < deadline:
                    cv.wait(deadline - now())
                job = agent["queue"].popleft() if agent["queue"] else None
            finally:
                agent["polling"] -= 1
                agent["last_seen"] = now()
        if job:
            return self.send(200, job)
        self.send(204, body=b"")

    def agent_ack(self, data):
        if not self.authorized():
            return self.send(403, {"error": "bad token"})
        with cv:
            req = requests.get(data.get("id"))
            if not req or req["status"] in TERMINAL:
                return self.send(404, {"error": "unknown request"})
            if data.get("ok"):
                req["status"] = "opened"
                log_event(req, "opened in browser on %s" % req["agent"])
                if not req["port"]:
                    finish(req, "done", "no redirect expected")
            else:
                finish(req, "failed", "agent error: %s" % data.get("error"))
            cv.notify_all()
        self.send(200, {})

    def agent_event(self, data):
        if not self.authorized():
            return self.send(403, {"error": "bad token"})
        if data.get("event") == "callback":
            mark_callback(data.get("id"), "redirect received by agent")
        self.send(200, {})


# ---------------------------------------------------------------- config & main


def save_state():
    os.makedirs(STATE_DIR, exist_ok=True)
    with open(os.path.join(STATE_DIR, "state.json"), "w") as f:
        json.dump(state, f)


def load():
    with open(os.path.join(CONFIG_DIR, "hub.json")) as f:
        config.update(json.load(f))
    with open(os.path.join(CONFIG_DIR, "token")) as f:
        config["token"] = f.read().strip()
    config.setdefault("port", 7788)
    config.setdefault("listen", ["127.0.0.1"])
    config.setdefault(
        "relay_addr", next((a for a in config["listen"] if not a.startswith("127.")), "127.0.0.1")
    )
    try:
        with open(os.path.join(STATE_DIR, "state.json")) as f:
            state.update(json.load(f))
    except (OSError, ValueError):
        pass
    try:
        with open(os.path.join(STATE_DIR, "history.jsonl")) as f:
            for line in collections.deque(f, maxlen=HISTORY_KEEP):
                req = json.loads(line)
                requests[req["id"]] = req
    except (OSError, ValueError):
        pass


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.parse_args()
    load()
    servers = []
    for addr in config["listen"]:
        server = http.server.ThreadingHTTPServer((addr, config["port"]), Handler)
        server.daemon_threads = True
        servers.append(server)
    for server in servers:
        threading.Thread(target=server.serve_forever, daemon=True).start()
    print(
        "browser-bridge hub %s on %s port %d, relay via %s"
        % (VERSION, ", ".join(config["listen"]), config["port"], config["relay_addr"]),
        flush=True,
    )
    watcher()


STARTED = now()
STATUS_PAGE = open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "status.html")).read()

if __name__ == "__main__":
    main()
