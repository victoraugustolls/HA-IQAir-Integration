"""Network client for IQAir. It sends what protocol.py builds and nothing else."""
from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Any

import aiohttp

from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_create_clientsession

from . import protocol
from .protocol import Device, Login

REQUEST_TIMEOUT = aiohttp.ClientTimeout(total=20)
MAX_DEVICE_PAGES = 10


def create_client_session(hass: HomeAssistant, *, auto_cleanup: bool = True) -> aiohttp.ClientSession:
    """Create a session that stores no cookies.

    The dashboard session travels as a literal Cookie header. With a cookie jar, aiohttp
    would merge that header with stored cookies and re-quote it, which corrupts a value
    containing characters such as "=" or "/".
    """
    return async_create_clientsession(hass, auto_cleanup=auto_cleanup, cookie_jar=aiohttp.DummyCookieJar())


class IQAirClient:
    """Talks to IQAir for one account."""

    def __init__(
        self,
        session: aiohttp.ClientSession,
        *,
        email: str,
        password: str,
        login: Login | None = None,
        dashboard_session: str | None = None,
        on_login: Callable[[Login], None] | None = None,
    ) -> None:
        self._session = session
        self._email = email
        self._password = password
        self._login = login
        self._dashboard_session = dashboard_session
        self._on_login = on_login

    @property
    def has_dashboard_session(self) -> bool:
        return bool(self._dashboard_session)

    async def async_sign_in(self) -> Login:
        """Sign in with email and password. Raises InvalidCredentials if refused."""
        status, body = await self._send(protocol.sign_in_request(self._email, self._password))
        self._login = protocol.parse_sign_in(status, body)
        if self._on_login is not None:
            self._on_login(self._login)
        return self._login

    async def async_get_devices(self) -> list[Device]:
        """Read every device. Signs in again at most once if the token is rejected."""
        if self._login is None:
            await self.async_sign_in()
        try:
            return await self._async_list_devices()
        except protocol.LoginExpired:
            await self.async_sign_in()
        try:
            return await self._async_list_devices()
        except protocol.LoginExpired as err:
            raise protocol.ProtocolError("IQAir rejected a login token it had just issued") from err

    async def _async_list_devices(self) -> list[Device]:
        assert self._login is not None
        devices: dict[str, Device] = {}
        for page in range(1, MAX_DEVICE_PAGES + 1):
            status, body = await self._send(protocol.devices_request(self._login, page))
            batch = protocol.parse_devices(status, body)
            new = [device for device in batch if device.id not in devices]
            devices.update((device.id, device) for device in new)
            if len(batch) < protocol.DEVICES_PER_PAGE or not new:
                break
        return list(devices.values())

    async def async_send_command(self, device_id: str, command: dict[str, Any]) -> Any:
        """Send one command. Raises SessionExpired or CommandRejected when it is not taken."""
        if not self._dashboard_session:
            raise protocol.SessionExpired("no dashboard session is configured")
        status, body = await self._send(protocol.command_request(device_id, self._dashboard_session, command))
        return protocol.parse_command_response(status, body)

    async def async_check_session(self) -> bool:
        """Return whether the dashboard still accepts the session. Read-only."""
        if not self._dashboard_session:
            return False
        status, body = await self._send(protocol.session_check_request(self._dashboard_session))
        return protocol.parse_session_check(status, body)

    async def _send(self, request: protocol.Request) -> tuple[int, str]:
        try:
            async with self._session.request(
                request.method,
                request.url,
                headers=request.headers,
                params=request.params,
                json=request.json,
                allow_redirects=False,  # a redirect means "log in again", never "go there"
                timeout=REQUEST_TIMEOUT,
            ) as response:
                return response.status, await response.text(errors="replace")
        except (aiohttp.ClientError, asyncio.TimeoutError) as err:
            raise protocol.TemporaryError(f"could not reach IQAir ({type(err).__name__})") from err
