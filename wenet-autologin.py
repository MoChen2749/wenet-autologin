#!/usr/bin/env python3
"""Campus portal (Huawei AC + WISPr / wenet) auto-login for OpenWrt / iStoreOS routers.

Why on the router?  The AC records the login against the WAN-side MAC address of
the device that sent the request.  Logging in from the router therefore binds the
account to the router itself, and every LAN client is NATed through that session.

Flow:
  1. probe a plain-http URL without following redirects
  2. if we get 30x + Location carrying userip/wlanacname/nas-ip/usermac, we are
     behind a captive portal: GET the portal page so the server hands us a
     `redirectUrl` cookie
  3. POST AES-128-CBC encrypted JSON to /portal-conversion/api/v3/portal/connect
  4. decrypt the answer, probe again to verify real connectivity
Run as a daemon (default, for procd) or one-shot (--once) for cron/testing.

Only the Python standard library plus the `openssl` binary are required.
"""

import argparse
import binascii
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

# key == iv, taken from the portal frontend bundle.  Change it if your portal
# uses a different constant (see docs/protocol.md).
AES_KEY = os.environ.get("WENET_AES_KEY", "1234567890000000").encode()
UA = os.environ.get(
    "WENET_UA",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/156.0.0.0 Safari/537.36",
)
CONF_PATH = os.environ.get("WENET_CONF", "/etc/wenet-autologin.conf")
PROBE_URL = os.environ.get("WENET_PROBE", "http://connect.rom.miui.com/generate_204")
PROBE_URLS_EXTRA = [
    "http://www.baidu.com",
    "http://connectivitycheck.gstatic.com/generate_204",
]
INTERVAL = int(os.environ.get("WENET_INTERVAL", "60"))
OPENSSL = os.environ.get("WENET_OPENSSL", "openssl")
PORTAL_PORT = os.environ.get("WENET_PORTAL_PORT", "9090")
LOGPATH = os.environ.get("WENET_LOG")
LOG_MAX_BYTES = 200 * 1024


def log(msg):
    line = "%s %s" % (time.strftime("%Y-%m-%d %H:%M:%S"), msg)
    print(line, flush=True)
    if LOGPATH:
        try:
            if os.path.exists(LOGPATH) and os.path.getsize(LOGPATH) > LOG_MAX_BYTES:
                os.replace(LOGPATH, LOGPATH + ".1")
            with open(LOGPATH, "a", encoding="utf-8") as fh:
                fh.write(line + "\n")
        except OSError:
            pass


def load_conf(path):
    conf = {}
    with open(path, "r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            value = value.strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
                value = value[1:-1]
            conf[key.strip()] = value
    return conf


def aes_cbc(mode, data):
    """mode is '-e' or '-d'; key == iv == AES_KEY."""
    keyhex = binascii.hexlify(AES_KEY).decode()
    cmd = [OPENSSL, "enc", "-aes-128-cbc", "-K", keyhex, "-iv", keyhex,
           "-nosalt", mode]
    proc = subprocess.run(cmd, input=data, stdout=subprocess.PIPE,
                          stderr=subprocess.PIPE)
    if proc.returncode != 0:
        raise RuntimeError(
            "openssl failed: %s" % proc.stderr.decode(errors="replace").strip())
    return proc.stdout


def enc_json(obj):
    raw = json.dumps(obj, separators=(",", ":")).encode()
    return binascii.hexlify(aes_cbc("-e", raw)).decode()


def dec_hex(hexstr):
    raw = aes_cbc("-d", binascii.unhexlify(hexstr.strip().strip('"')))
    return json.loads(raw.decode("utf-8", "replace"))


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


no_redirect_opener = urllib.request.build_opener(NoRedirect)


def http(url, data=None, headers=None, follow=True, timeout=10):
    hdrs = {"User-Agent": UA}
    if headers:
        hdrs.update(headers)
    req = urllib.request.Request(url, data=data, headers=hdrs)
    opener = urllib.request.urlopen if follow else no_redirect_opener.open
    try:
        resp = opener(req, timeout=timeout)
        return resp.status, dict(resp.headers), resp.read()
    except urllib.error.HTTPError as exc:
        body = exc.read() if hasattr(exc, "read") else b""
        return exc.code, dict(exc.headers or {}), body


def probe_urls():
    return [PROBE_URL] + PROBE_URLS_EXTRA


def probe():
    """Return (is_online, captive_location). Tries several probe URLs."""
    last_location = None
    for url in probe_urls():
        try:
            status, headers, _ = http(url, follow=False, timeout=8)
        except Exception as exc:  # noqa: BLE001
            log("probe %s error: %s" % (url, exc))
            continue
        location = headers.get("Location") or headers.get("location")
        if status in (301, 302, 303, 307, 308) and location:
            if "userip=" in location:
                return False, location
            last_location = location
            continue
        if status in (200, 204):
            return True, None
        last_location = location
    return False, last_location


def extract_query(url):
    parsed = urllib.parse.urlsplit(url)
    return parsed.query or parsed.path.lstrip("?")


def portal_base_from(url, override=None):
    if override:
        return override.rstrip("/")
    parsed = urllib.parse.urlsplit(url)
    scheme = parsed.scheme or "http"
    netloc = parsed.netloc
    if not netloc:
        raise ValueError("cannot determine portal host from %r" % url)
    if ":" not in netloc:  # the portal is usually served on a dedicated port
        netloc += ":" + PORTAL_PORT
    return "%s://%s" % (scheme, netloc)


def get_redirect_url(base, query):
    """GET the portal page, return the redirectUrl value the server hands out."""
    url = "%s/?%s" % (base, query)
    try:
        _, headers, _ = http(url, follow=False, timeout=10)
    except Exception as exc:  # noqa: BLE001
        log("portal page error: %s" % exc)
        return None
    cookie = headers.get("Set-Cookie") or headers.get("set-cookie") or ""
    match = re.search(r"redirectUrl=([^;]+)", cookie)
    return match.group(1) if match else None


def login(conf, portal_base=None, query=None, force=False):
    phone = conf.get("PHONE") or conf.get("USER")
    password = conf.get("PASS") or conf.get("PASSWORD")
    if not phone or not password:
        log("FATAL: PHONE/PASS missing in %s" % CONF_PATH)
        return False
    online, location = (False, None) if force else probe()
    if online:
        log("already online, nothing to do")
        return True
    if not location:
        if not force:
            log("offline but no captive portal redirect found")
            return False
        if not portal_base:
            log("FATAL: --force needs --portal-base when there is no captive redirect")
            return False
        location = "%s/?%s" % (portal_base.rstrip("/"), query or "")
    if not query:
        query = extract_query(location)
    base = portal_base_from(location, portal_base)
    if "userip=" not in query:
        log("FATAL: unexpected portal redirect: %s" % location)
        return False
    redirect_url = get_redirect_url(base, query) or ("%s/?%s" % (base, query))
    payload = {
        "deviceType": "PC",
        "redirectUrl": redirect_url,
        "webAuthUser": phone,
        "webAuthPassword": password,
    }
    body = enc_json(payload).encode()
    try:
        status, _, raw = http(
            base + "/portal-conversion/api/v3/portal/connect",
            data=body,
            headers={
                "Content-Type": "application/json",
                "Accept": "application/json, text/plain, */*",
                "Origin": base,
                "Referer": base + "/auth",
            },
            timeout=15,
        )
    except Exception as exc:  # noqa: BLE001
        log("connect request failed: %s" % exc)
        return False
    try:
        answer = dec_hex(raw.decode())
    except Exception as exc:  # noqa: BLE001
        log("connect HTTP %s, cannot decrypt answer: %s (%r)"
            % (status, exc, raw[:120]))
        return False
    code = answer.get("statusCode")
    if code == 200:
        sess = answer.get("session") or {}
        ok, _ = probe()
        log("LOGIN OK session=%s verify_online=%s" % (sess.get("id"), ok))
        return ok
    desc = answer.get("errorDescription") or ""
    log("LOGIN FAILED http=%s error=%s desc=%s"
        % (status, answer.get("error"), desc))
    # "challenge already online"-style answers mean we are effectively online
    if "already" in desc.lower() or "online" in desc.lower():
        return True
    return False


def main():
    ap = argparse.ArgumentParser(
        description="Campus portal auto-login daemon (Huawei AC + WISPr / wenet).")
    ap.add_argument("--once", action="store_true",
                    help="run a single login attempt and exit")
    ap.add_argument("--check", action="store_true",
                    help="only report online/captive state and exit")
    ap.add_argument("--conf", default=CONF_PATH)
    ap.add_argument("--log", default=None,
                    help="append logs to this file (rotates at 200KB)")
    ap.add_argument("--portal-base", default=None,
                    help="override portal base URL (testing)")
    ap.add_argument("--query", default=None,
                    help="override portal redirect query string (testing)")
    ap.add_argument("--force", action="store_true",
                    help="skip the online probe and always login (testing)")
    args = ap.parse_args()
    global LOGPATH
    if args.log:
        LOGPATH = args.log
    if args.check:
        online, location = probe()
        log("online=%s redirect=%s" % (online, location))
        return 0
    conf = load_conf(args.conf)
    if args.once:
        return 0 if login(conf, args.portal_base, args.query, args.force) else 1
    fails = 0
    was_online = None
    log("daemon started (interval %ds)" % INTERVAL)
    while True:
        online, location = probe()
        if online:
            if was_online is not True:
                log("online")
            was_online = True
            delay = INTERVAL
        else:
            if was_online is not False:
                log("captive portal detected, logging in")
            was_online = False
            ok = login(conf, args.portal_base, args.query, args.force)
            if ok:
                fails = 0
                was_online = True if probe()[0] else None
                delay = INTERVAL
            elif location is None:
                fails = 0
                delay = 15
                log("network not ready, retry in 15s")
            else:
                fails = min(fails + 1, 6)
                delay = min(INTERVAL * (2 ** (fails - 1)), 600)
                log("login failed (attempt %d), retry in %ds" % (fails, delay))
        time.sleep(delay)


if __name__ == "__main__":
    sys.exit(main() or 0)
