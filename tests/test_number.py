"""The fan speed number: the purifier's own levels (1-6), without percentages."""
import pytest

from homeassistant.exceptions import HomeAssistantError

from .common import KITCHEN, command_url, sent_commands

ACCEPTED = {"ok": True, "value": None}


async def set_level(hass, level):
    await hass.services.async_call(
        "number", "set_value", {"entity_id": "number.kitchen_fan_speed", "value": level}, blocking=True
    )


async def test_fan_speed_shows_the_level_on_a_one_to_six_slider(hass, setup):
    await setup()

    kitchen = hass.states.get("number.kitchen_fan_speed")
    healthpro = hass.states.get("number.primary_bedroom_fan_speed")

    assert kitchen.state == "4"
    assert healthpro.state == "6"
    assert (kitchen.attributes["min"], kitchen.attributes["max"], kitchen.attributes["step"]) == (1, 6, 1)
    assert kitchen.attributes["mode"] == "slider"
    assert kitchen.attributes.get("unit_of_measurement") is None


async def test_setting_a_level_sends_exactly_that_level_and_the_fan_entity_follows(hass, setup, aioclient_mock):
    await setup()
    aioclient_mock.post(command_url(KITCHEN), json=ACCEPTED)

    await set_level(hass, 3)

    assert sent_commands(aioclient_mock) == [(command_url(KITCHEN), {"command": "setFanSpeed", "level": 3})]
    assert hass.states.get("number.kitchen_fan_speed").state == "3"
    assert hass.states.get("fan.kitchen").attributes["percentage"] == 50


async def test_setting_a_level_on_a_standby_purifier_powers_it_on_first(hass, setup, aioclient_mock, devices):
    devices[2]["remote"]["powerMode"] = 3
    await setup()
    aioclient_mock.post(command_url(KITCHEN), json=ACCEPTED)

    await set_level(hass, 2)

    assert [body for _, body in sent_commands(aioclient_mock)] == [
        {"command": "setPowerMode", "power": "on"},
        {"command": "setFanSpeed", "level": 2},
    ]


async def test_in_smart_mode_it_shows_the_level_the_fan_is_running_at(hass, setup, devices):
    devices[2]["remote"].update(autoModeEnabled=True, speedLevel=4)
    devices[2]["current"]["fanSpeed"] = 2

    await setup()

    assert hass.states.get("number.kitchen_fan_speed").state == "2"


async def test_fan_speed_is_unavailable_while_the_purifier_is_offline(hass, setup, devices):
    devices[2]["isConnected"] = False

    await setup()

    assert hass.states.get("number.kitchen_fan_speed").state == "unavailable"
    assert hass.states.get("number.living_room_fan_speed").state == "4"


async def test_a_refused_level_raises_and_keeps_the_old_value(hass, setup, aioclient_mock):
    await setup()
    aioclient_mock.post(command_url(KITCHEN), json={"ok": False, "error": "Device is offline"})

    with pytest.raises(HomeAssistantError, match="Device is offline"):
        await set_level(hass, 3)

    assert hass.states.get("number.kitchen_fan_speed").state == "4"
