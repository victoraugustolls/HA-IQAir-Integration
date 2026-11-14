"""Shared test setup.

tests/test_protocol.py runs with plain pytest. Everything else needs the Home Assistant
test harness (`pip install pytest-homeassistant-custom-component`) and is skipped
without it.
"""
import json
import pathlib
import sys

import pytest

from .common import DEVICES_URL, ENTRY_DATA, SESSION_URL, USER_ID

ROOT = pathlib.Path(__file__).resolve().parents[1]
FIXTURES = pathlib.Path(__file__).parent / "fixtures"
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# Import this repository's custom_components package before any test runs. The Home
# Assistant test harness later puts its own (empty) custom_components folder first on
# sys.path; whichever package is imported first is the one Home Assistant scans.
import custom_components  # noqa: E402,F401

try:
    import pytest_homeassistant_custom_component  # noqa: F401
except ImportError:
    HAVE_HOME_ASSISTANT = False
else:
    HAVE_HOME_ASSISTANT = True

collect_ignore = [] if HAVE_HOME_ASSISTANT else [
    "test_api.py", "test_config_flow.py", "test_fan.py", "test_init.py", "test_number.py", "test_sensor.py",
]

if HAVE_HOME_ASSISTANT:
    from pytest_homeassistant_custom_component.common import MockConfigEntry

    @pytest.fixture(autouse=True)
    def _enable_custom_integrations(enable_custom_integrations):
        """Let Home Assistant load custom_components/iqair_cloud in every test."""
        yield

    @pytest.fixture
    def config_entry(hass):
        entry = MockConfigEntry(
            domain="iqair_cloud", version=2, unique_id=USER_ID, title="me@example.com", data=dict(ENTRY_DATA)
        )
        entry.add_to_hass(hass)
        return entry

    @pytest.fixture
    def devices(legacy_devices):
        """The captured device list; tests may edit it before `setup` runs."""
        return legacy_devices

    @pytest.fixture
    def setup(hass, aioclient_mock, config_entry, devices, dashboard_devices_text):
        """Return a coroutine function that loads the integration against mocked IQAir hosts."""

        async def _setup():
            aioclient_mock.get(DEVICES_URL, json=devices)
            aioclient_mock.get(SESSION_URL, text=dashboard_devices_text)
            await hass.config_entries.async_setup(config_entry.entry_id)
            await hass.async_block_till_done()
            return config_entry

        return _setup


@pytest.fixture
def legacy_devices_text() -> str:
    """The old API's device list, captured from a real account and sanitized."""
    return (FIXTURES / "legacy_devices.json").read_text()


@pytest.fixture
def legacy_devices(legacy_devices_text) -> list:
    return json.loads(legacy_devices_text)


@pytest.fixture
def dashboard_devices_text() -> str:
    """The dashboard's device list for a valid session, captured and sanitized."""
    return (FIXTURES / "dashboard_devices.data.json").read_text()
