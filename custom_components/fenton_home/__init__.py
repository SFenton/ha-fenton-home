"""S+H Home Integration: the S+H app's server half. Delivers Home Assistant notifications to the app through Apple Push.

The app registers as an ordinary ``mobile_app`` device whose ``push_url`` is this integration
(``views.PushView``), so every existing notify target, group and button handler works with it. The APNs key
lives here, in the config entry; nothing runs outside Home Assistant. ``notify.fenton_<person>`` targets
reach every device where that person is signed in (``notify.py``). The wire contract is ``docs/CONTRACT.md``.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

import voluptuous as vol
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_NAME, EVENT_HOMEASSISTANT_STARTED, Platform
from homeassistant.core import CoreState, Event, HomeAssistant, ServiceCall
from homeassistant.exceptions import ConfigEntryError
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import discovery
from homeassistant.helpers.typing import ConfigType
from homeassistant.helpers.httpx_client import create_async_httpx_client
from homeassistant.util.ssl import SSL_ALPN_HTTP11_HTTP2

from .apns import APNsClient, Credentials
from .const import (
    CONF_BUNDLE_ID,
    CONF_CRITICAL_ALERTS,
    CONF_KEY,
    CONF_KEY_ID,
    CONF_TEAM_ID,
    DEFAULT_BUNDLE_ID,
    DOMAIN,
    NOTIFY_SERVICE_NAME,
    SERVICE_PROBLEM,
)
from .devices import async_watch_removals
from .legacy import (
    async_import_legacy,
    async_register_alias_service,
    async_remove_alias_service,
    async_watch_registrations,
)
from .live_jobs import LiveJobs
from .payload import Timestamps
from .sweep import async_start_sweep
from .views import DailyCounter, register_views

_LOGGER = logging.getLogger(__name__)


@dataclass
class PushRuntime:
    """The loaded entry: Apple client, options and per-device counters."""

    client: APNsClient
    bundle_id: str
    options: dict[str, Any]
    counter: DailyCounter = field(default_factory=DailyCounter)
    #: Strictly increasing ``aps.timestamp`` per device and activity.
    timestamps: Timestamps = field(default_factory=Timestamps)
    #: The job engine (``live_jobs.py``), started with the entry.
    live: LiveJobs | None = None

    @property
    def critical_alerts(self) -> bool:
        return bool(self.options.get(CONF_CRITICAL_ALERTS, False))


PROBLEM_SCHEMA = vol.Schema({
    vol.Required("device"): cv.string,
    vol.Optional("code"): cv.string,
    vol.Optional("raw"): cv.string,
    vol.Optional("title"): cv.string,
    vol.Optional("message"): cv.string,
    vol.Optional("task_title"): cv.string,
    vol.Optional("url"): cv.string,
    vol.Optional("tag"): cv.string,
    vol.Optional("data"): dict,
})


def credentials(data: dict[str, Any]) -> Credentials:
    """The entry's Apple credentials."""
    return Credentials(key_pem=data[CONF_KEY], key_id=data[CONF_KEY_ID].strip(), team_id=data[CONF_TEAM_ID].strip(),
                       bundle_id=data.get(CONF_BUNDLE_ID, DEFAULT_BUNDLE_ID).strip())


def http_client(hass: HomeAssistant) -> Any:
    """An HTTP/2 client (APNs only speaks HTTP/2)."""
    return create_async_httpx_client(hass, alpn_protocols=SSL_ALPN_HTTP11_HTTP2)


CONFIG_SCHEMA = cv.empty_config_schema(DOMAIN)


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Import a leftover ``fenton_push`` entry. A background task: the import flow sets the new entry up, which waits
    for this setup to finish."""
    hass.async_create_task(async_import_legacy(hass))
    return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Start forwarding pushes and register the person targets."""
    try:
        client = APNsClient(credentials(dict(entry.data)), http_client(hass))
    except ValueError as err:
        raise ConfigEntryError("The saved APNs key can't be read; re-add the integration") from err
    runtime = PushRuntime(client=client, bundle_id=client.credentials.bundle_id, options=dict(entry.options))
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = runtime
    register_views(hass)

    hass.async_create_task(discovery.async_load_platform(
        hass, Platform.NOTIFY, DOMAIN, {CONF_NAME: NOTIFY_SERVICE_NAME, "entry_id": entry.entry_id}, {}))

    async def _refresh_targets(_: Event | None = None) -> None:
        # People load after us at startup; rebuild the person targets once everything is up.
        from homeassistant.components.notify import async_reload  # noqa: PLC0415

        await async_reload(hass, DOMAIN)

    if hass.state is CoreState.running:
        hass.async_create_task(_refresh_targets())
    else:
        entry.async_on_unload(hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STARTED, _refresh_targets))
    runtime.live = LiveJobs(hass, runtime)
    await runtime.live.async_start()

    async def _problem(call: ServiceCall) -> None:
        if (live := runtime.live) is not None:
            await live.async_problem(dict(call.data))

    hass.services.async_register(DOMAIN, SERVICE_PROBLEM, _problem, schema=PROBLEM_SCHEMA)
    await async_register_alias_service(hass, _problem, PROBLEM_SCHEMA)
    # A leftover fenton_push entry (the user step copied its key): remove it now that this one exists.
    hass.async_create_task(async_import_legacy(hass))
    entry.async_on_unload(async_watch_registrations(hass, runtime))
    entry.async_on_unload(async_watch_removals(hass, runtime))
    entry.async_on_unload(async_start_sweep(hass, runtime))
    entry.async_on_unload(entry.add_update_listener(_options_updated))
    return True


async def _options_updated(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Critical alerts and the push base URL are read live; no reload needed."""
    if runtime := hass.data.get(DOMAIN, {}).get(entry.entry_id):
        runtime.options = dict(entry.options)
        if runtime.live is not None:
            runtime.live.request()


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Stop forwarding (the push endpoint answers 503 once no entry is loaded) and drop the person targets."""
    runtime = hass.data.get(DOMAIN, {}).pop(entry.entry_id, None)
    if runtime is not None and runtime.live is not None:
        await runtime.live.async_stop()
    hass.services.async_remove(DOMAIN, SERVICE_PROBLEM)
    async_remove_alias_service(hass)
    try:
        from homeassistant.components.notify.legacy import NOTIFY_SERVICES  # noqa: PLC0415
    except ImportError:  # pragma: no cover - layout changed upstream
        return True
    services = hass.data.get(NOTIFY_SERVICES, {}).pop(DOMAIN, [])
    for service in services:
        await service.async_unregister_services()
    return True
