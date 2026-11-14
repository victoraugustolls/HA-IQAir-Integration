"""Constants for the IQAir Cloud integration. Protocol details live in protocol.py."""
from datetime import timedelta
from typing import Final

DOMAIN: Final = "iqair_cloud"

CONF_USER_ID: Final = "user_id"
CONF_LOGIN_TOKEN: Final = "login_token"
CONF_SESSION: Final = "session"

# One request reads every purifier on the account. IQAir's own dashboard refreshes
# every 5 minutes, and Atem Earth units only report to the cloud every 30 minutes.
SCAN_INTERVAL: Final = timedelta(seconds=60)
MAX_SCAN_INTERVAL: Final = timedelta(minutes=15)
SESSION_CHECK_INTERVAL: Final = timedelta(hours=6)
