"""Sweep of abandoned S+H registrations.

Every sign-in that registers afresh (a new install, a reset device) leaves the previous ``mobile_app`` registration
behind, with its entities and its notify target. The sweep finds those left-behind registrations of this app (never
anyone else's) and, in ``on`` mode, removes them. Two rules, both needing the registration to have stopped updating:

* no push details (the app signed out, which clears ``app_data``) and no update for 14 days;
* the same user has a newer S+H registration of the same ``model`` and this one has no update for 7 days.

"Last update" is the newest ``last_updated`` among the registration's entities' states, else the entry's
``modified_at``. ``dry_run`` (the default) removes nothing: it logs each candidate and keeps a Repairs issue listing
them. It runs at startup and then daily.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EVENT_HOMEASSISTANT_STARTED
from homeassistant.core import CoreState, Event, HomeAssistant, callback
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.event import async_track_time_interval
from homeassistant.util import dt as dt_util

from .const import (
    CONF_SWEEP_MODE,
    DEFAULT_SWEEP_MODE,
    DOMAIN,
    ISSUE_SWEEP_CANDIDATES,
    MOBILE_APP,
    SWEEP_DAYS_NO_PUSH,
    SWEEP_DAYS_SUPERSEDED,
    SWEEP_DRY_RUN,
    SWEEP_ON,
)
from .devices import short

if TYPE_CHECKING:
    from . import PushRuntime

_LOGGER = logging.getLogger(__name__)
NO_PUSH = "no_push"
SUPERSEDED = "superseded"


@dataclass(frozen=True)
class Candidate:
    entry_id: str
    name: str
    reason: str
    last_update: datetime

    def describe(self, now: datetime) -> str:
        days = (now - self.last_update).days
        why = ("signed out" if self.reason == NO_PUSH else "replaced by a newer registration of the same model")
        return f"{self.name} ({why}, no update for {days} days)"


def has_push_details(app_data: dict[str, Any]) -> bool:
    return bool(app_data.get("push_token") and app_data.get("push_url"))


def last_update(hass: HomeAssistant, entry: ConfigEntry) -> datetime:
    """The newest state change among the registration's entities, else when the entry last changed."""
    newest: datetime | None = None
    registry = er.async_get(hass)
    for ent in er.async_entries_for_config_entry(registry, entry.entry_id):
        state = hass.states.get(ent.entity_id)
        if state is not None and (newest is None or state.last_updated > newest):
            newest = state.last_updated
    return newest or entry.modified_at


def find_candidates(hass: HomeAssistant, bundle_id: str, now: datetime | None = None) -> list[Candidate]:
    """Abandoned registrations of ``bundle_id``'s app. Other apps' registrations are never considered."""
    now = now or dt_util.utcnow()
    ours = [e for e in hass.config_entries.async_entries(MOBILE_APP) if e.data.get("app_id") == bundle_id]
    found: list[Candidate] = []
    for entry in ours:
        data = entry.data
        updated = last_update(hass, entry)
        idle = now - updated
        reason = None
        if not has_push_details(data.get("app_data") or {}) and idle >= timedelta(days=SWEEP_DAYS_NO_PUSH):
            reason = NO_PUSH
        elif idle >= timedelta(days=SWEEP_DAYS_SUPERSEDED) and any(
                other.entry_id != entry.entry_id and other.created_at > entry.created_at
                and other.data.get("user_id") == data.get("user_id") and data.get("user_id")
                and other.data.get("model") == data.get("model") and data.get("model") for other in ours):
            reason = SUPERSEDED
        if reason:
            found.append(Candidate(entry.entry_id, data.get("device_name") or entry.title, reason, updated))
    return found


async def async_sweep(hass: HomeAssistant, runtime: PushRuntime) -> list[Candidate]:
    """One pass, in the entry's current mode. Returns the candidates found."""
    mode = runtime.options.get(CONF_SWEEP_MODE, DEFAULT_SWEEP_MODE)
    if mode not in (SWEEP_DRY_RUN, SWEEP_ON):
        ir.async_delete_issue(hass, DOMAIN, ISSUE_SWEEP_CANDIDATES)
        return []
    now = dt_util.utcnow()
    candidates = find_candidates(hass, runtime.bundle_id, now)
    if mode == SWEEP_ON:
        ir.async_delete_issue(hass, DOMAIN, ISSUE_SWEEP_CANDIDATES)
        for c in candidates:
            _LOGGER.info("Sweep removed abandoned registration %s: %s", c.name, c.describe(now))
            await hass.config_entries.async_remove(c.entry_id)
        return candidates
    for c in candidates:
        _LOGGER.info("Sweep (dry run) would remove abandoned registration %s: %s", c.name, c.describe(now))
    if candidates:
        ir.async_create_issue(
            hass, DOMAIN, ISSUE_SWEEP_CANDIDATES, is_fixable=False, severity=ir.IssueSeverity.WARNING,
            translation_key=ISSUE_SWEEP_CANDIDATES,
            translation_placeholders={"count": str(len(candidates)),
                                      "devices": "; ".join(c.describe(now) for c in candidates)})
    else:
        ir.async_delete_issue(hass, DOMAIN, ISSUE_SWEEP_CANDIDATES)
    return candidates


@callback
def async_start_sweep(hass: HomeAssistant, runtime: PushRuntime) -> Any:
    """Sweep at startup (once Home Assistant is running, so entity states exist) and every day. Returns unsubscribe."""

    async def _run(_now: Any = None) -> None:
        try:
            await async_sweep(hass, runtime)
        except Exception:  # noqa: BLE001 - a sweep must never take the integration down
            _LOGGER.exception("Registration sweep failed")

    unsubs = [async_track_time_interval(hass, _run, timedelta(days=1), cancel_on_shutdown=True)]
    if hass.state is CoreState.running:
        hass.async_create_task(_run())
    else:
        @callback
        def _started(_event: Event) -> None:
            hass.async_create_task(_run())

        unsubs.append(hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STARTED, _started))

    def _stop() -> None:
        for unsub in unsubs:
            unsub()

    return _stop
