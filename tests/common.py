"""Constants and small helpers shared by the Home Assistant tests."""
USER_ID = "aaaaaaaaaaaaaaaaaaaaaaaa"
SIGN_IN_URL = "https://website-api.airvisual.com/v2/auth/signin/by/email"
DEVICES_URL = f"https://website-api.airvisual.com/v2/users/{USER_ID}/devices"
SESSION_URL = "https://dashboard.iqair.com/personal/devices.data"
HEALTHPRO = "ui2_000000000000000000000001"
LIVING_ROOM = "wap_000000000000000000000002"
KITCHEN = "wap_000000000000000000000003"
ENTRY_DATA = {
    "email": "me@example.com",
    "password": "pw",
    "user_id": USER_ID,
    "login_token": "token-1",
    "session": "abc.sig",
}
# Captured body of an unauthenticated GET /personal/devices.data (HTTP 202).
LOGIN_REDIRECT_BODY = (
    '[["SingleFetchRedirect",1],{"_2":3,"_4":5,"_6":7,"_8":7,"_9":7},"redirect",'
    '"https://www.iqair.com/account/login?redirectURL=https%3A%2F%2Fdashboard.iqair.com%2Fpersonal%2Fdevices",'
    '"status",302,"revalidate",false,"reload","replace"]'
)


def command_url(device_id: str) -> str:
    return f"https://dashboard.iqair.com/personal/devices/{device_id}/remote-control/command"


def sent_commands(aioclient_mock) -> list:
    """Every command POSTed to the dashboard so far, as (url, json body)."""
    return [(str(url), data) for method, url, data, _ in aioclient_mock.mock_calls if method.lower() == "post"
            and "remote-control/command" in str(url)]


def reauth_flows(hass) -> list:
    return [f for f in hass.config_entries.flow.async_progress() if f["context"].get("source") == "reauth"]
