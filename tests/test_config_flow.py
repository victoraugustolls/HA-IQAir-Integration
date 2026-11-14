"""The setup, reauth and reconfigure forms."""
from unittest.mock import patch

import pytest

from homeassistant.config_entries import SOURCE_USER
from homeassistant.data_entry_flow import FlowResultType

from .common import DEVICES_URL, LOGIN_REDIRECT_BODY, SESSION_URL, SIGN_IN_URL, USER_ID

SIGNED_IN = {"id": USER_ID, "loginToken": "token-9", "email": "me@example.com"}


@pytest.fixture(autouse=True)
def no_real_setup():
    """These tests are about the forms; loading the integration is covered in test_init."""
    with patch("custom_components.iqair_cloud.async_setup_entry", return_value=True):
        yield


def calls_to(aioclient_mock, url: str) -> list:
    return [call for call in aioclient_mock.mock_calls if str(call[1]).split("?")[0] == url]


async def start(hass):
    result = await hass.config_entries.flow.async_init("iqair_cloud", context={"source": SOURCE_USER})
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "user"
    return result


async def submit(hass, result, **user_input):
    return await hass.config_entries.flow.async_configure(result["flow_id"], user_input)


# --- first setup -----------------------------------------------------------------------


async def test_setup_stores_the_login_and_only_the_session_value_from_a_pasted_cookie_header(
    hass, aioclient_mock, dashboard_devices_text
):
    aioclient_mock.post(SIGN_IN_URL, json=SIGNED_IN)
    aioclient_mock.get(SESSION_URL, text=dashboard_devices_text)

    result = await submit(
        hass, await start(hass),
        email=" me@example.com ", password="pw", session="Cookie: _ga=GA1.1.2; session=abc%3D.sig; __vdpl=dpl_1",
    )

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "me@example.com"
    assert result["data"] == {
        "email": "me@example.com",
        "password": "pw",
        "user_id": USER_ID,
        "login_token": "token-9",
        "session": "abc%3D.sig",
    }
    assert result["result"].unique_id == USER_ID
    assert calls_to(aioclient_mock, SIGN_IN_URL)[0][2] == {"email": "me@example.com", "password": "pw"}
    assert calls_to(aioclient_mock, SESSION_URL)[0][3]["Cookie"] == "session=abc%3D.sig"


async def test_setup_without_a_session_cookie_creates_a_read_only_entry(hass, aioclient_mock):
    aioclient_mock.post(SIGN_IN_URL, json=SIGNED_IN)

    result = await submit(hass, await start(hass), email="me@example.com", password="pw")

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert "session" not in result["data"]
    assert calls_to(aioclient_mock, SESSION_URL) == []


async def test_a_refused_password_shows_an_error_and_a_corrected_one_goes_through(hass, aioclient_mock):
    aioclient_mock.post(SIGN_IN_URL, status=401, json={"code": "unauthorized"})
    result = await submit(hass, await start(hass), email="me@example.com", password="wrong")

    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "invalid_auth"}

    aioclient_mock.clear_requests()
    aioclient_mock.post(SIGN_IN_URL, json=SIGNED_IN)
    result = await submit(hass, result, email="me@example.com", password="pw")

    assert result["type"] is FlowResultType.CREATE_ENTRY


async def test_an_iqair_outage_is_reported_as_a_connection_problem(hass, aioclient_mock):
    aioclient_mock.post(SIGN_IN_URL, status=503)

    result = await submit(hass, await start(hass), email="me@example.com", password="pw")

    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "cannot_connect"}


async def test_pasted_text_without_a_session_cookie_is_refused_before_anything_is_sent(hass, aioclient_mock):
    aioclient_mock.post(SIGN_IN_URL, json=SIGNED_IN)

    result = await submit(
        hass, await start(hass), email="me@example.com", password="pw", session="_ga=GA1.1.2; __vdpl=dpl_1"
    )

    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"session": "invalid_session"}
    assert aioclient_mock.mock_calls == []


async def test_a_session_cookie_the_dashboard_rejects_is_refused(hass, aioclient_mock):
    aioclient_mock.post(SIGN_IN_URL, json=SIGNED_IN)
    aioclient_mock.get(SESSION_URL, status=202, text=LOGIN_REDIRECT_BODY)

    result = await submit(hass, await start(hass), email="me@example.com", password="pw", session="stale.sig")

    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"session": "invalid_session"}


async def test_the_same_account_cannot_be_added_twice(hass, aioclient_mock, config_entry):
    aioclient_mock.post(SIGN_IN_URL, json=SIGNED_IN)

    result = await submit(hass, await start(hass), email="me@example.com", password="pw")

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"


# --- reauth ------------------------------------------------------------------------------


async def test_reauth_with_only_a_new_session_keeps_the_password_and_does_not_sign_in(
    hass, aioclient_mock, config_entry, dashboard_devices_text
):
    aioclient_mock.get(SESSION_URL, text=dashboard_devices_text)
    result = await config_entry.start_reauth_flow(hass)
    assert result["step_id"] == "reauth_confirm"

    result = await submit(hass, result, session="session=fresh.sig")

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reauth_successful"
    assert config_entry.data["session"] == "fresh.sig"
    assert config_entry.data["password"] == "pw"
    assert config_entry.data["login_token"] == "token-1"
    assert calls_to(aioclient_mock, SIGN_IN_URL) == []


async def test_reauth_with_a_new_password_stores_it_with_the_new_login_token(hass, aioclient_mock, config_entry):
    aioclient_mock.post(SIGN_IN_URL, json=SIGNED_IN)
    result = await config_entry.start_reauth_flow(hass)

    result = await submit(hass, result, password="new-pw")

    assert result["reason"] == "reauth_successful"
    assert config_entry.data["password"] == "new-pw"
    assert config_entry.data["login_token"] == "token-9"
    assert config_entry.data["session"] == "abc.sig"
    assert calls_to(aioclient_mock, SIGN_IN_URL)[0][2] == {"email": "me@example.com", "password": "new-pw"}


async def test_reauth_refuses_a_password_that_belongs_to_another_account(hass, aioclient_mock, config_entry):
    aioclient_mock.post(SIGN_IN_URL, json={"id": "bbbbbbbbbbbbbbbbbbbbbbbb", "loginToken": "other"})
    result = await config_entry.start_reauth_flow(hass)

    result = await submit(hass, result, password="pw")

    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "wrong_account"}
    assert config_entry.data["login_token"] == "token-1"


async def test_reauth_with_both_fields_empty_asks_for_one(hass, aioclient_mock, config_entry):
    result = await config_entry.start_reauth_flow(hass)

    result = await submit(hass, result)

    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "nothing_entered"}
    assert aioclient_mock.mock_calls == []


async def test_reauth_keeps_the_old_session_when_the_new_one_is_rejected(hass, aioclient_mock, config_entry):
    aioclient_mock.get(SESSION_URL, status=202, text=LOGIN_REDIRECT_BODY)
    result = await config_entry.start_reauth_flow(hass)

    result = await submit(hass, result, session="stale.sig")

    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"session": "invalid_session"}
    assert config_entry.data["session"] == "abc.sig"


# --- reconfigure -------------------------------------------------------------------------


async def test_reconfigure_adds_a_session_to_an_entry_that_had_none(
    hass, aioclient_mock, config_entry, dashboard_devices_text
):
    hass.config_entries.async_update_entry(
        config_entry, data={k: v for k, v in config_entry.data.items() if k != "session"}
    )
    aioclient_mock.get(SESSION_URL, text=dashboard_devices_text)
    aioclient_mock.get(DEVICES_URL, json=[])
    result = await config_entry.start_reconfigure_flow(hass)
    assert result["step_id"] == "reconfigure"

    result = await submit(hass, result, session="added.sig")

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reconfigure_successful"
    assert config_entry.data["session"] == "added.sig"
