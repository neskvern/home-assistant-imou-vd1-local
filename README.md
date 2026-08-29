# Imou VD1 for Home Assistant

[![hacs_badge](https://img.shields.io/badge/HACS-Custom-orange.svg)](https://github.com/hacs/integration)

Custom [HACS](https://hacs.xyz/) integration for Home Assistant that supports the Imou VD1 (door view camera). Access and control are local, but the integration uses the Imou Cloud OpenAPI to wake up a sleeping camera.

The VD1 is battery powered, but this integration is best suited to a camera that is always connected to power. The integration wakes the camera via the Imou Cloud OpenAPI and exposes its video stream over an HTTP view for go2rtc to consume via manual config. Motion from the camera is exposed as a binary sensor.

Together with go2rtc and Scrypted I use this to get a complete HomeKit / HKSV enabled doorbell
for my front door.

## Installation

### Via HACS (custom repository)

1. Go to HACS → Integrations → ⋮ → Custom repositories.
2. Add this repo with category **Integration**.
3. Search for "Imou VD1" in HACS and install it.
4. Restart Home Assistant.

### Manual

Copy `custom_components/imou_vd1` to `config/custom_components/imou_vd1` in your Home Assistant installation, and restart.

## Setup

Add the integration via **Settings → Devices & Services → Add Integration → Imou VD1**.

## Development

Structure:

```
cli.py                              # Standalone CLI for testing connection.py's protocol in isolation
custom_components/imou_vd1/
├── __init__.py        # Config entry setup/teardown
├── config_flow.py      # UI-based configuration flow
├── const.py             # Constants (DOMAIN etc.)
├── connection.py          # Protocol library: wake-up, DVRIP heartbeat, HTTP/8086 streaming, eventManager
├── stream.py               # HTTP view that exposes the stream for go2rtc (manual config)
├── binary_sensor.py           # Motion sensor driven by eventManager events
├── button.py                    # Button entity to force a cloud wake-up
├── entity.py                      # Shared device_info() helper
├── manifest.json                # HA integration manifest
├── strings.json                  # Config flow UI text (source of truth)
└── translations/                  # Translations for the config flow UI (en.json)
```
