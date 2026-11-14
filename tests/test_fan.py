"""The fan entity: what it shows, and exactly which commands each service sends."""
import pytest

from homeassistant.exceptions import HomeAssistantError

from .common import KITCHEN, LOGIN_REDIRECT_BODY, command_url, reauth_flows, sent_commands

ACCEPTED = {"ok": True, "value": None}


async def call(hass, service, **data):
    await hass.services.async_call("fan", service, {"entity_id": "fan.kitchen", **data}, blocking=True)


# --- state ---------------------------------------------------------------------------


async def test_a_running_purifier_shows_on_with_its_level_as_a_percentage(hass, setup):
    await setup()

    healthpro = hass.states.get("fan.primary_bedroom")
    kitchen = hass.states.get("fan.kitchen")

    assert (healthpro.state, healthpro.attributes["percentage"]) == ("on", 100)  # level 6 of 6
    assert (kitchen.state, kitchen.attributes["percentage"]) == ("on", 66)  # level 4 of 6
    assert kitchen.attributes["percentage_step"] == pytest.approx(100 / 6)


async def test_manual_mode_has_no_preset_and_offers_the_three_smart_profiles(hass, setup):
    await setup()

    kitchen = hass.states.get("fan.kitchen")

    assert kitchen.attributes["preset_mode"] is None
    assert kitchen.attributes["preset_modes"] == ["quiet", "balanced", "max"]


async def test_smart_mode_shows_its_profile_as_the_preset(hass, setup, devices):
    devices[2]["remote"].update(autoModeEnabled=True, autoModeProfile=3)

    await setup()

    assert hass.states.get("fan.kitchen").attributes["preset_mode"] == "max"


async def test_smart_mode_reports_the_speed_the_fan_is_running_at(hass, setup, devices):
    devices[2]["remote"].update(autoModeEnabled=True, speedLevel=4)
    devices[2]["current"]["fanSpeed"] = 2

    await setup()

    assert hass.states.get("fan.kitchen").attributes["percentage"] == 33  # level 2 of 6


async def test_standby_shows_off_at_zero_percent(hass, setup, devices):
    devices[2]["remote"]["powerMode"] = 3

    await setup()

    kitchen = hass.states.get("fan.kitchen")
    assert (kitchen.state, kitchen.attributes["percentage"]) == ("off", 0)


async def test_an_offline_purifier_is_unavailable_while_the_others_stay_usable(hass, setup, devices):
    devices[2]["isConnected"] = False

    await setup()

    assert hass.states.get("fan.kitchen").state == "unavailable"
    assert hass.states.get("fan.living_room").state == "on"


async def test_a_purifier_without_smart_mode_offers_no_presets(hass, setup, devices):
    devices[2]["featureSet"]["isAutoModeSupported"] = 0

    await setup()

    assert hass.states.get("fan.kitchen").attributes.get("preset_modes") is None


# --- commands --------------------------------------------------------------------------


async def test_set_percentage_sends_the_matching_level_and_shows_it_at_once(hass, setup, aioclient_mock):
    await setup()
    aioclient_mock.post(command_url(KITCHEN), json=ACCEPTED)

    await call(hass, "set_percentage", percentage=50)

    assert sent_commands(aioclient_mock) == [(command_url(KITCHEN), {"command": "setFanSpeed", "level": 3})]
    assert hass.states.get("fan.kitchen").attributes["percentage"] == 50


@pytest.mark.parametrize(("percentage", "level"), [(1, 1), (17, 2), (34, 3), (67, 5), (100, 6)])
async def test_set_percentage_rounds_up_to_the_next_level(hass, setup, aioclient_mock, percentage, level):
    await setup()
    aioclient_mock.post(command_url(KITCHEN), json=ACCEPTED)

    await call(hass, "set_percentage", percentage=percentage)

    assert sent_commands(aioclient_mock) == [(command_url(KITCHEN), {"command": "setFanSpeed", "level": level})]


async def test_set_percentage_leaves_smart_mode(hass, setup, aioclient_mock, devices):
    devices[2]["remote"].update(autoModeEnabled=True, autoModeProfile=1)
    await setup()
    aioclient_mock.post(command_url(KITCHEN), json=ACCEPTED)

    await call(hass, "set_percentage", percentage=100)

    assert hass.states.get("fan.kitchen").attributes["preset_mode"] is None


async def test_turn_off_sends_standby(hass, setup, aioclient_mock):
    await setup()
    aioclient_mock.post(command_url(KITCHEN), json=ACCEPTED)

    await call(hass, "turn_off")

    assert sent_commands(aioclient_mock) == [(command_url(KITCHEN), {"command": "setPowerMode", "power": "standby"})]
    assert hass.states.get("fan.kitchen").state == "off"


async def test_set_percentage_zero_turns_the_purifier_off(hass, setup, aioclient_mock):
    await setup()
    aioclient_mock.post(command_url(KITCHEN), json=ACCEPTED)

    await call(hass, "set_percentage", percentage=0)

    assert sent_commands(aioclient_mock) == [(command_url(KITCHEN), {"command": "setPowerMode", "power": "standby"})]


async def test_turn_on_sends_power_on(hass, setup, aioclient_mock, devices):
    devices[2]["remote"]["powerMode"] = 3
    await setup()
    aioclient_mock.post(command_url(KITCHEN), json=ACCEPTED)

    await call(hass, "turn_on")

    assert sent_commands(aioclient_mock) == [(command_url(KITCHEN), {"command": "setPowerMode", "power": "on"})]
    assert hass.states.get("fan.kitchen").state == "on"


async def test_setting_a_speed_on_a_standby_purifier_powers_it_on_first(hass, setup, aioclient_mock, devices):
    devices[2]["remote"]["powerMode"] = 3
    await setup()
    aioclient_mock.post(command_url(KITCHEN), json=ACCEPTED)

    await call(hass, "turn_on", percentage=100)

    assert [body for _, body in sent_commands(aioclient_mock)] == [
        {"command": "setPowerMode", "power": "on"},
        {"command": "setFanSpeed", "level": 6},
    ]


async def test_choosing_a_preset_enables_smart_mode_then_sets_the_profile(hass, setup, aioclient_mock):
    await setup()
    aioclient_mock.post(command_url(KITCHEN), json=ACCEPTED)

    await call(hass, "set_preset_mode", preset_mode="max")

    assert [body for _, body in sent_commands(aioclient_mock)] == [
        {"command": "setAutoMode", "enabled": True},
        {"command": "setAutoModeProfile", "profile": "power"},
    ]
    assert hass.states.get("fan.kitchen").attributes["preset_mode"] == "max"


async def test_choosing_the_current_profile_only_enables_smart_mode(hass, setup, aioclient_mock):
    await setup()  # the captured Kitchen is in manual mode with profile "balanced" stored
    aioclient_mock.post(command_url(KITCHEN), json=ACCEPTED)

    await call(hass, "set_preset_mode", preset_mode="balanced")

    assert [body for _, body in sent_commands(aioclient_mock)] == [{"command": "setAutoMode", "enabled": True}]


async def test_changing_the_preset_in_smart_mode_only_sets_the_profile(hass, setup, aioclient_mock, devices):
    devices[2]["remote"].update(autoModeEnabled=True, autoModeProfile=2)
    await setup()
    aioclient_mock.post(command_url(KITCHEN), json=ACCEPTED)

    await call(hass, "set_preset_mode", preset_mode="quiet")

    assert [body for _, body in sent_commands(aioclient_mock)] == [
        {"command": "setAutoModeProfile", "profile": "quiet"}
    ]


# --- failures --------------------------------------------------------------------------


async def test_a_sequence_stops_at_the_first_command_the_device_refuses(hass, setup, aioclient_mock, devices):
    devices[2]["remote"]["powerMode"] = 3
    await setup()
    aioclient_mock.post(command_url(KITCHEN), json={"ok": False, "error": "Device is offline"})

    with pytest.raises(HomeAssistantError, match="Device is offline"):
        await call(hass, "turn_on", percentage=100)

    assert [body for _, body in sent_commands(aioclient_mock)] == [{"command": "setPowerMode", "power": "on"}]
    assert hass.states.get("fan.kitchen").state == "off"


@pytest.mark.parametrize(("status", "body"), [(302, ""), (202, LOGIN_REDIRECT_BODY)])
async def test_an_expired_session_fails_the_command_and_asks_for_a_new_cookie(
    hass, setup, aioclient_mock, status, body
):
    await setup()
    aioclient_mock.post(command_url(KITCHEN), status=status, text=body)

    with pytest.raises(HomeAssistantError):
        await call(hass, "set_percentage", percentage=50)

    await hass.async_block_till_done()
    assert len(reauth_flows(hass)) == 1
    assert hass.states.get("fan.kitchen").attributes["percentage"] == 66
    assert hass.states.get("sensor.kitchen_main_particle_filter_life").state == "98"


async def test_without_a_session_cookie_the_state_shows_but_commands_are_refused_locally(
    hass, setup, aioclient_mock, config_entry
):
    hass.config_entries.async_update_entry(
        config_entry, data={k: v for k, v in config_entry.data.items() if k != "session"}
    )
    await setup()
    aioclient_mock.post(command_url(KITCHEN), json=ACCEPTED)

    with pytest.raises(HomeAssistantError):
        await call(hass, "turn_off")

    assert sent_commands(aioclient_mock) == []
    assert hass.states.get("fan.kitchen").state == "on"
    assert reauth_flows(hass) == []


async def test_a_server_error_fails_the_command_without_asking_for_a_new_cookie(hass, setup, aioclient_mock):
    await setup()
    aioclient_mock.post(command_url(KITCHEN), status=503)

    with pytest.raises(HomeAssistantError):
        await call(hass, "turn_off")

    await hass.async_block_till_done()
    assert reauth_flows(hass) == []
    assert hass.states.get("fan.kitchen").state == "on"
