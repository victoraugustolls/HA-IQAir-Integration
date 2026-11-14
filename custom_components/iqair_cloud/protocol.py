"""Everything this integration knows about IQAir's undocumented cloud APIs.

If IQAir changes something, this is the file to fix. It is pure: no network, no
Home Assistant imports. It builds requests and interprets responses; api.py sends
them. `Tools/iqair_diag.py` re-checks every assumption below against the live service.

State of the service, measured on 2026-11-14:

* READING uses the old website API with an email + password sign-in:
    POST https://website-api.airvisual.com/v2/auth/signin/by/email  -> id, loginToken
    GET  https://website-api.airvisual.com/v2/users/<id>/devices    (x-login-token)
  It returns full state for HealthPro XE ("ui2") and Atem Earth ("wap") purifiers.

* CONTROL uses the dashboard, a server-rendered app that only accepts its own
  signed `session` cookie (set by the login page, which is bot-protected):
    POST https://dashboard.iqair.com/personal/devices/<id>/remote-control/command
  Device ids are the same on both APIs. A missing or expired session answers with a
  redirect to the login page instead of an error status.

* The old gRPC command API (cloud-api.iqair.io) still exists but needs an app token
  the dashboard stopped publishing, and it rejects the sign-in's loginToken.

The device-state field names and the numeric power/profile codes come from the 1.x
versions of this integration.
"""
from __future__ import annotations

import dataclasses
import json
import re
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote

WEBSITE_API = "https://website-api.airvisual.com"
DASHBOARD = "https://dashboard.iqair.com"
USER_AGENT = "HomeAssistant-IQAirCloud/2.0 (custom integration)"

DEVICES_PER_PAGE = 15
SMART_PROFILES = ("quiet", "balanced", "power")  # the dashboard labels "power" as "Max"

_POWER_MODES = {1: False, 2: True, 3: False}  # off, on, standby
_PROFILE_NUMBERS = {1: "quiet", 2: "balanced", 3: "power"}
_REDIRECT_STATUSES = (301, 302, 303, 307, 308)
_DEVICES_ROUTE = '"routes/personal/devices"'
_COOKIE_NAME = re.compile(r"[A-Za-z0-9_.\-]{1,32}")


class IQAirError(Exception):
    """Base class for everything this module raises."""


class ProtocolError(IQAirError):
    """IQAir answered with something this module does not understand."""


class TemporaryError(IQAirError):
    """Rate limited or a server-side failure. Back off and try again later."""


class InvalidCredentials(IQAirError):
    """IQAir did not accept the email and password."""


class LoginExpired(IQAirError):
    """The login token was rejected. Signing in again should fix it."""


class SessionExpired(IQAirError):
    """The dashboard session cookie was rejected. Only the user can supply a new one."""


class CommandRejected(IQAirError):
    """The dashboard understood the command but the device did not take it."""


@dataclass(frozen=True)
class Request:
    """An HTTP request for api.py to send. Redirects must never be followed."""

    method: str
    url: str
    headers: dict[str, str]
    params: dict[str, str] | None = None
    json: Any = None


@dataclass(frozen=True)
class Login:
    user_id: str
    login_token: str


@dataclass(frozen=True)
class Filter:
    slot: int
    name: str
    health_percent: int | None


@dataclass(frozen=True)
class Device:
    id: str
    name: str
    model: str | None
    model_label: str | None
    model_variation: str | None
    connected: bool
    is_on: bool | None
    speed_level: int | None  # the manual setting, 1..speed_count
    running_speed: int | None  # what the fan is doing right now (also in smart mode)
    speed_count: int | None
    smart_mode: bool | None
    smart_profile: str | None  # one of SMART_PROFILES
    supports_smart_mode: bool
    pm25: float | None  # µg/m³
    particle_count: float | None
    aqi: float | None  # US AQI
    filters: tuple[Filter, ...]


# --- shared helpers --------------------------------------------------------------------


def _raise_if_temporary(status: int) -> None:
    if status == 429 or status >= 500:
        raise TemporaryError(f"IQAir answered HTTP {status}")


def _is_login_redirect(status: int, body: str) -> bool:
    return status in _REDIRECT_STATUSES or status in (202, 401, 403) or "SingleFetchRedirect" in body


def _load_json(body: str) -> Any:
    try:
        return json.loads(body)
    except ValueError as err:
        raise ProtocolError("IQAir did not answer with JSON") from err


def _as_dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _as_int(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _as_number(value: Any) -> float | None:
    return value if isinstance(value, (int, float)) and not isinstance(value, bool) else None


# --- sign-in (website API) ---------------------------------------------------------------


def sign_in_request(email: str, password: str) -> Request:
    return Request(
        "POST",
        f"{WEBSITE_API}/v2/auth/signin/by/email",
        {"User-Agent": USER_AGENT, "Accept": "application/json"},
        json={"email": email, "password": password},
    )


def parse_sign_in(status: int, body: str) -> Login:
    _raise_if_temporary(status)
    if 400 <= status < 500:
        # Only a 422 for an empty body was ever observed; a wrong password was never sent.
        raise InvalidCredentials(f"IQAir refused the sign-in (HTTP {status})")
    if status != 200:
        raise ProtocolError(f"unexpected sign-in status {status}")
    data = _load_json(body)
    if not isinstance(data, dict) or not isinstance(data.get("id"), str) or not isinstance(data.get("loginToken"), str):
        raise ProtocolError("sign-in answer has no id and loginToken")
    return Login(user_id=data["id"], login_token=data["loginToken"])


# --- device list (website API) -----------------------------------------------------------


def devices_request(login: Login, page: int = 1) -> Request:
    return Request(
        "GET",
        f"{WEBSITE_API}/v2/users/{quote(login.user_id, safe='')}/devices",
        {"User-Agent": USER_AGENT, "Accept": "application/json", "x-login-token": login.login_token},
        params={
            "page": str(page),
            "perPage": str(DEVICES_PER_PAGE),
            "units.system": "imperial",
            "AQI": "US",
            "language": "en",
        },
    )


def parse_devices(status: int, body: str) -> list[Device]:
    _raise_if_temporary(status)
    if status in (401, 403):
        raise LoginExpired(f"IQAir rejected the login token (HTTP {status})")
    if status != 200:
        raise ProtocolError(f"unexpected device list status {status}")
    data = _load_json(body)
    if not isinstance(data, list):
        raise ProtocolError("device list is not a list")
    return [_parse_device(raw) for raw in data if isinstance(raw, dict) and isinstance(raw.get("id"), str)]


def _parse_device(raw: dict[str, Any]) -> Device:
    remote = _as_dict(raw.get("remote"))
    current = _as_dict(raw.get("current"))
    smart_mode = remote.get("autoModeEnabled")
    return Device(
        id=raw["id"],
        name=str(raw.get("name") or raw.get("modelLabel") or "IQAir purifier"),
        model=raw.get("model") if isinstance(raw.get("model"), str) else None,
        model_label=raw.get("modelLabel") if isinstance(raw.get("modelLabel"), str) else None,
        model_variation=raw.get("modelVariation") if isinstance(raw.get("modelVariation"), str) else None,
        connected=raw.get("isConnected") is True,
        is_on=_POWER_MODES.get(_as_int(remote.get("powerMode"))),
        speed_level=_as_int(remote.get("speedLevel")),
        running_speed=_as_int(current.get("fanSpeed")),
        speed_count=_as_int(remote.get("maxSpeedLevel")),
        smart_mode=smart_mode if isinstance(smart_mode, bool) else None,
        smart_profile=_PROFILE_NUMBERS.get(_as_int(remote.get("autoModeProfile"))),
        supports_smart_mode=_as_dict(raw.get("featureSet")).get("isAutoModeSupported") in (1, True),
        pm25=_reading(current, "pm25"),
        particle_count=_reading(current, "pc"),
        aqi=_reading(current, "aqi"),
        filters=tuple(_parse_filters(remote.get("filters") or raw.get("filterMaintenance"))),
    )


def _reading(current: dict[str, Any], key: str) -> float | None:
    return _as_number(_as_dict(current.get(key)).get("value"))


def _parse_filters(raw_filters: Any) -> list[Filter]:
    filters = []
    for index, raw in enumerate(raw_filters if isinstance(raw_filters, list) else [], start=1):
        if not isinstance(raw, dict):
            continue
        slot = _as_int(raw.get("slot")) or index
        mediums = raw.get("filterMediums")
        name = mediums[0] if isinstance(mediums, list) and mediums and isinstance(mediums[0], str) else f"Filter {slot}"
        filters.append(Filter(slot=slot, name=name, health_percent=_as_int(raw.get("healthPercent"))))
    return filters


# --- commands (dashboard) ------------------------------------------------------------------
# Only setPanelLock has been seen accepted from a script. The other shapes come from the
# dashboard's own remote-control code. The dashboard only offers speed and smart mode
# while the purifier is on, and the profile only while smart mode is on.


def power_command(on: bool) -> dict[str, Any]:
    return {"command": "setPowerMode", "power": "on" if on else "standby"}


def fan_speed_command(level: int) -> dict[str, Any]:
    if _as_int(level) is None or level < 1:
        raise ValueError(f"fan level must be a positive whole number, got {level!r}")
    return {"command": "setFanSpeed", "level": level}


def smart_mode_command(enabled: bool) -> dict[str, Any]:
    return {"command": "setAutoMode", "enabled": bool(enabled)}


def smart_profile_command(profile: str) -> dict[str, Any]:
    if profile not in SMART_PROFILES:
        raise ValueError(f"unknown smart mode profile {profile!r}")
    return {"command": "setAutoModeProfile", "profile": profile}


def command_request(device_id: str, session: str, command: dict[str, Any]) -> Request:
    return Request(
        "POST",
        f"{DASHBOARD}/personal/devices/{quote(device_id, safe='')}/remote-control/command",
        {
            "User-Agent": USER_AGENT,
            "Accept": "application/json",
            "Content-Type": "application/json",
            "Cookie": f"session={session}",
        },
        json=command,
    )


def parse_command_response(status: int, body: str) -> Any:
    """Return the value the dashboard reports for an accepted command."""
    _raise_if_temporary(status)
    if _is_login_redirect(status, body):
        raise SessionExpired("the IQAir dashboard session is no longer accepted")
    if status != 200:
        raise ProtocolError(f"unexpected command status {status}")
    data = _load_json(body)
    if not isinstance(data, dict) or not isinstance(data.get("ok"), bool):
        raise ProtocolError("command answer has no ok flag")
    if not data["ok"]:
        raise CommandRejected(str(data.get("error") or "IQAir refused the command"))
    return data.get("value")


def apply_command(device: Device, command: dict[str, Any]) -> Device:
    """Predict the state after an accepted command, the way the dashboard does."""
    name = command.get("command")
    if name == "setPowerMode":
        return dataclasses.replace(device, is_on=command["power"] == "on")
    if name == "setFanSpeed":
        return dataclasses.replace(device, speed_level=command["level"], smart_mode=False)
    if name == "setAutoMode":
        return dataclasses.replace(device, smart_mode=command["enabled"])
    if name == "setAutoModeProfile":
        return dataclasses.replace(device, smart_profile=command["profile"])
    return device


# --- dashboard session ---------------------------------------------------------------------


def normalize_session(pasted: str) -> str:
    """Pull the session value out of whatever the user copied from the browser.

    Accepts a whole Cookie header, a single `session=...` pair, or the bare value.
    """
    text = pasted.strip()
    if text.lower().startswith("cookie:"):
        text = text[len("cookie:"):].strip()
    if "\n" in text or "\r" in text:
        raise ValueError("the session must be a single line")
    for pair in (part.strip() for part in text.split(";")):
        if pair.startswith("session="):
            text = pair[len("session="):]
            break
    else:
        name, separator, rest = text.partition("=")
        other_cookie = bool(separator) and _COOKIE_NAME.fullmatch(name) and rest.strip("=")
        if ";" in text or other_cookie:
            raise ValueError("no session cookie in the pasted text")
    if not text or any(character.isspace() for character in text):
        raise ValueError("no session cookie in the pasted text")
    return text


def session_check_request(session: str) -> Request:
    """A read-only request that only a valid session can answer with data."""
    return Request(
        "GET",
        f"{DASHBOARD}/personal/devices.data",
        {"User-Agent": USER_AGENT, "Accept": "text/x-script", "Cookie": f"session={session}"},
    )


def parse_session_check(status: int, body: str) -> bool:
    _raise_if_temporary(status)
    if _is_login_redirect(status, body):
        return False
    if status == 200 and _DEVICES_ROUTE in body:
        return True
    raise ProtocolError(f"unexpected session check answer (HTTP {status})")
