"""Constants for the eduVULCAN integration."""

from datetime import timedelta

DOMAIN = "eduvulcan"

CONF_CREDENTIAL = "credential"
CONF_TENANT = "tenant"
CONF_TOKENS = "tokens"
# Options: eduVULCAN login used only for the web messages module.
CONF_USERNAME = "username"
CONF_PASSWORD = "password"

DEFAULT_UPDATE_INTERVAL = timedelta(minutes=30)

# How far ahead we look when fetching data.
SCHEDULE_DAYS_AHEAD = 7
HOMEWORK_DAYS_AHEAD = 14
EXAMS_DAYS_AHEAD = 14

# Fake device identity presented to the Vulcan API (mimics the mobile app).
DEVICE_OS = "Android"
DEVICE_MODEL = "SM-A525F"

MANUFACTURER = "VULCAN"
