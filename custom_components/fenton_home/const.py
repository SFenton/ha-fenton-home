"""Constants for S+H Home Integration."""

from __future__ import annotations

DOMAIN = "fenton_home"

CONF_KEY = "key"
CONF_KEY_ID = "key_id"
CONF_TEAM_ID = "team_id"
CONF_BUNDLE_ID = "bundle_id"
#: Options.
CONF_CRITICAL_ALERTS = "critical_alerts"
CONF_PUSH_BASE_URL = "push_base_url"
#: Job cards (``live_jobs.py``): the master switch, which jobs run, which registrations get cards (empty: all with
#: S+H), and the notify action that reaches a person's whole phone (``*`` stands for their first name).
CONF_LIVE_JOBS_ENABLED = "live_jobs_enabled"
CONF_LIVE_JOBS = "live_jobs"
CONF_LIVE_DEVICES = "live_devices"
CONF_PERSON_NOTIFY = "person_notify"
#: Abandoned-registration sweep (``sweep.py``): ``off``, ``dry_run`` (log and list, remove nothing) or ``on``.
CONF_SWEEP_MODE = "sweep_mode"
SWEEP_OFF, SWEEP_DRY_RUN, SWEEP_ON = "off", "dry_run", "on"
SWEEP_MODES = (SWEEP_OFF, SWEEP_DRY_RUN, SWEEP_ON)
DEFAULT_SWEEP_MODE = SWEEP_DRY_RUN
#: Days without an update before a registration may be swept: no push details / superseded by a newer same-model one.
SWEEP_DAYS_NO_PUSH = 14
SWEEP_DAYS_SUPERSEDED = 7

DEFAULT_BUNDLE_ID = "com.sfenton.home"

#: ``app_data`` key the app sets to ``sandbox`` (Xcode installs) or ``production`` (TestFlight, App Store).
APP_DATA_ENVIRONMENT = "apns_environment"

#: Our devices' ``device_name`` suffix, which keeps their notify services apart from the official app's.
DEVICE_NAME_SUFFIX = " · Fenton"

#: Base notify service; person targets become ``notify.fenton_<name>``.
NOTIFY_SERVICE_NAME = "fenton"
TARGET_EVERYONE = "everyone"

#: Pushes per device per UTC day, a guard against runaway automations (Apple sets no fixed cap).
DAILY_LIMIT = 1000

API_CONFIG = "/api/fenton_home/config"
API_PUSH = "/api/fenton_home/apns"
API_UNREGISTER = "/api/fenton_home/devices/unregister"
#: Unauthenticated unregister calls allowed per client address per minute.
UNREGISTER_PER_MINUTE = 10

#: The names this integration had before 1.0 (Fenton Home Push). The paths and the problem service stay as aliases
#: while registrations and automations move; the config entry and the store are imported once (``legacy.py``).
LEGACY_DOMAIN = "fenton_push"
LEGACY_API_CONFIG = "/api/fenton_push/config"
LEGACY_API_PUSH = "/api/fenton_push/apns"
LEGACY_STORE_KEY = "fenton_push.live_jobs"

#: Repairs issue ids.
ISSUE_LEGACY_SERVICE = "legacy_problem_service"
ISSUE_ALIASES_CAN_GO = "legacy_paths_unused"

MOBILE_APP = "mobile_app"

#: Services and events of the job engine.
SERVICE_PROBLEM = "problem"
EVENT_ACTION = "fenton_live_activity_action"
EVENT_DISMISSED = "fenton_live_activity_dismissed"
SENSOR_JOBS = "sensor.fenton_live_jobs"
ISSUE_LEGACY_STILL_INSTALLED = "legacy_still_installed"
ISSUE_SWEEP_CANDIDATES = "sweep_candidates"

#: Presence auto-enrolment (``presence.py``): the master switch (off until the owner turns it on), dry run (log what
#: would change, change nothing; on by default) and the trackers kept out of every composite.
CONF_PRESENCE_ENABLED = "presence_enabled"
CONF_PRESENCE_DRY_RUN = "presence_dry_run"
CONF_PRESENCE_EXCLUDE = "presence_exclude"
DEFAULT_PRESENCE_EXCLUDE = ["device_tracker.iphone_18_pro", "device_tracker.iphone_17_pro", "device_tracker.ipad"]
ISSUE_PRESENCE_COMPOSITE_MISSING = "presence_composite_missing"
ISSUE_PRESENCE_NO_COMPOSITE = "presence_no_composite"
ISSUE_PRESENCE_COMPOSITE_YAML = "presence_composite_yaml"

#: The house configuration document (``house_config.py``): a folder under the Home Assistant config directory,
#: the files tried in order (YAML wins), the size limit, the websocket commands, the admin service and its Repair.
HOUSE_CONFIG_DIR = "fenton_home"
HOUSE_CONFIG_FILES = ("house.yaml", "house.json")
HOUSE_CONFIG_MAX_BYTES = 256 * 1024
WS_HOUSE_CONFIG = "fenton_home/house_config"
WS_HOUSE_CONFIG_SUBSCRIBE = "fenton_home/house_config/subscribe"
SERVICE_RELOAD_HOUSE_CONFIG = "reload_house_config"
ISSUE_HOUSE_CONFIG_INVALID = "house_config_invalid"
