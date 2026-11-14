"""Tests for the IQAir protocol module.

These need neither Home Assistant nor the network: protocol.py is loaded straight
from its file. Fixtures in tests/fixtures are real responses captured on 2026-11-14
with Tools/iqair_diag.py and sanitized (ids, serials, account and network details
replaced).

Where an expectation comes from is marked on each test:
  [captured]   seen in a real response from IQAir
  [dashboard]  taken from the dashboard's own JavaScript, never seen on the wire
  [upstream]   taken from this integration's 1.x code or from hmk/iqair-matter
"""
import importlib.util
import json
import pathlib
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
FIXTURES = pathlib.Path(__file__).parent / "fixtures"

_spec = importlib.util.spec_from_file_location(
    "iqair_protocol", ROOT / "custom_components" / "iqair_cloud" / "protocol.py"
)
protocol = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = protocol
_spec.loader.exec_module(protocol)

LOGIN = protocol.Login(user_id="aaaaaaaaaaaaaaaaaaaaaaaa", login_token="token-123")
# [captured] body of an unauthenticated GET /personal/devices.data (HTTP 202)
LOGIN_REDIRECT_BODY = (
    '[["SingleFetchRedirect",1],{"_2":3,"_4":5,"_6":7,"_8":7,"_9":7},"redirect",'
    '"https://www.iqair.com/account/login?redirectURL=https%3A%2F%2Fdashboard.iqair.com%2Fpersonal%2Fdevices",'
    '"status",302,"revalidate",false,"reload","replace"]'
)


def legacy_devices_text():
    return (FIXTURES / "legacy_devices.json").read_text()


def devices_by_name():
    return {d.name: d for d in protocol.parse_devices(200, legacy_devices_text())}


def one_device(**remote_overrides):
    """The captured Kitchen purifier with some `remote` fields changed."""
    raw = json.loads(legacy_devices_text())[2]
    raw["remote"].update(remote_overrides)
    return protocol.parse_devices(200, json.dumps([raw]))[0]


# --- sign-in -------------------------------------------------------------------------


def test_sign_in_request_posts_email_and_password_as_json():
    request = protocol.sign_in_request("me@example.com", "pw")

    assert request.method == "POST"
    assert request.url == "https://website-api.airvisual.com/v2/auth/signin/by/email"
    assert request.json == {"email": "me@example.com", "password": "pw"}


def test_parse_sign_in_returns_user_id_and_login_token():
    # [captured] key set of a successful sign-in; the values here are made up
    body = json.dumps(
        {
            "id": "aaaaaaaaaaaaaaaaaaaaaaaa", "loginToken": "token-123", "email": "me@example.com",
            "name": "Me", "profilePicture": "", "nextStep": "", "dialog": None, "shopifyAuth": "x",
        }
    )

    assert protocol.parse_sign_in(200, body) == protocol.Login(
        user_id="aaaaaaaaaaaaaaaaaaaaaaaa", login_token="token-123"
    )


@pytest.mark.parametrize("status", [400, 401, 403, 404, 422])
def test_parse_sign_in_treats_a_client_error_as_wrong_credentials(status):
    # [captured] only 422 for an empty body. A wrong password was never sent, so every
    # 4xx except 429 counts as "IQAir did not accept these credentials".
    with pytest.raises(protocol.InvalidCredentials):
        protocol.parse_sign_in(status, '{"code":"validation_failed"}')


@pytest.mark.parametrize("status", [429, 500, 503])
def test_parse_sign_in_treats_rate_limit_and_server_errors_as_temporary(status):
    with pytest.raises(protocol.TemporaryError):
        protocol.parse_sign_in(status, "")


@pytest.mark.parametrize("body", ["{}", '{"id":"abc"}', "not json", "[]"])
def test_parse_sign_in_without_a_token_is_a_protocol_error(body):
    with pytest.raises(protocol.ProtocolError):
        protocol.parse_sign_in(200, body)


# --- device list ---------------------------------------------------------------------


def test_devices_request_carries_the_login_token_header():
    request = protocol.devices_request(LOGIN, page=2)

    assert request.method == "GET"
    assert request.url == "https://website-api.airvisual.com/v2/users/aaaaaaaaaaaaaaaaaaaaaaaa/devices"
    assert request.headers["x-login-token"] == "token-123"
    assert request.params["page"] == "2"
    assert request.params["perPage"] == str(protocol.DEVICES_PER_PAGE)


def test_parse_devices_reads_the_healthpro_state():
    # [captured]
    device = devices_by_name()["Primary Bedroom"]

    assert device.id == "ui2_000000000000000000000001"
    assert (device.model, device.model_label, device.model_variation) == ("ui2", "HealthPro Plus", "HP250XE")
    assert device.connected is True
    assert device.is_on is True
    assert (device.speed_level, device.speed_count) == (6, 6)
    assert device.smart_mode is False
    assert device.smart_profile == "power"
    assert device.supports_smart_mode is True
    assert (device.pm25, device.particle_count, device.aqi) == (0, 781, 0)


def test_parse_devices_reads_the_atem_earth_state():
    # [captured]
    device = devices_by_name()["Living Room"]

    assert device.id == "wap_000000000000000000000002"
    assert (device.model, device.model_label, device.model_variation) == ("wap", "Atem E", "ATEM-E")
    assert device.is_on is True
    assert (device.speed_level, device.speed_count) == (4, 6)
    assert device.smart_mode is False
    assert device.smart_profile == "balanced"
    assert (device.pm25, device.particle_count, device.aqi) == (0, 333, 0)


def test_parse_devices_reads_the_speed_the_fan_is_actually_running_at():
    # [captured] current.fanSpeed is a bare number, unlike the other readings
    devices = devices_by_name()

    assert devices["Primary Bedroom"].running_speed == 6
    assert devices["Kitchen"].running_speed == 4


def test_parse_devices_lists_every_filter_with_its_slot_and_health():
    # [captured]
    devices = devices_by_name()

    assert [(f.slot, f.name, f.health_percent) for f in devices["Primary Bedroom"].filters] == [
        (1, "Pre-Filter", 66),
        (2, "Gas and odour filter", 73),
        (3, "Main particle filter", 90),
    ]
    assert [(f.slot, f.name, f.health_percent) for f in devices["Kitchen"].filters] == [
        (1, "Main particle filter", 98)
    ]


@pytest.mark.parametrize(("power_mode", "expected"), [(2, True), (3, False), (1, False), (7, None), (None, None)])
def test_power_mode_maps_on_standby_off_and_leaves_other_values_unknown(power_mode, expected):
    # [captured] 2 = on.  [upstream] 3 = standby, 1 = off.
    assert one_device(powerMode=power_mode).is_on is expected


@pytest.mark.parametrize(
    ("number", "expected"), [(1, "quiet"), (2, "balanced"), (3, "power"), (9, None), (None, None)]
)
def test_smart_profile_number_maps_to_the_dashboard_name(number, expected):
    # [captured] 2 = balanced and 3 = power (old API next to the dashboard stream).  [upstream] 1 = quiet.
    assert one_device(autoModeProfile=number).smart_profile == expected


def test_parse_devices_survives_a_device_without_state_or_readings():
    raw = {"id": "wap_000000000000000000000009", "name": "Bare", "model": "wap"}

    device = protocol.parse_devices(200, json.dumps([raw]))[0]

    assert (device.is_on, device.speed_level, device.speed_count, device.smart_mode) == (None, None, None, None)
    assert (device.pm25, device.particle_count, device.aqi) == (None, None, None)
    assert device.filters == ()
    assert device.connected is False
    assert device.supports_smart_mode is False


def test_parse_devices_skips_entries_without_an_id():
    assert protocol.parse_devices(200, json.dumps([{"name": "ghost"}, "junk"])) == []


@pytest.mark.parametrize("status", [401, 403])
def test_parse_devices_reports_a_rejected_login_token(status):
    # [captured] 403 {"code":"forbidden","description":"Authentication required"} without a token
    with pytest.raises(protocol.LoginExpired):
        protocol.parse_devices(status, '{"code":"forbidden"}')


@pytest.mark.parametrize("status", [429, 500, 502])
def test_parse_devices_treats_rate_limit_and_server_errors_as_temporary(status):
    with pytest.raises(protocol.TemporaryError):
        protocol.parse_devices(status, "")


@pytest.mark.parametrize(("status", "body"), [(404, '{"code":"not_found"}'), (200, '{"devices":[]}'), (200, "<html>")])
def test_parse_devices_flags_an_unexpected_answer(status, body):
    # 404 is what IQAir returned when it retired the v1 routes (upstream issue #4).
    with pytest.raises(protocol.ProtocolError):
        protocol.parse_devices(status, body)


# --- commands ------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("command", "expected"),
    [
        # [dashboard] shapes from remote-control-ui; only setPanelLock was ever sent for real
        (lambda: protocol.power_command(True), {"command": "setPowerMode", "power": "on"}),
        (lambda: protocol.power_command(False), {"command": "setPowerMode", "power": "standby"}),
        (lambda: protocol.fan_speed_command(3), {"command": "setFanSpeed", "level": 3}),
        (lambda: protocol.smart_mode_command(True), {"command": "setAutoMode", "enabled": True}),
        (lambda: protocol.smart_mode_command(False), {"command": "setAutoMode", "enabled": False}),
        (lambda: protocol.smart_profile_command("quiet"), {"command": "setAutoModeProfile", "profile": "quiet"}),
    ],
)
def test_command_payloads_match_what_the_dashboard_sends(command, expected):
    assert command() == expected


@pytest.mark.parametrize("level", [0, -1, 2.5, True])
def test_fan_speed_command_refuses_a_level_that_is_not_a_positive_whole_number(level):
    with pytest.raises(ValueError):
        protocol.fan_speed_command(level)


def test_smart_profile_command_refuses_an_unknown_profile():
    with pytest.raises(ValueError):
        protocol.smart_profile_command("turbo")


def test_command_request_posts_json_to_the_device_with_only_the_session_cookie():
    # [captured] this exact request shape was accepted for setPanelLock
    request = protocol.command_request("wap_000000000000000000000003", "abc%3D.sig", {"command": "setFanSpeed", "level": 3})

    assert request.method == "POST"
    assert request.url == (
        "https://dashboard.iqair.com/personal/devices/wap_000000000000000000000003/remote-control/command"
    )
    assert request.headers["Cookie"] == "session=abc%3D.sig"
    assert request.json == {"command": "setFanSpeed", "level": 3}


def test_parse_command_response_returns_the_value_of_an_accepted_command():
    # [captured] answer to setPanelLock false
    assert protocol.parse_command_response(200, '{"ok":true,"value":false}') is False


def test_parse_command_response_raises_the_reason_when_the_device_refuses():
    # [dashboard] shape of a refused command
    with pytest.raises(protocol.CommandRejected, match="Device is offline"):
        protocol.parse_command_response(200, '{"ok":false,"error":"Device is offline"}')


@pytest.mark.parametrize(
    ("status", "body"),
    [
        (302, ""),  # [captured] POST without a session redirects to the login page
        (202, LOGIN_REDIRECT_BODY),
        (401, ""),
        (403, ""),
    ],
)
def test_parse_command_response_reports_an_expired_session(status, body):
    with pytest.raises(protocol.SessionExpired):
        protocol.parse_command_response(status, body)


@pytest.mark.parametrize("status", [429, 500, 503])
def test_parse_command_response_treats_rate_limit_and_server_errors_as_temporary(status):
    with pytest.raises(protocol.TemporaryError):
        protocol.parse_command_response(status, "")


@pytest.mark.parametrize("body", ["<html>oops</html>", "[]", '{"value":1}'])
def test_parse_command_response_flags_an_answer_without_ok(body):
    with pytest.raises(protocol.ProtocolError):
        protocol.parse_command_response(200, body)


def test_setting_a_fan_speed_predicts_manual_mode_at_that_level():
    # [dashboard] its own optimistic update turns smart mode off on setFanSpeed
    before = one_device(speedLevel=4, autoModeEnabled=True)

    after = protocol.apply_command(before, protocol.fan_speed_command(2))

    assert (after.speed_level, after.smart_mode) == (2, False)
    assert before.speed_level == 4


def test_power_smart_mode_and_profile_commands_predict_their_own_state():
    device = one_device(powerMode=2, autoModeEnabled=False, autoModeProfile=2)

    assert protocol.apply_command(device, protocol.power_command(False)).is_on is False
    assert protocol.apply_command(device, protocol.smart_mode_command(True)).smart_mode is True
    assert protocol.apply_command(device, protocol.smart_profile_command("power")).smart_profile == "power"


# --- dashboard session ---------------------------------------------------------------


@pytest.mark.parametrize(
    ("pasted", "expected"),
    [
        ("_ga=GA1.1.2; session=abc%3D%3D.s-i_g/+; __vdpl=dpl_1", "abc%3D%3D.s-i_g/+"),
        ("Cookie: session=abc.sig; _ga=1", "abc.sig"),
        ("session=abc.sig", "abc.sig"),
        ("  abc.sig \n", "abc.sig"),
        ("eyJjdXN0b21lciI6e319.c2ln==", "eyJjdXN0b21lciI6e319.c2ln=="),
    ],
)
def test_normalize_session_accepts_a_header_a_pair_or_the_bare_value(pasted, expected):
    assert protocol.normalize_session(pasted) == expected


@pytest.mark.parametrize(
    "pasted", ["", "   ", "_ga=GA1.1.2; __vdpl=dpl_1", "_ga=GA1.1.2", "session=", "two words", "a\nb"]
)
def test_normalize_session_refuses_text_that_holds_no_session(pasted):
    with pytest.raises(ValueError):
        protocol.normalize_session(pasted)


def test_session_check_request_reads_the_device_list_with_the_session_cookie():
    request = protocol.session_check_request("abc.sig")

    assert request.method == "GET"
    assert request.url == "https://dashboard.iqair.com/personal/devices.data"
    assert request.headers["Cookie"] == "session=abc.sig"


def test_session_check_accepts_a_real_device_list():
    # [captured]
    body = (FIXTURES / "dashboard_devices.data.json").read_text()

    assert protocol.parse_session_check(200, body) is True


@pytest.mark.parametrize(
    ("status", "body"),
    [(202, LOGIN_REDIRECT_BODY), (302, ""), (401, ""), (403, "")],  # [captured] the 202
)
def test_session_check_rejects_a_login_redirect(status, body):
    assert protocol.parse_session_check(status, body) is False


@pytest.mark.parametrize("status", [429, 500, 503])
def test_session_check_treats_rate_limit_and_server_errors_as_temporary(status):
    with pytest.raises(protocol.TemporaryError):
        protocol.parse_session_check(status, "")


def test_session_check_does_not_guess_on_an_unrecognized_answer():
    with pytest.raises(protocol.ProtocolError):
        protocol.parse_session_check(200, "<html>maintenance</html>")
