"""The 1.0 rename from Fenton Home Push (``fenton_push``): the one-time import, the aliases and their Repairs issues.

Nothing here changes behavior. The old config entry (it holds the APNs key) is copied into a ``fenton_home`` entry by
an import config flow, the live-job store moves to its new key, and the old paths and the ``problem`` service keep
answering until the owner has moved the app and the automations. Entry data and options are never logged.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any
from pathlib import Path
from urllib.parse import urlparse

from homeassistant.config_entries import SOURCE_IMPORT, ConfigEntry
from homeassistant.core import HomeAssistant, ServiceCall, callback
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.config_entries import ConfigEntryState
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers import service as service_helper
from homeassistant.util.yaml import load_yaml_dict
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.storage import Store

from .const import (
    API_PUSH,
    DOMAIN,
    ISSUE_ALIASES_CAN_GO,
    ISSUE_LEGACY_SERVICE,
    ISSUE_LEGACY_STILL_INSTALLED,
    LEGACY_DOMAIN,
    LEGACY_STORE_KEY,
    MOBILE_APP,
    SERVICE_PROBLEM,
)

if TYPE_CHECKING:
    from . import PushRuntime

_LOGGER = logging.getLogger(__name__)
STORE_VERSION = 1
_LOCK = f"{DOMAIN}_legacy_import_lock"


# -- import ----------------------------------------------------------------------------------------------------------


async def async_migrate_store(hass: HomeAssistant) -> bool:
    """Move ``fenton_push.live_jobs`` to ``fenton_home.live_jobs``: load old, save new, remove old.

    A new store that already holds data wins (the move already happened); the old one is removed either way.
    Returns whether anything was copied.
    """
    old: Store = Store(hass, STORE_VERSION, LEGACY_STORE_KEY)
    stored = await old.async_load()
    if stored is None:
        return False
    new: Store = Store(hass, STORE_VERSION, f"{DOMAIN}.live_jobs")
    copied = False
    if await new.async_load() is None:
        await new.async_save(stored)
        copied = True
    await old.async_remove()
    return copied


def legacy_loaded(hass: HomeAssistant) -> bool:
    """True while the old integration's files are still installed and its entry is running."""
    return any(e.state is ConfigEntryState.LOADED for e in hass.config_entries.async_entries(LEGACY_DOMAIN))


@callback
def async_flag_still_installed(hass: HomeAssistant) -> None:
    """Removing a loaded old entry would run its unload, which drops the shared ``notify.fenton_*`` services and the
    alias service and leaves its routes answering 503. So nothing is imported until it is uninstalled."""
    _LOGGER.warning("Fenton Home Push (%s) is still installed; remove it in HACS and restart before importing",
                    LEGACY_DOMAIN)
    ir.async_create_issue(hass, DOMAIN, ISSUE_LEGACY_STILL_INSTALLED, is_fixable=False,
                          severity=ir.IssueSeverity.ERROR, translation_key=ISSUE_LEGACY_STILL_INSTALLED)


async def async_import_legacy(hass: HomeAssistant) -> None:
    """Import every ``fenton_push`` config entry once, then remove it. Safe to run any number of times.

    The old entry may not be loaded (its files can already be gone), so only its stored ``data`` and ``options`` are
    read. The store moves first, so the new entry's job engine finds it when the import flow sets the entry up.
    """
    # Setup, the Add integration flow and a setup of the new entry may each start this; one at a time.
    async with hass.data.setdefault(_LOCK, asyncio.Lock()):
        await _import_legacy(hass)


async def _import_legacy(hass: HomeAssistant) -> None:
    old_entries = hass.config_entries.async_entries(LEGACY_DOMAIN)
    if not old_entries:
        ir.async_delete_issue(hass, DOMAIN, ISSUE_LEGACY_STILL_INSTALLED)
        return
    if legacy_loaded(hass):
        async_flag_still_installed(hass)
        return
    ir.async_delete_issue(hass, DOMAIN, ISSUE_LEGACY_STILL_INSTALLED)
    await async_migrate_store(hass)
    for old in old_entries:
        unique_id = old.unique_id
        already = any(e.unique_id == unique_id for e in hass.config_entries.async_entries(DOMAIN)) if unique_id \
            else bool(hass.config_entries.async_entries(DOMAIN))
        if not already:
            result = await hass.config_entries.flow.async_init(
                DOMAIN, context={"source": SOURCE_IMPORT},
                data={"data": dict(old.data), "options": dict(old.options), "unique_id": unique_id})
            if result["type"] is not FlowResultType.CREATE_ENTRY and result.get("reason") != "already_configured":
                # Keep the old entry: nothing is lost, and the next start tries again.
                _LOGGER.warning("Couldn't import the %s entry (%s); leaving it in place", LEGACY_DOMAIN,
                                result.get("reason") or result["type"])
                continue
        await hass.config_entries.async_remove(old.entry_id)
        _LOGGER.info("Imported the %s config entry into %s and removed it", LEGACY_DOMAIN, DOMAIN)


# -- aliases ---------------------------------------------------------------------------------------------------------


def async_register_alias_service(hass: HomeAssistant, handler: Any, schema: Any) -> None:
    """``fenton_push.problem``: the same as ``fenton_home.problem``, plus a Repairs issue naming the new service."""

    async def _alias(call: ServiceCall) -> None:
        # Something still calls the old name, so the old paths can't be said to be unused.
        ir.async_delete_issue(hass, DOMAIN, ISSUE_ALIASES_CAN_GO)
        ir.async_create_issue(
            hass, DOMAIN, ISSUE_LEGACY_SERVICE, is_fixable=False, severity=ir.IssueSeverity.WARNING,
            translation_key=ISSUE_LEGACY_SERVICE,
            translation_placeholders={"old": f"{LEGACY_DOMAIN}.{SERVICE_PROBLEM}", "new": f"{DOMAIN}.{SERVICE_PROBLEM}"})
        await handler(call)

    hass.services.async_register(LEGACY_DOMAIN, SERVICE_PROBLEM, _alias, schema=schema)
    # The old domain has no services.yaml; describe the service so Home Assistant doesn't log that it can't load one.
    description = load_yaml_dict(str(Path(__file__).parent / "services.yaml")).get(SERVICE_PROBLEM, {})
    service_helper.async_set_service_schema(hass, LEGACY_DOMAIN, SERVICE_PROBLEM, description)


def async_remove_alias_service(hass: HomeAssistant) -> None:
    hass.services.async_remove(LEGACY_DOMAIN, SERVICE_PROBLEM)


def registrations_moved(hass: HomeAssistant, bundle_id: str) -> bool:
    """True when there is at least one S+H registration with a push address and all of them use the new path."""
    urls = [str((e.data.get("app_data") or {}).get("push_url") or "")
            for e in hass.config_entries.async_entries(MOBILE_APP) if e.data.get("app_id") == bundle_id]
    urls = [u for u in urls if u]
    return bool(urls) and all(urlparse(u).path.endswith(API_PUSH) for u in urls)


@callback
def async_check_aliases(hass: HomeAssistant, bundle_id: str) -> None:
    """Raise or clear the "the old paths can go" issue from the registrations as they are now."""
    if (registrations_moved(hass, bundle_id)
            and ir.async_get(hass).async_get_issue(DOMAIN, ISSUE_LEGACY_SERVICE) is None):
        ir.async_create_issue(hass, DOMAIN, ISSUE_ALIASES_CAN_GO, is_fixable=False, severity=ir.IssueSeverity.WARNING,
                              translation_key=ISSUE_ALIASES_CAN_GO)
    else:
        ir.async_delete_issue(hass, DOMAIN, ISSUE_ALIASES_CAN_GO)


@callback
def async_watch_registrations(hass: HomeAssistant, runtime: PushRuntime) -> Any:
    """Re-check on every config entry change (a registration's ``push_url`` changes with its entry). Returns unsub."""
    from homeassistant.config_entries import SIGNAL_CONFIG_ENTRY_CHANGED  # noqa: PLC0415

    @callback
    def _changed(_change: Any, entry: ConfigEntry) -> None:
        if entry.domain == MOBILE_APP:
            async_check_aliases(hass, runtime.bundle_id)

    async_check_aliases(hass, runtime.bundle_id)
    return async_dispatcher_connect(hass, SIGNAL_CONFIG_ENTRY_CHANGED, _changed)
