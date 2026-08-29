"""Constants for the Imou VD1 integration."""

DOMAIN = "imou_vd1"

CONF_DVRIP_PORT = "dvrip_port"
CONF_HTTP_PORT = "http_port"
CONF_CHANNEL = "channel"
CONF_STREAM = "stream"
CONF_IMOU_APP_ID = "imou_app_id"
CONF_IMOU_APP_SECRET = "imou_app_secret"
CONF_IMOU_DEVICE_ID = "imou_device_id"
CONF_IMOU_DATA_CENTER = "imou_data_center"

DEFAULT_DVRIP_PORT = 37777
DEFAULT_HTTP_PORT = 8086
DEFAULT_CHANNEL = 0
DEFAULT_STREAM = 0
DEFAULT_IMOU_DATA_CENTER = "fk"

PLATFORMS = ["button", "binary_sensor"]
