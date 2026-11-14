"""Setup, polling, re-login, back-off and the dashboard session check."""
from datetime import timedelta

from homeassistant.config_entries import ConfigEntryState
from homeassistant.helpers import device_registry as dr
from homeassistant.util import dt as dt_util

from pytest_homeassistant_custom_component.common import MockConfigEntry, async_fire_time_changed

from .common import (
    DEVICES_URL,
    HEALTHPRO,
    KITCHEN,
    LOGIN_REDIRECT_BODY,
    SESSION_URL,
    SIGN_IN_URL,
    USER_ID,
    reauth_flows,
)


def calls_to(aioclient_mock, url: str) -> list:
    return [call for call in aioclient_mock.mock_calls if str(call[1]).split("?")[0] == url]


async def test_setup_registers_each_purifier_as_a_device_without_its_serial(hass, setup):
    entry = await setup()

    assert entry.state is ConfigEntryState.LOADED
    registry = dr.async_get(hass)
    by_id = {
        next(iter(device.identifiers)): device
        for device in dr.async_entries_for_config_entry(registry, entry.entry_id)
    }
    healthpro = by_id[("iqair_cloud", HEALTHPRO)]
    kitchen = by_id[("iqair_cloud", KITCHEN)]
    assert (healthpro.name, healthpro.manufacturer, healthpro.model, healthpro.model_id) == (
        "Primary Bedroom", "IQAir", "HealthPro Plus", "HP250XE",
    )
    assert (kitchen.name, kitchen.model, kitchen.model_id) == ("Kitchen", "Atem E", "ATEM-E")
    assert healthpro.serial_number is None
    assert len(by_id) == 3


async def test_unloading_removes_the_entities_cleanly(hass, setup):
    entry = await setup()

    assert await hass.config_entries.async_unload(entry.entry_id)

    assert entry.state is ConfigEntryState.NOT_LOADED
    assert hass.states.get("fan.kitchen").state == "unavailable"


async def test_a_rejected_login_token_is_replaced_and_stored_without_reloading(
    hass, aioclient_mock, config_entry, devices
):
    aioclient_mock.get(DEVICES_URL, status=403, json={"code": "forbidden"})
    aioclient_mock.post(SIGN_IN_URL, json={"id": USER_ID, "loginToken": "token-2"})
    aioclient_mock.get(SESSION_URL, text=LOGIN_REDIRECT_BODY, status=202)

    def accept_new_token():
        aioclient_mock.clear_requests()
        aioclient_mock.get(DEVICES_URL, json=devices)
        aioclient_mock.get(SESSION_URL, text=LOGIN_REDIRECT_BODY, status=202)

    # The first list call is refused; the retry after signing in must see the device list.
    original = aioclient_mock.match_request

    async def match_request(method, url, **kwargs):
        response = await original(method, url, **kwargs)
        if str(url).startswith(SIGN_IN_URL):
            accept_new_token()
        return response

    aioclient_mock.match_request = match_request
    await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()

    assert config_entry.state is ConfigEntryState.LOADED
    assert config_entry.data["login_token"] == "token-2"
    assert hass.states.get("fan.kitchen").state == "on"
    assert len(calls_to(aioclient_mock, DEVICES_URL)) == 1  # one more after the token was stored means a reload


async def test_a_refused_password_fails_setup_and_starts_reauth(hass, aioclient_mock, config_entry):
    aioclient_mock.get(DEVICES_URL, status=403, json={"code": "forbidden"})
    aioclient_mock.post(SIGN_IN_URL, status=401, json={"code": "unauthorized"})

    await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()

    assert config_entry.state is ConfigEntryState.SETUP_ERROR
    assert len(reauth_flows(hass)) == 1


async def test_a_server_error_at_startup_retries_later_without_reauth(hass, aioclient_mock, config_entry):
    aioclient_mock.get(DEVICES_URL, status=503)

    await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()

    assert config_entry.state is ConfigEntryState.SETUP_RETRY
    assert reauth_flows(hass) == []


async def test_failed_polls_double_the_interval_up_to_the_cap_and_success_resets_it(
    hass, setup, aioclient_mock, devices
):
    entry = await setup()
    coordinator = entry.runtime_data
    assert coordinator.update_interval == timedelta(seconds=60)

    aioclient_mock.clear_requests()
    aioclient_mock.get(DEVICES_URL, status=503)
    intervals = []
    for _ in range(6):
        await coordinator.async_refresh()
        intervals.append(coordinator.update_interval.total_seconds())

    assert intervals == [120, 240, 480, 900, 900, 900]
    assert hass.states.get("fan.kitchen").state == "unavailable"

    aioclient_mock.clear_requests()
    aioclient_mock.get(DEVICES_URL, json=devices)
    await coordinator.async_refresh()

    assert coordinator.update_interval == timedelta(seconds=60)
    assert hass.states.get("fan.kitchen").state == "on"


async def test_the_device_list_is_polled_once_a_minute(hass, setup, aioclient_mock, devices):
    await setup()
    before = len(calls_to(aioclient_mock, DEVICES_URL))
    devices[2]["current"]["pc"]["value"] = 700

    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=61))
    await hass.async_block_till_done()

    assert len(calls_to(aioclient_mock, DEVICES_URL)) == before + 1


async def test_a_session_rejected_at_startup_starts_reauth_but_readings_keep_working(
    hass, aioclient_mock, config_entry, devices
):
    aioclient_mock.get(DEVICES_URL, json=devices)
    aioclient_mock.get(SESSION_URL, status=202, text=LOGIN_REDIRECT_BODY)

    await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()

    assert config_entry.state is ConfigEntryState.LOADED
    assert len(reauth_flows(hass)) == 1
    assert hass.states.get("sensor.kitchen_particle_count").state == "646"
    assert hass.states.get("fan.kitchen").state == "on"


async def test_the_session_is_checked_again_every_six_hours(hass, setup, aioclient_mock):
    await setup()
    assert len(calls_to(aioclient_mock, SESSION_URL)) == 1
    assert reauth_flows(hass) == []

    aioclient_mock.clear_requests()
    aioclient_mock.get(DEVICES_URL, status=503)  # keep polling quiet; only the session check matters here
    aioclient_mock.get(SESSION_URL, status=202, text=LOGIN_REDIRECT_BODY)
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(hours=6, minutes=1))
    await hass.async_block_till_done()

    assert len(calls_to(aioclient_mock, SESSION_URL)) == 1
    assert len(reauth_flows(hass)) == 1


async def test_an_unclear_session_check_does_not_ask_for_a_new_cookie(hass, aioclient_mock, config_entry, devices):
    aioclient_mock.get(DEVICES_URL, json=devices)
    aioclient_mock.get(SESSION_URL, status=503)

    await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()

    assert config_entry.state is ConfigEntryState.LOADED
    assert reauth_flows(hass) == []


async def test_no_session_check_is_made_when_no_cookie_is_configured(hass, setup, aioclient_mock, config_entry):
    hass.config_entries.async_update_entry(
        config_entry, data={k: v for k, v in config_entry.data.items() if k != "session"}
    )

    await setup()

    assert calls_to(aioclient_mock, SESSION_URL) == []
    assert reauth_flows(hass) == []


async def test_an_entry_from_the_old_grpc_version_is_not_loaded(hass):
    old = MockConfigEntry(
        domain="iqair_cloud",
        version=1,
        data={"login_token": "x", "user_id": USER_ID, "auth_token": "y", "device_id": KITCHEN, "serial_number": "z"},
    )
    old.add_to_hass(hass)

    await hass.config_entries.async_setup(old.entry_id)
    await hass.async_block_till_done()

    assert old.state is ConfigEntryState.MIGRATION_ERROR
