# Home Assistant IQAir Cloud Integration

Read and control IQAir air purifiers from Home Assistant through IQAir's cloud.

Version 2 is a rewrite. IQAir replaced its dashboard in late October 2026 and the login
flow of the 1.x versions stopped working (issue #7), so this version reads and controls
purifiers a different way. Entries created by 1.x cannot be migrated: delete them and add
the integration again.

**This uses undocumented IQAir APIs. It can break whenever IQAir changes them.**

## What you get

For every purifier on the account:

- **Fan**: on/off, speed as a percentage (the purifier's own levels, 6 on the models
  below), and preset modes **Quiet**, **Balanced** and **Max** for smart mode. Setting a
  speed returns the purifier to manual mode.
- **Fan speed** (number): the same speed as a plain 1-6 slider. Home Assistant's fan
  entity can only express speed as a percentage, which for six levels lands on values
  like 33% and 67%; this control shows the level itself.
- **Sensors**: PM2.5, particle count, AQI (US), and the remaining life of each filter.

Tested against real devices on 2026-11-14:

| Model | IQAir code | Fan levels | Filters |
|---|---|---|---|
| HealthPro Plus XE (HP250XE) | `ui2` | 6 | Pre-filter, gas and odour, main particle |
| Atem Earth | `wap` | 6 | Main particle |

Other models that IQAir's dashboard can control (Atem X, other XE units) should work the
same way but have not been tried.

## How it talks to IQAir

Two different things are needed, because IQAir split them:

| | What it uses | When it expires |
|---|---|---|
| **Reading** state and sensors | Your IQAir **email and password**, against the old website API | Renewed automatically by signing in again |
| **Controlling** a purifier | The **session cookie** of a signed-in IQAir dashboard | You paste a new one. Lifetime unknown |

A password sign-in cannot produce the dashboard session (it is signed by IQAir's server),
and IQAir's login page is bot-protected, so the cookie has to come from your browser.

If the session expires, only control stops: Home Assistant shows a re-authentication
prompt, and the sensors and the purifier's current state keep updating. The session is
checked at startup and every 6 hours, so the prompt appears before an automation fails.

Both the password and the session cookie are stored in Home Assistant's config entry.
The session cookie is a full login to your IQAir account, not a limited API token.

## Installation

### HACS (custom repository)

1. HACS → menu → **Custom repositories**. Add `ThioJoe/HA-IQAir-Integration` with type
   **Integration**.
2. Install **IQAir Cloud Integration**, then restart Home Assistant.

### Manual

Copy `custom_components/iqair_cloud` into your Home Assistant `custom_components` folder
and restart Home Assistant.

## Setup

**Settings → Devices & Services → Add Integration → IQAir Cloud**, then enter:

- **Email** and **Password** of your IQAir account. If you normally sign in with Google,
  Apple or Facebook, set a password on the account first.
- **Dashboard session cookie** (optional). Without it you get sensors and state, but
  commands are refused. You can add it later with **Reconfigure**.

### Getting the session cookie

1. Sign in at [dashboard.iqair.com](https://dashboard.iqair.com/) and open the Devices
   page.
2. Open the browser's developer tools, go to the **Network** tab, and reload the page.
3. Click the first request in the list (`devices`). Under **Request Headers**, right-click
   **Cookie** and choose **Copy value**.
4. Paste it into the form. You can paste the whole header; only the `session` cookie is
   kept.

Logging out of the dashboard may invalidate the cookie. Closing the browser tab does not.

## Things to know

- **Atem Earth readings are slow.** Those units report to IQAir's cloud every 30 minutes,
  so their PM2.5 and particle count can be that old. HealthPro XE units report every few
  minutes.
- **Polling.** One request per minute reads every purifier. After errors the interval
  doubles, up to 15 minutes.
- **Particle count has no unit.** IQAir's API labels it `ppb` and its dashboard `/L`.
- **After a command** the entity shows the new state immediately; the next poll confirms
  it.
- **Not included:** display light, panel lock and brightness controls (the dashboard has
  them; the commands are documented in `protocol.py`).
- **Serial numbers** are never stored or shown. IQAir's older command API treats the
  serial as the only thing identifying a purifier.

## When it breaks

Everything IQAir-specific lives in `custom_components/iqair_cloud/protocol.py`.

`Tools/iqair_diag.py` is a standalone script (no Home Assistant needed) that walks
sign-in, device list, device state, the live stream and the session check one request at
a time and prints the HTTP status and a redacted body for each. Credentials are read from
environment variables only:

```sh
IQAIR_EMAIL=... IQAIR_PASSWORD=... IQAIR_COOKIE=... python3 Tools/iqair_diag.py
```

## Development

```sh
pip install pytest-homeassistant-custom-component
pytest
```

`tests/test_protocol.py` runs with plain `pytest` and no Home Assistant. The fixtures in
`tests/fixtures` are real responses with ids, serials, account and network details
replaced.

## Extras

- `Tools/gRPC Decode.html`: decodes payloads of IQAir's older gRPC-web command API. That
  API still exists but needs an app token the dashboard no longer publishes.
