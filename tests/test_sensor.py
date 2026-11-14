"""Sensor entities, driven by the captured device list."""
import pytest

from homeassistant.const import STATE_UNAVAILABLE, UnitOfDensity

UG_M3 = UnitOfDensity.MICROGRAMS_PER_CUBIC_METER


@pytest.mark.parametrize(
    ("entity_id", "state", "unit"),
    [
        # HealthPro Plus XE
        ("sensor.primary_bedroom_pm2_5", "0", UG_M3),
        ("sensor.primary_bedroom_particle_count", "781", None),
        ("sensor.primary_bedroom_air_quality_index", "0", None),
        ("sensor.primary_bedroom_pre_filter_life", "66", "%"),
        ("sensor.primary_bedroom_gas_and_odour_filter_life", "73", "%"),
        ("sensor.primary_bedroom_main_particle_filter_life", "90", "%"),
        # Atem Earth
        ("sensor.living_room_pm2_5", "0", UG_M3),
        ("sensor.living_room_particle_count", "333", None),
        ("sensor.kitchen_particle_count", "646", None),
        ("sensor.kitchen_main_particle_filter_life", "98", "%"),
    ],
)
async def test_sensors_report_the_captured_readings(hass, setup, entity_id, state, unit):
    await setup()

    entity = hass.states.get(entity_id)

    assert entity is not None
    assert entity.state == state
    assert entity.attributes.get("unit_of_measurement") == unit


async def test_an_atem_earth_gets_one_filter_sensor_and_a_healthpro_three(hass, setup):
    await setup()

    filter_sensors = sorted(s.entity_id for s in hass.states.async_all("sensor") if s.entity_id.endswith("_life"))

    assert filter_sensors == [
        "sensor.kitchen_main_particle_filter_life",
        "sensor.living_room_main_particle_filter_life",
        "sensor.primary_bedroom_gas_and_odour_filter_life",
        "sensor.primary_bedroom_main_particle_filter_life",
        "sensor.primary_bedroom_pre_filter_life",
    ]


async def test_air_readings_go_unavailable_when_the_purifier_is_offline_but_filter_life_stays(hass, setup, devices):
    devices[2]["isConnected"] = False

    await setup()

    assert hass.states.get("sensor.kitchen_pm2_5").state == STATE_UNAVAILABLE
    assert hass.states.get("sensor.kitchen_particle_count").state == STATE_UNAVAILABLE
    assert hass.states.get("sensor.kitchen_main_particle_filter_life").state == "98"
    assert hass.states.get("sensor.living_room_pm2_5").state == "0"


async def test_a_reading_the_cloud_does_not_report_is_unknown(hass, setup, devices):
    del devices[2]["current"]["pm25"]

    await setup()

    assert hass.states.get("sensor.kitchen_pm2_5").state == "unknown"
