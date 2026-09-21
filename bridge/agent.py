#!/usr/bin/env python3
"""browser-bridge agent: runs on the desktop PC.

Long-polls the hub for URLs to open, opens them in the configured browser (optionally a
specific Chrome profile) and, when the requesting tool waits for a redirect on
localhost:<port>, forwards this machine's localhost:<port> to the hub for a few minutes.
Standard library only.
"""
import argparse
import getpass
import json
import os
import shutil
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser

VERSION = "0.1.0"
CONFIG_DIR = os.path.expanduser("~/.config/browser-bridge")
CONFIG_FILE = os.path.join(CONFIG_DIR, "agent.json")
TOKEN_FILE = os.path.join(CONFIG_DIR, "token")
FORWARD_TTL = 300

# Chromium-family browsers: (label, user data dir, executables to try)
CHROMIUM_BROWSERS = [
    ("Google Chrome", "~/.config/google-chrome", ["google-chrome", "google-chrome-stable"]),
    ("Chromium", "~/.config/chromium", ["chromium", "chromium-browser"]),
    ("Chromium (snap)", "~/snap/chromium/common/chromium", ["chromium"]),
    ("Brave", "~/.config/BraveSoftware/Brave-Browser", ["brave-browser", "brave"]),
    ("Microsoft Edge", "~/.config/microsoft-edge", ["microsoft-edge", "microsoft-edge-stable"]),
]


def log(msg):
    print(time.strftime("%H:%M:%S ") + msg, flush=True)


# ---------------------------------------------------------------- config

def load_config():
    with open(CONFIG_FILE) as f:
        cfg = json.load(f)
    with open(TOKEN_FILE) as f:
        cfg["token"] = f.read().strip()
    return cfg


def chrome_profiles():
    """[{browser, exe, dir, name, email}] for every installed Chromium-family profile."""
    found = []
    for label, data_dir, exes in CHROMIUM_BROWSERS:
        exe = next((shutil.which(e) for e in exes if shutil.which(e)), None)
        try:
            with open(os.path.join(os.path.expanduser(data_dir), "Local State")) as f:
                cache = json.load(f).get("profile", {}).get("info_cache", {})
        except (OSError, ValueError):
            continue
        if not exe:
            continue
        for directory, info in sorted(cache.items()):
            found.append({"browser": label, "exe": exe, "dir": directory,
                          "name": info.get("name") or directory, "email": info.get("user_name") or ""})
    return found


def describe_browser(cfg):
    return cfg.get("browser_label") or ("custom: " + " ".join(cfg["browser"]) if cfg.get("browser") else None)


# ---------------------------------------------------------------- hub API

def hub_call(cfg, method, path, payload=None, timeout=10):
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(cfg["hub"].rstrip("/") + path, data=data, method=method, headers={
        "X-Token": cfg["token"],
        "Content-Type": "application/json",
        "X-Agent-Version": VERSION,
        "X-Agent-Browser": describe_browser(cfg) or "system default",
    })
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        body = resp.read()
        return resp.status, (json.loads(body) if body else None)


def report(cfg, path, payload):
    try:
        hub_call(cfg, "POST", path, payload)
    except (OSError, ValueError) as e:
        log("could not report to hub: %s" % e)


# ---------------------------------------------------------------- forwarding

forwards = {}  # local port -> {deadline, target, job_id, reported}
forwards_lock = threading.Lock()


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


def ensure_forward(cfg, job):
    """Forward 127.0.0.1:<port> -> forward_host:forward_port until FORWARD_TTL passes."""
    port = job["port"]
    target = (job["forward_host"], job["forward_port"])
    with forwards_lock:
        if port in forwards:
            forwards[port]["deadline"] = time.time() + FORWARD_TTL
            forwards[port].update(target=target, job_id=job["id"], reported=False)
            return
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind(("127.0.0.1", port))  # raises if something local already uses the port
        listener.listen(8)
        listener.settimeout(1)
        entry = {"deadline": time.time() + FORWARD_TTL, "target": target, "job_id": job["id"], "reported": False}
        forwards[port] = entry

    def handle(client):
        try:
            upstream = socket.create_connection(entry["target"], timeout=10)
            upstream.settimeout(None)
        except OSError as e:
            log("forward :%d -> %s:%d failed: %s" % (port, entry["target"][0], entry["target"][1], e))
            client.close()
            return
        if not entry["reported"]:
            entry["reported"] = True
            log("redirect on :%d forwarded to hub" % port)
            report(cfg, "/api/agent/event", {"id": entry["job_id"], "event": "callback"})
        threading.Thread(target=pipe, args=(client, upstream), daemon=True).start()
        pipe(upstream, client)
        client.close()
        upstream.close()

    def accept_loop():
        try:
            while True:
                with forwards_lock:
                    if time.time() > entry["deadline"]:
                        del forwards[port]
                        return
                try:
                    client, _ = listener.accept()
                except socket.timeout:
                    continue
                threading.Thread(target=handle, args=(client,), daemon=True).start()
        finally:
            listener.close()

    threading.Thread(target=accept_loop, daemon=True).start()


# ---------------------------------------------------------------- browser

def open_browser(cfg, url):
    cmd = cfg.get("browser")
    if not cmd:
        if not webbrowser.open_new_tab(url):
            raise RuntimeError("no usable browser found")
        return
    # Detached, so a Chrome started by us is not tied to the agent's lifetime.
    subprocess.Popen(cmd + [url], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                     stderr=subprocess.DEVNULL, start_new_session=True)


def handle_job(cfg, job):
    url = job.get("url", "")
    if not url.startswith(("http://", "https://")):
        return report(cfg, "/api/agent/ack", {"id": job["id"], "ok": False, "error": "refusing non-http URL"})
    try:
        if job.get("port"):
            if not 1024 <= int(job["port"]) <= 65535:
                raise ValueError("refusing privileged port %s" % job["port"])
            ensure_forward(cfg, job)
        open_browser(cfg, url)
    except Exception as e:  # report anything back so the hub can fail fast
        log("job %s failed: %s" % (job["id"], e))
        return report(cfg, "/api/agent/ack", {"id": job["id"], "ok": False, "error": str(e)})
    log("opened %s" % url[:120] + (" (forwarding :%d)" % job["port"] if job.get("port") else ""))
    report(cfg, "/api/agent/ack", {"id": job["id"], "ok": True})


# ---------------------------------------------------------------- commands

def cmd_run(_args):
    cfg = load_config()
    log("browser-bridge agent %s as %r -> %s (%s)" % (VERSION, cfg["name"], cfg["hub"], describe_browser(cfg) or "system default"))
    backoff = 1
    connected = False
    while True:
        try:
            status, job = hub_call(cfg, "GET", "/api/agent/poll?name=" + urllib.parse.quote(cfg["name"]), timeout=40)
            if not connected:
                log("connected to hub")
                connected = True
            backoff = 1
            if status == 200 and job:
                threading.Thread(target=handle_job, args=(cfg, job), daemon=True).start()
        except urllib.error.HTTPError as e:
            if e.code == 403:
                log("hub rejected the token; check %s" % TOKEN_FILE)
            else:
                log("hub error %s" % e.code)
            connected = False
            time.sleep(backoff)
            backoff = min(backoff * 2, 30)
        except (OSError, ValueError) as e:
            if connected:
                log("lost hub connection: %s" % e)
            connected = False
            time.sleep(backoff)
            backoff = min(backoff * 2, 30)


def cmd_ping(_args):
    cfg = load_config()
    try:
        _, res = hub_call(cfg, "GET", "/api/agent/ping")
    except urllib.error.HTTPError as e:
        sys.exit("hub at %s answered %s (%s)" % (cfg["hub"], e.code, "bad token" if e.code == 403 else e.reason))
    except OSError as e:
        sys.exit("cannot reach hub at %s: %s" % (cfg["hub"], e))
    print("hub %s reachable, version %s, token accepted" % (cfg["hub"], res["version"]))


def cmd_profiles(_args):
    for i, p in enumerate(chrome_profiles(), 1):
        print("%2d. %-16s %-12s %s%s" % (i, p["browser"], p["dir"], p["name"], " <%s>" % p["email"] if p["email"] else ""))


def ask(prompt, default=None):
    answer = input("%s%s: " % (prompt, " [%s]" % default if default else "")).strip()
    return answer or default


def cmd_configure(args):
    os.makedirs(CONFIG_DIR, mode=0o700, exist_ok=True)
    try:
        with open(CONFIG_FILE) as f:
            cfg = json.load(f)
    except (OSError, ValueError):
        cfg = {}
    cfg["hub"] = args.hub or cfg.get("hub") or ask("Hub URL (e.g. http://my-server:7788)")
    cfg["name"] = args.name or cfg.get("name") or ask("Name of this machine", socket.gethostname().split(".")[0])

    if args.token:
        token = args.token
    elif os.path.exists(TOKEN_FILE):
        token = None
    else:
        token = getpass.getpass("Hub token (on the hub: cat ~/.config/browser-bridge/token): ").strip()
    if token:
        fd = os.open(TOKEN_FILE, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write(token + "\n")

    if not args.keep_browser or "browser" not in cfg:
        profiles = chrome_profiles()
        print("\nWhich browser should open requests? Pick the profile where you are logged in to your SSO.")
        print(" 0. System default browser")
        for i, p in enumerate(profiles, 1):
            print("%2d. %s: %s%s" % (i, p["browser"], p["name"], " <%s>" % p["email"] if p["email"] else ""))
        while True:
            choice = ask("Choice", "1" if profiles else "0")
            if choice.isdigit() and 0 <= int(choice) <= len(profiles):
                break
        if choice == "0":
            cfg.pop("browser", None)
            cfg.pop("browser_label", None)
        else:
            p = profiles[int(choice) - 1]
            cfg["browser"] = [p["exe"], "--profile-directory=" + p["dir"]]
            cfg["browser_label"] = "%s · %s%s" % (p["browser"], p["name"], " (%s)" % p["email"] if p["email"] else "")

    with open(CONFIG_FILE, "w") as f:
        json.dump(cfg, f, indent=2)
        f.write("\n")
    print("\nWrote %s" % CONFIG_FILE)


def cmd_open(args):
    cfg = load_config()
    open_browser(cfg, args.url)


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("run", help="connect to the hub and serve requests").set_defaults(fn=cmd_run)
    sub.add_parser("ping", help="check hub reachability and token").set_defaults(fn=cmd_ping)
    sub.add_parser("profiles", help="list Chromium-family browser profiles").set_defaults(fn=cmd_profiles)
    p = sub.add_parser("configure", help="write ~/.config/browser-bridge/agent.json")
    p.add_argument("--hub")
    p.add_argument("--name")
    p.add_argument("--token")
    p.add_argument("--keep-browser", action="store_true", help="keep the configured browser if set")
    p.set_defaults(fn=cmd_configure)
    p = sub.add_parser("open", help="open a URL with the configured browser (local test)")
    p.add_argument("url")
    p.set_defaults(fn=cmd_open)
    args = parser.parse_args()
    args.fn(args)


if __name__ == "__main__":
    main()
