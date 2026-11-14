#!/usr/bin/env python3
"""Standalone diagnostic for IQAir's (undocumented) cloud APIs. No Home Assistant needed.

It walks the login -> device list -> device state path one request at a time and
reports the HTTP status and a redacted body for every stage, so you can see exactly
where things break. It never retries, and it sends a device command only when you
ask for exactly one with --device and --command.

Credentials come ONLY from environment variables. Nothing secret is written to disk
or printed: tokens, cookies, ids, serial numbers, share codes and emails are redacted.

  IQAIR_COOKIE     The Cookie request header of a signed-in dashboard.iqair.com
                   session. Needed for accounts that sign in with Google/Apple/
                   Facebook (they have no password to send).
  IQAIR_EMAIL      Only for accounts that have an IQAir password. Set these together
  IQAIR_PASSWORD   with IQAIR_COOKIE to learn whether a password sign-in could renew
                   the dashboard session without a browser (stage 9).
  IQAIR_DEVICE     Optional. Part of a device name; picks the device for the state read.

Usage:
  python3 Tools/iqair_diag.py [--out REPORT.txt] [--fixtures DIR] [--probe-command-auth]
  python3 Tools/iqair_diag.py --device kitchen --command panel-lock-off [--out REPORT.txt]

Findings on 2026-11-14 (see the stages below for how each was established):
  - Email + password still works on website-api.airvisual.com and returns full state
    for ui2 (HealthPro XE) and wap (Atem Earth) devices.
  - The old gRPC command API rejects that login token; it needs an app token that the
    dashboard no longer publishes.
  - Commands therefore go through dashboard.iqair.com, which needs its own signed
    `session` cookie. That cookie cannot be derived from a password sign-in.

Protocol notes (as of 2026-11): the dashboard is a server-rendered React Router app.
The browser no longer talks to website-api.airvisual.com / cloud-api.iqair.io itself;
it talks to dashboard.iqair.com with a session cookie:
  GET  /personal/devices.data                                device list (turbo-stream)
  GET  /personal/devices/<id>.data                           one device, incl. remote state
  GET  /personal/devices/<id>/remote-control/stream          server-sent events
  POST /personal/devices/<id>/remote-control/command         JSON command (NOT used here)

Licensed AGPL-3.0, like the rest of this repository.
"""
import argparse
import base64
import http.client
import json
import os
import re
import ssl
import subprocess
import sys
import time
import urllib.parse

DASHBOARD_HOST = "dashboard.iqair.com"
WEBSITE_API_HOST = "website-api.airvisual.com"
CLOUD_API_HOST = "cloud-api.iqair.io"  # old gRPC-web command API (HTTP/2 only)
USER_AGENT = "iqair-diag/0.1 (personal diagnostic script; python http.client)"
PAUSE_SECONDS = 1.5  # between requests; this script makes fewer than ten in total
STREAM_SECONDS = 8

DEVICES_ROUTE = "routes/personal/devices"
DEVICE_DETAIL_ROUTE = "routes/personal/device-detail"

# Keys whose values identify the account, the device or its location.
SENSITIVE_KEYS = {
    "serialnumber", "sharecode", "email", "token", "logintoken", "password", "apikey",
    "key", "ip", "mac", "macaddress", "ssid", "latitude", "longitude", "lat", "lon",
    "lng", "coordinates", "address", "city", "state", "country", "zip", "userid",
    "ownerid", "avatar", "picture", "firstname", "lastname", "username", "url", "link",
    "api", "apiendpoints", "downloaddatalink", "stationurl",
}
# Any key containing one of these is treated the same way (wifiApSsid, ipAddress, ...).
SENSITIVE_STEMS = re.compile(
    r"ssid|ipaddr|macaddr|serial|registrationnumber|sharecode|token|email|password|"
    r"latitude|longitude|coordinates|picture",
    re.IGNORECASE,
)
ACCOUNT_KEYS = {"personalAccount", "organizationAccount"}


def is_sensitive(key):
    key = str(key)
    return key.lower() in SENSITIVE_KEYS or bool(SENSITIVE_STEMS.search(key))
# Cookies that are plainly analytics/ads and cannot be the session.
TRACKER_COOKIE = re.compile(
    r"^(_ga|_gcl|_fbp|_uet|_shopify|__hs|hubspotutk|messagesUtk|IR_|_pin|__adroll|"
    r"_mpxl|FPGSID|_rdt|__obref|globalcmpUUID|__grp|_tt|_clck|_clsk|_hj|ajs_)",
)

HEX24 = re.compile(r"[0-9a-fA-F]{24,}")  # database ids (24 hex) and longer hex identifiers
EMAIL = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")
JWT = re.compile(r"eyJ[\w-]{8,}\.[\w-]{8,}\.[\w-]{8,}")
LONG_TOKEN = re.compile(r"[A-Za-z0-9_\-%]{40,}")


class Report:
    """Collects output and scrubs every line before it is shown or saved."""

    def __init__(self):
        self.lines = []
        self.secrets = set()

    def add_secret(self, value):
        if isinstance(value, str) and len(value) >= 6:
            self.secrets.add(value)
            self.secrets.add(urllib.parse.quote(value, safe=""))

    def scrub_secrets(self, text):
        """Remove only the known secret values (used for fixtures, which keep fake ids)."""
        for secret in sorted(self.secrets, key=len, reverse=True):
            text = text.replace(secret, "<redacted>")
        return text

    def scrub(self, text):
        text = self.scrub_secrets(text)
        text = JWT.sub("<jwt>", text)
        text = EMAIL.sub("<email>", text)
        text = LONG_TOKEN.sub("<token>", text)
        return HEX24.sub("<id>", text)

    def say(self, text=""):
        line = self.scrub(str(text))
        self.lines.append(line)
        print(line, flush=True)

    def stage(self, title):
        self.say()
        self.say("=== " + title)

    def save(self, path):
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as handle:
            handle.write("\n".join(self.lines) + "\n")


class Response:
    def __init__(self, status, headers, set_cookie_names, text):
        self.status = status
        self.headers = headers
        self.set_cookie_names = set_cookie_names
        self.text = text


def request(host, method, path, headers=None, body=None, stream_seconds=None):
    """One HTTPS request to a pinned IQAir host. Redirects are never followed."""
    if host not in (DASHBOARD_HOST, WEBSITE_API_HOST):
        raise ValueError("refusing to talk to unexpected host " + host)
    conn = http.client.HTTPSConnection(host, timeout=25, context=ssl.create_default_context())
    send_headers = {"User-Agent": USER_AGENT, "Accept": "*/*"}
    send_headers.update(headers or {})
    try:
        conn.request(method, path, body=body, headers=send_headers)
        raw = conn.getresponse()
        cookie_names = sorted({c.split("=", 1)[0].strip() for c in raw.headers.get_all("Set-Cookie") or []})
        response_headers = {k.lower(): v for k, v in raw.headers.items() if k.lower() != "set-cookie"}
        if stream_seconds is None:
            text = raw.read().decode("utf-8", "replace")
        else:
            text = _read_stream(conn, raw, stream_seconds)
        return Response(raw.status, response_headers, cookie_names, text)
    finally:
        conn.close()
        time.sleep(PAUSE_SECONDS)


def _read_stream(conn, raw, seconds):
    """Read a server-sent-event response for a few seconds, then hang up."""
    deadline = time.monotonic() + seconds
    chunks = []
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        conn.sock.settimeout(remaining)
        try:
            line = raw.readline()
        except (TimeoutError, OSError):
            break
        if not line:
            break
        chunks.append(line.decode("utf-8", "replace"))
    return "".join(chunks)


def parse_sse(text):
    events, event, data = [], "message", []
    for line in text.splitlines() + [""]:
        if line.startswith(":"):
            continue
        if line == "":
            if data or event != "message":
                events.append({"event": event, "data": "\n".join(data)})
            event, data = "message", []
        elif line.startswith("event:"):
            event = line[6:].strip()
        elif line.startswith("data:"):
            data.append(line[5:].lstrip())
    return events


# --- turbo-stream (React Router "single fetch" .data responses) ---------------------

_SPECIAL = {-1: None, -2: float("nan"), -3: float("-inf"), -4: -0.0, -5: None, -6: float("inf"), -7: None}


def decode_turbo_stream(text):
    """Decode the flattened value table React Router sends for loader data.

    The first line is a JSON array. Element 0 is the root. Objects are stored as
    {"_<index of key>": <index of value>}, arrays as lists of indices, and negative
    indices are special values (null, undefined, NaN, ...).
    """
    values = json.loads(text.split("\n", 1)[0])
    if not isinstance(values, list):
        raise ValueError("not a turbo-stream body")
    cache = {}

    def hydrate(index):
        if index < 0:
            return _SPECIAL.get(index)
        if index in cache:
            return cache[index]
        value = values[index]
        if isinstance(value, dict):
            out = cache[index] = {}
            for key_ref, value_index in value.items():
                out[values[int(key_ref[1:])]] = hydrate(value_index)
            return out
        if isinstance(value, list):
            if value and isinstance(value[0], str):  # typed value such as ["D", ms]
                out = cache[index] = {"__type": value[0], "args": value[1:]}
                return out
            out = cache[index] = []
            out.extend(hydrate(i) for i in value)
            return out
        cache[index] = value
        return value

    return hydrate(0)


def sanitize_turbo_stream(text):
    """Return the same turbo-stream body with identifying values replaced.

    Works on the flat table so the result keeps the server's exact structure and can
    be used as a test fixture.
    """
    values = json.loads(text.split("\n", 1)[0])
    replace = {}
    seen = set()

    def walk(index, redact_all):
        if index < 0 or (index, redact_all) in seen:
            return
        seen.add((index, redact_all))
        value = values[index]
        if isinstance(value, dict):
            for key_ref, value_index in value.items():
                key = values[int(key_ref[1:])]
                sensitive = redact_all or key in ACCOUNT_KEYS or is_sensitive(key)
                if value_index >= 0 and key == "timezone" and isinstance(values[value_index], str):
                    replace[value_index] = "Etc/UTC"
                elif value_index >= 0 and sensitive:
                    target = values[value_index]
                    if isinstance(target, str) and target:
                        replace[value_index] = "REDACTED-%d" % (len(replace) + 1)
                    elif isinstance(target, float):
                        replace[value_index] = 0.5
                walk(value_index, sensitive)
        elif isinstance(value, list) and not (value and isinstance(value[0], str)):
            for item in value:
                walk(item, redact_all)

    walk(0, False)
    fake_ids = {}

    def fake_id(match):
        real = match.group(0).lower()
        fake_ids.setdefault(real, "%024x" % (len(fake_ids) + 1))
        return fake_ids[real]

    out = []
    for index, value in enumerate(values):
        if index in replace:
            value = replace[index]
        elif isinstance(value, str):
            value = EMAIL.sub("user@example.com", HEX24.sub(fake_id, value))
        out.append(value)
    return json.dumps(out, ensure_ascii=False, separators=(",", ":"))


def redact(value, key=None):
    """Redact a decoded payload for display."""
    if isinstance(value, dict):
        return {k: ("<redacted>" if k in ACCOUNT_KEYS else redact(v, k)) for k, v in value.items()}
    if isinstance(value, list):
        return [redact(v, key) for v in value]
    if key is not None and is_sensitive(key) and value not in (None, "", [], {}):
        return "<redacted>"
    return value


def show_json(report, label, value, limit=6000):
    text = json.dumps(redact(value), indent=2, ensure_ascii=False, default=str)
    if len(text) > limit:
        text = text[:limit] + "\n... (truncated)"
    report.say(label)
    report.say(text)


def show_response(report, method, host, path, response, body_chars=400):
    report.say("%s https://%s%s" % (method, host, path))
    report.say("HTTP %s  content-type=%s" % (response.status, response.headers.get("content-type")))
    if response.headers.get("location"):
        report.say("location: " + response.headers["location"])
    if response.headers.get("x-vercel-mitigated"):
        report.say("x-vercel-mitigated: " + response.headers["x-vercel-mitigated"] + "  (bot protection)")
    if response.set_cookie_names:
        report.say("set-cookie names: " + ", ".join(response.set_cookie_names))
    if body_chars:
        body = " ".join(response.text.split())
        report.say("body: " + (body[:body_chars] + (" ..." if len(body) > body_chars else "") if body else "(empty)"))


def measurements(block):
    return {k: v.get("value") for k, v in (block or {}).items() if isinstance(v, dict) and v.get("value") is not None}


def decode_route(report, response, route):
    """Decode a .data response and return one route's loader data, or None."""
    try:
        decoded = decode_turbo_stream(response.text)
    except (ValueError, IndexError, KeyError, TypeError) as err:
        report.say("could not decode turbo-stream body: %r" % (err,))
        return None
    if not isinstance(decoded, dict):
        report.say("unexpected turbo-stream root: " + type(decoded).__name__)
        return None
    if route not in decoded:
        report.say("route %r missing; routes present: %s" % (route, ", ".join(map(str, decoded))))
        show_json(report, "decoded body:", decoded, limit=800)
        return None
    entry = decoded[route]
    if isinstance(entry, dict) and "error" in entry:
        show_json(report, "route returned an error:", entry, limit=800)
        return None
    return entry.get("data") if isinstance(entry, dict) else None


# --- stages -------------------------------------------------------------------------

def stage_old_token_scrape(report):
    report.stage("1. What the existing integration does after sign-in: scrape dashboard JS for cloudApiAuthToken")
    response = request(DASHBOARD_HOST, "GET", "/")
    show_response(report, "GET", DASHBOARD_HOST, "/", response, body_chars=200)
    has_bundle = bool(re.search(r'src="/?main\.[a-f0-9]+\.js"', response.text))
    report.say("main.<hash>.js bundle present: %s" % has_bundle)
    if response.status in (301, 302, 303, 307, 308) or not has_bundle:
        report.say("RESULT: FAILS HERE. The page that used to expose the gRPC bearer token is now a login redirect.")
    else:
        report.say("RESULT: ok (bundle still served)")


def stage_legacy_signin(report, email, password):
    report.stage("2. Legacy sign-in: POST website-api /v2/auth/signin/by/email")
    body = json.dumps({"email": email, "password": password})
    path = "/v2/auth/signin/by/email"
    response = request(WEBSITE_API_HOST, "POST", path, {"Content-Type": "application/json"}, body)
    try:
        data = json.loads(response.text)
    except ValueError:
        data = None
    if isinstance(data, dict):
        for secret_key in ("loginToken", "id", "token"):
            report.add_secret(data.get(secret_key))
    show_response(report, "POST", WEBSITE_API_HOST, path, response, body_chars=0)
    if isinstance(data, dict):
        report.say("body keys: " + ", ".join(sorted(data)))
        if response.status >= 400:
            show_json(report, "body:", data, limit=600)
    else:
        report.say("body: " + " ".join(response.text.split())[:300])
    if response.status != 200 or not isinstance(data, dict) or not data.get("loginToken"):
        report.say("RESULT: FAILS HERE (no loginToken).")
        return None
    report.say("RESULT: ok, got a loginToken and user id.")
    return data


def stage_legacy_devices(report, signin):
    report.stage("3. Legacy device list: GET website-api /v2/users/<id>/devices")
    query = urllib.parse.urlencode(
        {"page": "1", "perPage": "15", "units.system": "imperial", "AQI": "US", "language": "en"}
    )
    path = "/v2/users/%s/devices?%s" % (signin["id"], query)
    response = request(WEBSITE_API_HOST, "GET", path, {"x-login-token": signin["loginToken"]})
    show_response(report, "GET", WEBSITE_API_HOST, path, response, body_chars=0)
    try:
        devices = json.loads(response.text)
    except ValueError:
        devices = None
    if response.status != 200 or not isinstance(devices, list):
        report.say("body: " + " ".join(response.text.split())[:400])
        report.say("RESULT: FAILS HERE.")
        return None
    report.say("devices returned: %d" % len(devices))
    for device in devices:
        if not isinstance(device, dict):
            continue
        report.say()
        report.say(
            "--- %r model=%s label=%r connected=%s"
            % (device.get("name"), device.get("model"), device.get("modelLabel"), device.get("isConnected"))
        )
        report.say("top-level keys: " + ", ".join(sorted(device)))
        for key in ("remote", "current", "featureSet", "filterMaintenance", "performance"):
            if device.get(key) not in (None, {}, []):
                show_json(report, key + ":", device[key], limit=2500)
    report.say()
    report.say("RESULT: ok." if devices else "RESULT: 200 but no devices.")
    return devices


def stage_token_on_dashboard(report, signin):
    report.stage("3b. Does the new dashboard accept the API login token directly? (a guess; one request)")
    token = signin["loginToken"]
    names = ("loginToken", "login_token", "x-login-token", "token", "session", "__session")
    headers = {
        "x-login-token": token,
        "Authorization": "Bearer " + token,
        "Cookie": "; ".join("%s=%s" % (name, token) for name in names),
    }
    try:
        response = request(DASHBOARD_HOST, "GET", "/personal/devices.data", headers)
    except ValueError:
        report.say("RESULT: could not try (the token has characters a header cannot carry).")
        return
    show_response(report, "GET", DASHBOARD_HOST, "/personal/devices.data", response, body_chars=0)
    if response.status == 200 and DEVICES_ROUTE in response.text:
        report.say("RESULT: yes. The dashboard API works with the password login's token.")
    else:
        report.say("RESULT: no. The dashboard only accepts its own session cookie.")


def stage_unauthenticated(report):
    report.stage("4. New dashboard API without a session (how an expired login looks)")
    response = request(DASHBOARD_HOST, "GET", "/personal/devices.data")
    show_response(report, "GET", DASHBOARD_HOST, "/personal/devices.data", response, body_chars=300)


def stage_device_list(report, cookie, label="5. New dashboard API: device list"):
    report.stage(label)
    path = "/personal/devices.data"
    response = request(DASHBOARD_HOST, "GET", path, {"Cookie": cookie})
    show_response(report, "GET", DASHBOARD_HOST, path, response, body_chars=0)
    if response.status != 200:
        report.say("body: " + " ".join(response.text.split())[:400])
        report.say("RESULT: FAILS HERE.")
        return None, response
    data = decode_route(report, response, DEVICES_ROUTE)
    devices = (data or {}).get("data") if isinstance(data, dict) else None
    if not isinstance(devices, list):
        report.say("RESULT: FAILS HERE (200 but no device list; the session is probably not accepted).")
        return None, response
    report.say("devices returned: %d (totalCount=%s)" % (len(devices), data.get("totalCount")))
    for device in devices:
        info = device.get("baseInfo") or {}
        filters = ["%s %s%%" % (f.get("filterTitle"), f.get("healthPercent")) for f in device.get("filters") or []]
        report.say(
            "  - %r model=%s label=%r variation=%s status=%s mode=%s readings=%s filters=%s"
            % (
                info.get("name"), info.get("model"), info.get("modelLabel"), info.get("modelVariation"),
                device.get("connectionStatus"), device.get("mode"), measurements(device), filters,
            )
        )
    report.say("RESULT: ok.")
    return devices, response


def stage_device_state(report, cookie, device):
    name = (device.get("baseInfo") or {}).get("name")
    report.stage("6. New dashboard API: state of %r" % name)
    path = "/personal/devices/%s.data" % urllib.parse.quote(device["id"], safe="")
    response = request(DASHBOARD_HOST, "GET", path, {"Cookie": cookie})
    show_response(report, "GET", DASHBOARD_HOST, path, response, body_chars=0)
    if response.status != 200:
        report.say("body: " + " ".join(response.text.split())[:400])
        report.say("RESULT: FAILS HERE.")
        return response
    detail = decode_route(report, response, DEVICE_DETAIL_ROUTE)
    if not isinstance(detail, dict):
        report.say("RESULT: FAILS HERE (no device detail).")
        return response
    report.say("detail keys: " + ", ".join(detail))
    show_json(report, "baseInfo:", detail.get("baseInfo"))
    show_json(report, "remoteControlInitialState:", detail.get("remoteControlInitialState"))
    show_json(report, "current (readings that have a value):", measurements(detail.get("current")))
    show_json(report, "filterMaintenances:", detail.get("filterMaintenances"))
    show_json(report, "airCleaningPerformance:", detail.get("airCleaningPerformance"))
    report.say("isPurifier=%s supportedFilters=%s" % (detail.get("isPurifier"), detail.get("supportedFilters")))
    report.say("RESULT: ok.")
    return response


def stage_stream(report, cookie, device):
    name = (device.get("baseInfo") or {}).get("name")
    report.stage("7. New dashboard API: live state stream of %r (%ds, read-only)" % (name, STREAM_SECONDS))
    path = "/personal/devices/%s/remote-control/stream" % urllib.parse.quote(device["id"], safe="")
    headers = {"Cookie": cookie, "Accept": "text/event-stream", "Cache-Control": "no-cache"}
    response = request(DASHBOARD_HOST, "GET", path, headers, stream_seconds=STREAM_SECONDS)
    show_response(report, "GET", DASHBOARD_HOST, path, response, body_chars=0)
    events = parse_sse(response.text) if response.status == 200 else []
    for event in events:
        report.say("  %s: %s" % (event["event"], event["data"]))
    if response.status != 200:
        report.say("body: " + " ".join(response.text.split())[:400])
    report.say("RESULT: ok, %d events." % len(events) if events else "RESULT: FAILS HERE (no events).")
    return events


def stage_cookie_narrowing(report, cookie):
    report.stage("8. Which cookies carry the session (names only)")
    pairs = [p.strip() for p in cookie.split(";") if "=" in p]
    names = [p.split("=", 1)[0] for p in pairs]
    candidates = [p for p in pairs if not TRACKER_COOKIE.match(p.split("=", 1)[0])]
    report.say("cookies supplied: %d; analytics/ads cookies ignored: %d" % (len(names), len(names) - len(candidates)))
    report.say("candidate session cookies: " + (", ".join(p.split("=", 1)[0] for p in candidates) or "(none)"))
    if not candidates or len(candidates) == len(pairs):
        return
    response = request(DASHBOARD_HOST, "GET", "/personal/devices.data", {"Cookie": "; ".join(candidates)})
    ok = response.status == 200 and DEVICES_ROUTE in response.text
    report.say("device list with only the candidates: HTTP %s -> %s" % (response.status, "works" if ok else "does not work"))


def stage_token_vs_cookie(report, signin, cookie):
    """Tell whether a password sign-in could renew the dashboard session unattended."""
    report.stage("9. Could a password sign-in renew the dashboard session? (local comparison, sends nothing)")
    token = signin.get("loginToken") or ""
    user_id = str(signin.get("id") or "")
    exact, unsigned, signed = [], [], []
    for pair in cookie.split(";"):
        if "=" not in pair or not token:
            continue
        name, value = pair.strip().split("=", 1)
        decoded = urllib.parse.unquote(value)
        parts = decoded.split(".")
        unpacked = ""
        try:
            unpacked = base64.urlsafe_b64decode(parts[0] + "=" * (-len(parts[0]) % 4)).decode("utf-8", "replace")
        except ValueError:
            pass
        if token in (value, decoded):
            exact.append(name)
            report.say("  %s: is exactly the API login token" % name)
        elif user_id and user_id in (value, decoded):
            report.say("  %s: is exactly the user id" % name)
        elif token in decoded or token in unpacked or (user_id and (user_id in decoded or user_id in unpacked)):
            wrapper = unpacked if (token in unpacked or (user_id and user_id in unpacked)) else decoded
            try:
                keys = sorted(json.loads(wrapper)) if isinstance(json.loads(wrapper), dict) else []
            except ValueError:
                keys = []
            is_signed = wrapper is unpacked and len(parts) > 1
            (signed if is_signed else unsigned).append(name)
            report.say(
                "  %s: wraps the login token/user id (%s%s; keys: %s)"
                % (
                    name, "base64 JSON" if wrapper is unpacked else "plain text/JSON",
                    ", followed by a signature" if is_signed else ", no signature", ", ".join(keys) or "n/a",
                )
            )
    if exact:
        report.say("RESULT: yes. Email + password can mint the session cookie, so reauth can be automatic.")
    elif unsigned:
        report.say("RESULT: probably yes. The session is an unsigned wrapper around values a password sign-in returns.")
    elif signed:
        report.say("RESULT: probably not. The token sits inside a server-signed cookie that a client cannot produce.")
    else:
        report.say("RESULT: no. The session cookie is unrelated to the API login token.")


def describe_session(report, cookie):
    """Show the field names inside the signed session cookie, and any dates. Never values."""
    session = next((p.strip()[8:] for p in cookie.split(";") if p.strip().startswith("session=")), None)
    if not session:
        return
    head = urllib.parse.unquote(session).split(".")[0]
    try:
        data = json.loads(base64.urlsafe_b64decode(head + "=" * (-len(head) % 4)))
    except ValueError:
        report.say("session cookie contents: not readable")
        return

    def as_date(value):
        if isinstance(value, str) and re.match(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}", value):
            return value[:19]
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            seconds = value / 1000 if 1.5e12 < value < 2.2e12 else value
            if 1.5e9 < seconds < 2.2e9:
                return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(seconds))
        return None

    def walk(value, path):
        if isinstance(value, dict):
            for key, child in value.items():
                walk(child, path + [str(key)])
        else:
            kind = "list[%d]" % len(value) if isinstance(value, list) else type(value).__name__
            when = as_date(value)
            report.say("  %s: %s%s" % (".".join(path) or "(root)", kind, " = date %s" % when if when else ""))

    report.say("session cookie contents (field names and dates only):")
    walk(data, [])


def _curl_grpc(service, method, token):
    """POST an EMPTY grpc-web message (no serial number, so no device is addressed)."""
    config = [
        'url = "https://%s/%s/%s"' % (CLOUD_API_HOST, service, method),
        'request = "POST"',
        'user-agent = "%s"' % USER_AGENT,
        'header = "Content-Type: application/grpc-web-text"',
        'header = "Accept: application/grpc-web-text"',
        'header = "X-User-Agent: grpc-web-javascript/0.1"',
        'data = "AAAAAAA="',  # base64 of a zero-length message frame
        'dump-header = "/dev/stderr"',
        "max-time = 20",
    ]
    if token:
        config.append('header = "Authorization: Bearer %s"' % token.replace("\\", "\\\\").replace('"', '\\"'))
    done = subprocess.run(
        ["curl", "-sS", "--http2", "-K", "-"], input="\n".join(config) + "\n", capture_output=True, text=True, timeout=40
    )
    time.sleep(PAUSE_SECONDS)
    head = done.stderr
    status = re.search(r"^HTTP/\S+ (\d+)", head, re.MULTILINE)
    grpc_status = re.search(r"^grpc-status:\s*(\S+)", head, re.MULTILINE | re.IGNORECASE)
    grpc_message = re.search(r"^grpc-message:\s*(.*)$", head, re.MULTILINE | re.IGNORECASE)
    frames = []
    for chunk in re.sub(r"(=+)([A-Za-z0-9+/])", r"\1\n\2", done.stdout.strip()).split("\n"):
        try:
            raw = base64.b64decode(chunk) if chunk else b""
        except ValueError:
            raw = b""
        if len(raw) >= 5 and raw[0] == 0x80:
            trailer = raw[5:].decode("utf-8", "replace")
            grpc_status = grpc_status or re.search(r"grpc-status:\s*(\S+)", trailer)
            grpc_message = grpc_message or re.search(r"grpc-message:\s*(.*)", trailer)
        elif len(raw) >= 5:
            frames.append(raw[5:].hex(" ") or "(empty)")
    return {
        "http": status.group(1) if status else "none (%s)" % " ".join(head.split())[:120],
        "grpc": grpc_status.group(1) if grpc_status else None,
        "message": urllib.parse.unquote(grpc_message.group(1).strip()) if grpc_message else "",
        "data": frames,
    }


GRPC_CODES = {"0": "OK", "3": "INVALID_ARGUMENT", "5": "NOT_FOUND", "7": "PERMISSION_DENIED",
              "12": "UNIMPLEMENTED", "13": "INTERNAL", "14": "UNAVAILABLE", "16": "UNAUTHENTICATED"}


def stage_command_auth_probe(report, signin):
    report.stage("3c. Does the old command API accept the password login's token? (empty messages, no device addressed)")
    token = signin["loginToken"]
    probes = (
        ("unknown service, no auth (baseline)", "grpc.zzz.v1.ZZZService", None),
        ("HealthPro service, no auth", "grpc.ui2.v1.UI2Service", None),
        ("HealthPro service, login token", "grpc.ui2.v1.UI2Service", token),
        ("guessed Atem Earth service, no auth", "grpc.wap.v1.WAPService", None),
        ("guessed Atem Earth service, login token", "grpc.wap.v1.WAPService", token),
    )
    for label, service, bearer in probes:
        try:
            result = _curl_grpc(service, "SetLightIndicator", bearer)
        except (OSError, subprocess.SubprocessError) as err:
            report.say("  %s: could not run curl (%s)" % (label, type(err).__name__))
            return
        code = result["grpc"]
        report.say(
            "  %-42s HTTP %s  grpc-status=%s %s  %s%s"
            % (label + ":", result["http"], code, GRPC_CODES.get(str(code), ""), result["message"][:120],
               ("  data=" + "; ".join(result["data"])) if result["data"] else "")
        )
    report.say("How to read it: UNAUTHENTICATED/PERMISSION_DENIED or HTTP 401/403 with the login token means the")
    report.say("old command API will not take it. INVALID_ARGUMENT/NOT_FOUND/INTERNAL means the token got past auth.")


# --- single command test (only runs when --device and --command are both given) ------

COMMANDS = {
    "panel-lock-off": {"command": "setPanelLock", "enabled": False},
    "panel-lock-on": {"command": "setPanelLock", "enabled": True},
    "light-on": {"command": "setLight", "enabled": True},
    "light-off": {"command": "setLight", "enabled": False},
    "smart-on": {"command": "setAutoMode", "enabled": True},
    "smart-off": {"command": "setAutoMode", "enabled": False},
    "power-on": {"command": "setPowerMode", "power": "on"},
    "power-standby": {"command": "setPowerMode", "power": "standby"},
}
for _level in range(1, 9):
    COMMANDS["fan-speed-%d" % _level] = {"command": "setFanSpeed", "level": _level}
REMOTE_FIELDS = ("powerMode", "speedLevel", "speedPercent", "autoModeEnabled", "autoModeProfile",
                 "lightIndicatorEnabled", "isLocksEnabled")


def _legacy_devices(signin):
    query = urllib.parse.urlencode(
        {"page": "1", "perPage": "15", "units.system": "imperial", "AQI": "US", "language": "en"}
    )
    response = request(WEBSITE_API_HOST, "GET", "/v2/users/%s/devices?%s" % (signin["id"], query),
                       {"x-login-token": signin["loginToken"]})
    return response, (json.loads(response.text) if response.status == 200 else None)


def _remote_summary(device):
    remote = device.get("remote") or {}
    return ", ".join("%s=%s" % (field, remote.get(field)) for field in REMOTE_FIELDS)


def command_test(report, email, password, cookie, wanted, command_name):
    """Send exactly one command to exactly one device, then watch the old API reflect it."""
    payload = COMMANDS[command_name]
    report.stage("Command test: %s -> device matching %r" % (json.dumps(payload), wanted))
    if not (email and password and cookie):
        report.say("STOPPED: needs IQAIR_EMAIL, IQAIR_PASSWORD and IQAIR_COOKIE. Nothing was sent.")
        return
    signin = stage_legacy_signin(report, email, password)
    if not signin:
        return
    response, devices = _legacy_devices(signin)
    if not isinstance(devices, list):
        report.say("STOPPED: old device list returned HTTP %s. Nothing was sent." % response.status)
        return
    matches = [d for d in devices if wanted in str(d.get("name", "")).lower()]
    if len(matches) != 1:
        report.say("STOPPED: %r matches %d devices (%s). Nothing was sent."
                   % (wanted, len(matches), ", ".join(repr(d.get("name")) for d in devices)))
        return
    device = matches[0]
    max_level = (device.get("remote") or {}).get("maxSpeedLevel") or 6
    if payload["command"] == "setFanSpeed" and payload["level"] > max_level:
        report.say("STOPPED: %r has %s fan levels. Nothing was sent." % (device.get("name"), max_level))
        return
    report.say("device: %r model=%s connected=%s" % (device.get("name"), device.get("model"), device.get("isConnected")))
    report.say("before:  " + _remote_summary(device))

    session = next((p.strip() for p in cookie.split(";") if p.strip().startswith("session=")), None)
    report.stage("Does the 'session' cookie work on its own (without the other cookies)?")
    check = request(DASHBOARD_HOST, "GET", "/personal/devices.data", {"Cookie": session or cookie})
    session_ok = bool(session) and check.status == 200 and DEVICES_ROUTE in check.text
    report.say("HTTP %s -> %s" % (check.status, "yes" if session_ok else "no, using the full cookie header"))
    describe_session(report, cookie)
    use_cookie = session if session_ok else cookie

    path = "/personal/devices/%s/remote-control/command" % urllib.parse.quote(str(device["id"]), safe="")
    headers = {"Cookie": use_cookie, "Content-Type": "application/json", "Accept": "application/json"}
    report.stage("POST the command (device id taken from the old API)")
    result = request(DASHBOARD_HOST, "POST", path, headers, json.dumps(payload))
    show_response(report, "POST", DASHBOARD_HOST, path, result, body_chars=300)
    if result.status in (400, 403) and '"ok"' not in result.text:
        report.say("retrying ONCE with an Origin header, in case the server checks it for form posts")
        headers["Origin"] = "https://" + DASHBOARD_HOST
        result = request(DASHBOARD_HOST, "POST", path, headers, json.dumps(payload))
        show_response(report, "POST", DASHBOARD_HOST, path, result, body_chars=300)
    try:
        answer = json.loads(result.text)
    except ValueError:
        answer = None
    if not (result.status == 200 and isinstance(answer, dict) and answer.get("ok")):
        report.say("RESULT: the command was NOT accepted. Not retrying.")
        return
    report.say("RESULT: accepted, value=%s" % json.dumps(answer.get("value")))

    report.stage("How quickly does the old API show it?")
    sent = time.monotonic()
    for wait_until in (3, 10, 20, 40):
        time.sleep(max(0.0, wait_until - (time.monotonic() - sent)))
        response, devices = _legacy_devices(signin)
        current = next((d for d in devices or [] if d.get("id") == device["id"]), None)
        report.say("after %2ds: %s" % (round(time.monotonic() - sent), _remote_summary(current) if current
                                       else "HTTP %s" % response.status))


def write_legacy_fixture(report, directory, devices):
    """Save the old API's device list with identifying values replaced."""
    os.makedirs(directory, exist_ok=True)
    fake_ids = {}

    def fake_id(match):
        fake_ids.setdefault(match.group(0).lower(), "%024x" % (len(fake_ids) + 1))
        return fake_ids[match.group(0).lower()]

    text = json.dumps(redact(devices), indent=2, ensure_ascii=False, default=str)
    text = EMAIL.sub("user@example.com", HEX24.sub(fake_id, report.scrub_secrets(text)))
    text = re.sub(r'("timezone":\s*")[^"]*', r"\1Etc/UTC", text)
    with open(os.path.join(directory, "legacy_devices.json"), "w") as handle:
        handle.write(text + "\n")
    report.say("sanitized fixture written: " + os.path.join(directory, "legacy_devices.json"))


def write_fixtures(report, directory, list_response, detail_response, events):
    os.makedirs(directory, exist_ok=True)
    written = []
    for filename, response in (("devices.data.json", list_response), ("device_detail.data.json", detail_response)):
        if response is not None and response.status == 200:
            with open(os.path.join(directory, filename), "w") as handle:
                handle.write(report.scrub_secrets(sanitize_turbo_stream(response.text)) + "\n")
            written.append(filename)
    if events:
        with open(os.path.join(directory, "stream_events.json"), "w") as handle:
            handle.write(report.scrub_secrets(json.dumps(events, indent=2)) + "\n")
        written.append("stream_events.json")
    report.say()
    report.say("sanitized fixtures written to %s: %s" % (directory, ", ".join(written) or "(none)"))


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--out", help="also save the redacted report to this file")
    parser.add_argument("--fixtures", help="directory for sanitized payloads (for unit tests)")
    parser.add_argument(
        "--probe-command-auth",
        action="store_true",
        help="also send five EMPTY requests (no serial number, so no device is addressed) to the old "
        "command API to learn whether it accepts the password login's token",
    )
    parser.add_argument("--device", help="command test: part of the name of the ONE device to command")
    parser.add_argument("--command", choices=sorted(COMMANDS), help="command test: the ONE command to send")
    args = parser.parse_args()
    if bool(args.device) != bool(args.command):
        parser.error("--device and --command must be given together")

    email = os.environ.get("IQAIR_EMAIL", "").strip()
    password = os.environ.get("IQAIR_PASSWORD", "")
    cookie = os.environ.get("IQAIR_COOKIE", "").strip()
    if cookie.lower().startswith("cookie:"):
        cookie = cookie[7:].strip()
    if cookie and not re.fullmatch(r"[\x21-\x7e]+=[\x20-\x7e]*", cookie):
        # Never echo the value: it may be the wrong clipboard content.
        print(
            "IQAIR_COOKIE does not look like a Cookie header (%d characters, %d lines). "
            "It must be one line of name=value pairs. Nothing was sent."
            % (len(cookie), len(cookie.splitlines())),
            file=sys.stderr,
        )
        return 2
    wanted = os.environ.get("IQAIR_DEVICE", "").strip().lower()

    report = Report()
    for secret in (email, password, cookie):
        report.add_secret(secret)
    for pair in cookie.split(";"):
        if "=" in pair:
            report.add_secret(pair.split("=", 1)[1].strip())

    report.say("IQAir cloud diagnostic  " + time.strftime("%Y-%m-%d %H:%M:%S %z"))
    report.say(
        "credentials present: IQAIR_EMAIL=%s IQAIR_PASSWORD=%s IQAIR_COOKIE=%s"
        % tuple("yes" if v else "no" for v in (email, password, cookie))
    )

    signin = None
    if args.command:
        try:
            command_test(report, email, password, cookie, args.device.strip().lower(), args.command)
        except (OSError, http.client.HTTPException) as err:
            report.say("NETWORK ERROR, stopping (no retry): %r" % (err,))
        except Exception as err:  # no traceback: it could carry header values
            report.say("UNEXPECTED ERROR, stopping: %s: %s" % (type(err).__name__, str(err)[:200]))
        finally:
            if args.out:
                report.save(args.out)
                print("report saved to " + args.out)
        return 0
    try:
        stage_old_token_scrape(report)
        if email and password:
            signin = stage_legacy_signin(report, email, password)
            if signin:
                legacy_devices = stage_legacy_devices(report, signin)
                if args.fixtures and legacy_devices:
                    write_legacy_fixture(report, args.fixtures, legacy_devices)
                stage_token_on_dashboard(report, signin)
                if args.probe_command_auth:
                    stage_command_auth_probe(report, signin)
        else:
            report.stage("2-3. Legacy sign-in and device list")
            report.say("SKIPPED: needs IQAIR_EMAIL and IQAIR_PASSWORD (not available for Google sign-in accounts).")

        stage_unauthenticated(report)

        if not cookie:
            report.stage("5-8. New dashboard API with a session")
            report.say("SKIPPED: set IQAIR_COOKIE to test the API the dashboard uses today.")
        else:
            devices, list_response = stage_device_list(report, cookie)
            if devices:
                device = next(
                    (d for d in devices if wanted and wanted in str((d.get("baseInfo") or {}).get("name", "")).lower()),
                    devices[0],
                )
                detail_response = stage_device_state(report, cookie, device)
                events = stage_stream(report, cookie, device)
                stage_cookie_narrowing(report, cookie)
                if args.fixtures:
                    write_fixtures(report, args.fixtures, list_response, detail_response, events)
            if signin:
                stage_token_vs_cookie(report, signin, cookie)
    except (OSError, http.client.HTTPException) as err:
        report.say("NETWORK ERROR, stopping (no retry): %r" % (err,))
    except Exception as err:  # no traceback: it could carry header values
        report.say("UNEXPECTED ERROR, stopping: %s: %s" % (type(err).__name__, str(err)[:200]))
    finally:
        if args.out:
            report.save(args.out)
            print("report saved to " + args.out)


if __name__ == "__main__":
    sys.exit(main())
