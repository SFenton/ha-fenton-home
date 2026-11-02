"""Device lifecycle: forgetting a removed S+H registration.

A ``mobile_app`` config entry can disappear three ways: the app's unregister call (``views.UnregisterView``), an admin
in Settings, or the sweep (``sweep.py``). Whichever it was, the entry's state held here is dropped the same way: the
config entry change signal fires ``purge`` for every removed ``mobile_app`` entry.

Core already drops the registration's own Live Activity tokens (``mobile_app`` ``async_remove_entry``), and the
push-to-start token lives in the entry's ``app_data``, so what is left to clear is this integration's own state keyed
by webhook id: job cards and their timers, the daily push counters, the timestamp counters and the "no notify
action" warning memory.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from homeassistant.config_entries import SIGNAL_CONFIG_ENTRY_CHANGED, ConfigEntry, ConfigEntryChange
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_connect

from .const import MOBILE_APP

if TYPE_CHECKING:
    from . import PushRuntime

_LOGGER = logging.getLogger(__name__)


def short(webhook_id: str) -> str:
    """The most of a webhook id that may be logged (it is a credential)."""
    return webhook_id[:6]


@callback
def purge(hass: HomeAssistant, runtime: PushRuntime, webhook_id: str) -> None:
    """Drop everything this integration keeps for ``webhook_id``."""
    runtime.counter.forget(webhook_id)
    runtime.timestamps.forget_prefix(f"{webhook_id}:")
    if (live := runtime.live) is not None:
        live.forget_webhook(webhook_id)
    _LOGGER.debug("Dropped state for removed registration %s…", short(webhook_id))


@callback
def async_watch_removals(hass: HomeAssistant, runtime: PushRuntime) -> Any:
    """Purge on every ``mobile_app`` entry removal. Returns the unsubscribe function."""

    @callback
    def _changed(change: ConfigEntryChange, entry: ConfigEntry) -> None:
        if change is ConfigEntryChange.REMOVED and entry.domain == MOBILE_APP \
                and (webhook_id := entry.data.get("webhook_id")):
            purge(hass, runtime, webhook_id)

    return async_dispatcher_connect(hass, SIGNAL_CONFIG_ENTRY_CHANGED, _changed)
