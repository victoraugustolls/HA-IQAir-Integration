"""Tests for the network client, run against a local stand-in for IQAir's two hosts."""
import json

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer
from yarl import URL

from custom_components.iqair_cloud import api, protocol

# Characters that cookie libraries like to quote or re-encode.
SESSION = "eyJhIjoiYi9jK2Q9In0%3D.s1g/n+at=ure%2B"
USER_ID = "aaaaaaaaaaaaaaaaaaaaaaaa"


class FakeIQAir:
    """Records what the client sends and answers like the real hosts were seen to."""

    def __init__(self, devices_text: str, dashboard_text: str) -> None:
        self.devices = json.loads(devices_text)
        self.dashboard_text = dashboard_text
        self.valid_token = "token-1"
        self.ignores_paging = False
        self.issued_tokens = ["token-2", "token-3"]
        self.sign_ins = []
        self.device_requests = []
        self.cookie_headers = []
        self.commands = []
        self.landing_hits = 0
        self.command_status = 200
        self.command_headers = {}
        self.command_body = '{"ok":true,"value":false}'  # captured answer to setPanelLock

    def app(self) -> web.Application:
        app = web.Application()
        app.router.add_post("/v2/auth/signin/by/email", self._sign_in)
        app.router.add_get("/v2/users/{user}/devices", self._devices)
        app.router.add_get("/personal/devices.data", self._session_check)
        app.router.add_post("/personal/devices/{device}/remote-control/command", self._command)
        app.router.add_get("/account/login", self._landing)
        return app

    async def _sign_in(self, request: web.Request) -> web.Response:
        self.sign_ins.append(await request.json())
        return web.json_response({"id": USER_ID, "loginToken": self.issued_tokens.pop(0), "email": "me@example.com"})

    async def _devices(self, request: web.Request) -> web.Response:
        token = request.headers.get("x-login-token")
        self.device_requests.append((token, request.query["page"]))
        if token != self.valid_token:
            return web.json_response({"code": "forbidden", "description": "Authentication required"}, status=403)
        page, size = int(request.query["page"]), int(request.query["perPage"])
        if self.ignores_paging:
            page = 1
        return web.json_response(self.devices[(page - 1) * size : page * size])

    async def _session_check(self, request: web.Request) -> web.Response:
        self.cookie_headers.append(request.headers.get("Cookie"))
        response = web.Response(text=self.dashboard_text, content_type="text/x-script")
        # A real server may set cookies of its own; the client must not echo or merge them.
        response.set_cookie("__vdpl", "dpl_1")
        response.set_cookie("session", "replaced-by-server")
        return response

    async def _command(self, request: web.Request) -> web.Response:
        self.cookie_headers.append(request.headers.get("Cookie"))
        self.commands.append((request.match_info["device"], await request.json()))
        return web.Response(
            status=self.command_status,
            headers=self.command_headers,
            text=self.command_body,
            content_type="application/json",
        )

    async def _landing(self, request: web.Request) -> web.Response:
        self.landing_hits += 1
        return web.json_response({"ok": True, "value": "followed a redirect"})


@pytest.fixture
async def fake(hass, monkeypatch, socket_enabled, legacy_devices_text, dashboard_devices_text):
    fake = FakeIQAir(legacy_devices_text, dashboard_devices_text)
    server = TestServer(fake.app())
    await server.start_server()
    base = str(server.make_url("")).rstrip("/")
    monkeypatch.setattr(protocol, "WEBSITE_API", base)
    monkeypatch.setattr(protocol, "DASHBOARD", base)
    yield fake
    await server.close()


def make_client(hass, *, login=None, session=SESSION, on_login=None) -> api.IQAirClient:
    return api.IQAirClient(
        api.create_client_session(hass),
        email="me@example.com",
        password="pw",
        login=login,
        dashboard_session=session,
        on_login=on_login,
    )


async def test_session_cookie_reaches_the_dashboard_byte_for_byte(hass, fake):
    client = make_client(hass)

    assert await client.async_check_session() is True
    await client.async_send_command("wap_1", protocol.fan_speed_command(3))
    await client.async_send_command("wap_1", protocol.fan_speed_command(4))

    assert fake.cookie_headers == [f"session={SESSION}"] * 3


async def test_client_session_does_not_keep_cookies_the_dashboard_sets(hass):
    # aiohttp merges stored cookies into an explicit Cookie header and re-quotes the
    # result, so a stored cookie for the dashboard would corrupt the session value.
    # (The local server above cannot show this: cookie jars ignore bare IP hosts.)
    session = api.create_client_session(hass)
    dashboard = URL("https://dashboard.iqair.com/personal/devices.data")

    session.cookie_jar.update_cookies({"session": "replaced-by-server", "__vdpl": "dpl_1"}, dashboard)

    assert len(session.cookie_jar.filter_cookies(dashboard)) == 0


async def test_send_command_posts_the_command_to_the_device(hass, fake):
    client = make_client(hass)

    value = await client.async_send_command("wap_000000000000000000000003", protocol.fan_speed_command(3))

    assert value is False
    assert fake.commands == [("wap_000000000000000000000003", {"command": "setFanSpeed", "level": 3})]


async def test_send_command_never_follows_the_login_redirect(hass, fake):
    fake.command_status, fake.command_headers, fake.command_body = 302, {"Location": "/account/login"}, ""
    client = make_client(hass)

    with pytest.raises(protocol.SessionExpired):
        await client.async_send_command("wap_1", protocol.power_command(True))

    assert fake.landing_hits == 0


async def test_get_devices_uses_a_stored_login_without_signing_in(hass, fake):
    client = make_client(hass, login=protocol.Login(USER_ID, "token-1"))

    devices = await client.async_get_devices()

    assert [d.name for d in devices] == ["Primary Bedroom", "Living Room", "Kitchen"]
    assert fake.sign_ins == []


async def test_get_devices_signs_in_first_when_there_is_no_login(hass, fake):
    fake.valid_token = "token-2"
    logins = []
    client = make_client(hass, on_login=logins.append)

    devices = await client.async_get_devices()

    assert len(devices) == 3
    assert fake.sign_ins == [{"email": "me@example.com", "password": "pw"}]
    assert logins == [protocol.Login(USER_ID, "token-2")]


async def test_get_devices_signs_in_again_once_when_the_token_is_rejected(hass, fake):
    fake.valid_token = "token-2"
    logins = []
    client = make_client(hass, login=protocol.Login(USER_ID, "stale"), on_login=logins.append)

    devices = await client.async_get_devices()

    assert len(devices) == 3
    assert len(fake.sign_ins) == 1
    assert [token for token, _ in fake.device_requests] == ["stale", "token-2"]
    assert logins == [protocol.Login(USER_ID, "token-2")]


async def test_get_devices_gives_up_when_a_fresh_token_is_rejected_too(hass, fake):
    fake.valid_token = "never-issued"
    client = make_client(hass, login=protocol.Login(USER_ID, "stale"))

    with pytest.raises(protocol.ProtocolError):
        await client.async_get_devices()

    assert len(fake.sign_ins) == 1
    assert len(fake.device_requests) == 2


async def test_get_devices_reads_the_next_page_when_a_page_is_full(hass, fake, legacy_devices):
    fake.devices = [dict(legacy_devices[0], id=f"ui2_{n:024d}", name=f"Purifier {n}") for n in range(16)]
    client = make_client(hass, login=protocol.Login(USER_ID, "token-1"))

    devices = await client.async_get_devices()

    assert len(devices) == 16
    assert [page for _, page in fake.device_requests] == ["1", "2"]


async def test_get_devices_stops_when_a_page_only_repeats_known_devices(hass, fake, legacy_devices):
    fake.devices = [dict(legacy_devices[0], id=f"ui2_{n:024d}", name=f"Purifier {n}") for n in range(15)]
    fake.ignores_paging = True
    client = make_client(hass, login=protocol.Login(USER_ID, "token-1"))

    devices = await client.async_get_devices()

    assert len(devices) == 15
    assert len(fake.device_requests) == 2


async def test_unreachable_host_is_a_temporary_error(hass, monkeypatch, socket_enabled):
    monkeypatch.setattr(protocol, "WEBSITE_API", "http://127.0.0.1:9")
    client = make_client(hass, login=protocol.Login(USER_ID, "token-1"))

    with pytest.raises(protocol.TemporaryError):
        await client.async_get_devices()
