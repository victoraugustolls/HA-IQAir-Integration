"""Setup, reauth and reconfigure forms for an IQAir account."""
from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import voluptuous as vol

from homeassistant.config_entries import ConfigEntry, ConfigFlow, ConfigFlowResult
from homeassistant.const import CONF_EMAIL, CONF_PASSWORD
from homeassistant.helpers.selector import TextSelector, TextSelectorConfig, TextSelectorType

from . import protocol
from .api import IQAirClient, create_client_session
from .const import CONF_LOGIN_TOKEN, CONF_SESSION, CONF_USER_ID, DOMAIN
from .protocol import Login

_PASSWORD = TextSelector(TextSelectorConfig(type=TextSelectorType.PASSWORD, autocomplete="current-password"))
_SECRET = TextSelector(TextSelectorConfig(type=TextSelectorType.PASSWORD))

USER_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_EMAIL): TextSelector(
            TextSelectorConfig(type=TextSelectorType.EMAIL, autocomplete="username")
        ),
        vol.Required(CONF_PASSWORD): _PASSWORD,
        vol.Optional(CONF_SESSION): _SECRET,
    }
)
SECRETS_SCHEMA = vol.Schema({vol.Optional(CONF_PASSWORD): _PASSWORD, vol.Optional(CONF_SESSION): _SECRET})


class IQAirConfigFlow(ConfigFlow, domain=DOMAIN):
    """Config flow for IQAir Cloud. One entry is one IQAir account."""

    VERSION = 2

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            email = user_input[CONF_EMAIL].strip()
            password = user_input[CONF_PASSWORD]
            login, session, errors = await self._async_check(
                email=email, password=password, session_text=user_input.get(CONF_SESSION)
            )
            if not errors and login is not None:
                await self.async_set_unique_id(login.user_id)
                self._abort_if_unique_id_configured()
                data = {
                    CONF_EMAIL: email,
                    CONF_PASSWORD: password,
                    CONF_USER_ID: login.user_id,
                    CONF_LOGIN_TOKEN: login.login_token,
                }
                if session:
                    data[CONF_SESSION] = session
                return self.async_create_entry(title=email, data=data)
        # Only the email is offered back after an error; secrets must be typed again.
        suggested = {CONF_EMAIL: user_input[CONF_EMAIL]} if user_input else None
        return self.async_show_form(
            step_id="user",
            data_schema=self.add_suggested_values_to_schema(USER_SCHEMA, suggested),
            errors=errors,
        )

    async def async_step_reauth(self, entry_data: Mapping[str, Any]) -> ConfigFlowResult:
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        return await self._async_update_secrets("reauth_confirm", self._get_reauth_entry(), user_input)

    async def async_step_reconfigure(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        return await self._async_update_secrets("reconfigure", self._get_reconfigure_entry(), user_input)

    async def _async_update_secrets(
        self, step_id: str, entry: ConfigEntry, user_input: dict[str, Any] | None
    ) -> ConfigFlowResult:
        """Replace the password, the dashboard session, or both, on an existing entry."""
        errors: dict[str, str] = {}
        if user_input is not None:
            password = user_input.get(CONF_PASSWORD) or None
            session_text = (user_input.get(CONF_SESSION) or "").strip() or None
            login: Login | None = None
            session: str | None = None
            if password is None and session_text is None:
                errors["base"] = "nothing_entered"
            else:
                login, session, errors = await self._async_check(
                    email=entry.data[CONF_EMAIL], password=password, session_text=session_text
                )
                if not errors and login is not None and entry.unique_id not in (None, login.user_id):
                    errors["base"] = "wrong_account"
            if not errors:
                updates: dict[str, Any] = {}
                if login is not None:
                    updates[CONF_PASSWORD] = password
                    updates[CONF_USER_ID] = login.user_id
                    updates[CONF_LOGIN_TOKEN] = login.login_token
                if session is not None:
                    updates[CONF_SESSION] = session
                return self.async_update_reload_and_abort(entry, data_updates=updates)
        return self.async_show_form(
            step_id=step_id,
            data_schema=SECRETS_SCHEMA,
            errors=errors,
            description_placeholders={"email": entry.data[CONF_EMAIL]},
        )

    async def _async_check(
        self, *, email: str, password: str | None, session_text: str | None
    ) -> tuple[Login | None, str | None, dict[str, str]]:
        """Try what the user entered against IQAir. Returns (login, session, errors)."""
        session: str | None = None
        if session_text and session_text.strip():
            try:
                session = protocol.normalize_session(session_text)
            except ValueError:
                # Wrong clipboard content: say so before sending anything anywhere.
                return None, None, {CONF_SESSION: "invalid_session"}

        login: Login | None = None
        errors: dict[str, str] = {}
        http = create_client_session(self.hass, auto_cleanup=False)
        try:
            client = IQAirClient(http, email=email, password=password or "", dashboard_session=session)
            if password is not None:
                try:
                    login = await client.async_sign_in()
                except protocol.InvalidCredentials:
                    errors["base"] = "invalid_auth"
                except protocol.IQAirError:
                    errors["base"] = "cannot_connect"
            if not errors and session is not None:
                try:
                    accepted = await client.async_check_session()
                except protocol.IQAirError:
                    errors["base"] = "cannot_connect"
                else:
                    if not accepted:
                        errors[CONF_SESSION] = "invalid_session"
        finally:
            http.detach()
        return login, session, errors
