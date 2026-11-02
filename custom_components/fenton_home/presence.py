"""Presence auto-enrolment: each person follows one Composite tracker that merges all of their phones and tablets.

Without this, ``mobile_app`` adds a new registration's ``device_tracker`` straight to its user's person
(``person.async_add_user_device_tracker``), so the person follows whichever device reported last and a phone left at
home flips the house between Away and Home. The fix is a ``composite`` tracker (pnbruckner/ha-composite-tracker,
``require_movement``) per person; this module keeps it and the person in step with the registrations:

1. registration -> ``entry.data["user_id"]`` -> the person with that ``user_id`` (the same ownership 1.1 uses);
2. the person's composite inputs = every ``mobile_app`` tracker of that user (Companion and S+H) minus the exclude list;
3. the person's ``device_trackers`` = no ``mobile_app`` tracker at all (the composite stays, as do trackers from other
   integrations a person was given on purpose);
4. a registration removed by 1.1 (unregister, sweep, admin) drops out through the same path, because removing its
   entities fires the same registry event.

It runs at startup, on every ``device_tracker`` entity-registry create/remove, and when a person changes (that is how
the auto-add is noticed and undone). It makes no write when nothing differs and logs each change at info.

Only a Composite *UI* config entry is written. An entry imported from YAML has its options replaced from the YAML on
every ``composite.reload`` and restart, so it is reported (a Repairs issue and the log) and left alone: one source of
truth means moving the composites from YAML to the UI first (README, "Presence").
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING, Any

from homeassistant.config_entries import SOURCE_IMPORT, ConfigEntry
from homeassistant.const import EVENT_HOMEASSISTANT_STARTED
from homeassistant.core import CoreState, Event, HomeAssistant, callback
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.event import async_call_later
from homeassistant.loader import IntegrationNotFound, async_get_integration

from .const import (
    CONF_PRESENCE_DRY_RUN,
    CONF_PRESENCE_ENABLED,
    CONF_PRESENCE_EXCLUDE,
    DEFAULT_PRESENCE_EXCLUDE,
    DOMAIN,
    ISSUE_PRESENCE_COMPOSITE_MISSING,
    ISSUE_PRESENCE_COMPOSITE_YAML,
    ISSUE_PRESENCE_NO_COMPOSITE,
    MOBILE_APP,
)

if TYPE_CHECKING:
    from . import PushRuntime

_LOGGER = logging.getLogger(__name__)

COMPOSITE = "composite"
PERSON = "person"
TRACKER = "device_tracker"
#: Composite's option keys (``composite/const.py``).
CONF_ENTITY_ID, CONF_ENTITY, CONF_ALL_STATES, CONF_USE_PICTURE = "entity_id", "entity", "all_states", "use_picture"
#: Wait this long after the last trigger so ``mobile_app``'s own person update (which follows the entity's creation)
#: lands first.
SETTLE_SECONDS = 2.0


@dataclass(frozen=True)
class Action:
    """One change the pass makes (or, in a dry run, would make)."""

    kind: str  # "composite" or "person"
    target: str  # config entry id / person id
    description: str


@dataclass
class Plan:
    """What a pass found: the changes, and whether the composite writes it wants are possible."""

    actions: list[Action] = field(default_factory=list)
    #: Writes to apply: (composite entry, new options) and (person id, new tracker list).
    composite_writes: list[tuple[ConfigEntry, dict[str, Any]]] = field(default_factory=list)
    person_writes: list[tuple[str, list[str]]] = field(default_factory=list)
    #: Repairs issues wanted: issue id -> (translation key, placeholders).
    issues: dict[str, tuple[str, dict[str, str]]] = field(default_factory=dict)


def settings(options: dict[str, Any]) -> tuple[bool, bool, set[str]]:
    """(enabled, dry run, excluded entity ids) from the entry's options."""
    return (bool(options.get(CONF_PRESENCE_ENABLED, False)), bool(options.get(CONF_PRESENCE_DRY_RUN, True)),
            set(options.get(CONF_PRESENCE_EXCLUDE, DEFAULT_PRESENCE_EXCLUDE) or []))


def mobile_trackers(hass: HomeAssistant) -> dict[str, str]:
    """Every enabled ``mobile_app`` tracker with its registration's owner: entity id -> user id ('' when unowned)."""
    found: dict[str, str] = {}
    for ent in er.async_get(hass).entities.values():
        if ent.domain != TRACKER or ent.platform != MOBILE_APP or ent.disabled_by is not None:
            continue
        entry = hass.config_entries.async_get_entry(ent.config_entry_id) if ent.config_entry_id else None
        found[ent.entity_id] = (entry.data.get("user_id") or "") if entry else ""
    return found


def _composite_of(hass: HomeAssistant, trackers: list[str]) -> tuple[str, ConfigEntry] | None:
    """The first of a person's trackers that is a Composite tracker, with its config entry."""
    registry = er.async_get(hass)
    for entity_id in trackers:
        ent = registry.async_get(entity_id)
        if ent is not None and ent.platform == COMPOSITE and ent.config_entry_id:
            entry = hass.config_entries.async_get_entry(ent.config_entry_id)
            if entry is not None:
                return entity_id, entry
    return None


def _is_foreign(hass: HomeAssistant, entity_id: str) -> bool:
    """An input this module leaves alone: not a ``mobile_app`` tracker and still known to Home Assistant."""
    ent = er.async_get(hass).async_get(entity_id)
    if ent is not None:
        return ent.platform != MOBILE_APP
    return hass.states.get(entity_id) is not None


def _input_cfgs(entry: ConfigEntry) -> list[dict[str, Any]]:
    return [dict(cfg) for cfg in entry.options.get(CONF_ENTITY_ID, [])]


def _persons(hass: HomeAssistant) -> list[dict[str, Any]]:
    """People stored in Home Assistant (the ones ``person/update`` can change)."""
    data = hass.data.get(PERSON)
    return list(data[1].async_items()) if data else []


async def composite_installed(hass: HomeAssistant) -> bool:
    try:
        await async_get_integration(hass, COMPOSITE)
    except IntegrationNotFound:
        return False
    return True


async def async_plan(hass: HomeAssistant, options: dict[str, Any]) -> Plan:
    """Work out the changes for the current registrations. Changes nothing."""
    _, _, exclude = settings(options)
    plan = Plan()
    if not await composite_installed(hass):
        plan.issues[ISSUE_PRESENCE_COMPOSITE_MISSING] = (ISSUE_PRESENCE_COMPOSITE_MISSING, {})
        return plan
    mobile = mobile_trackers(hass)
    yaml_managed: list[str] = []
    for person in _persons(hass):
        user_id = person.get("user_id")
        if not user_id:
            continue
        pid, name = person["id"], person.get("name") or person["id"]
        current_trackers: list[str] = list(person.get("device_trackers") or [])
        owned = sorted(t for t, owner in mobile.items() if owner == user_id and t not in exclude)
        found = _composite_of(hass, current_trackers)
        if found is None:
            if owned or any(t in mobile for t in current_trackers):
                plan.issues[f"{ISSUE_PRESENCE_NO_COMPOSITE}_{pid}"] = (ISSUE_PRESENCE_NO_COMPOSITE, {"person": name})
                _LOGGER.debug("Presence: %s has no composite tracker, left alone", name)
            continue
        composite_id, entry = found

        # 2. The composite's inputs.
        cfgs = _input_cfgs(entry)
        present = [cfg[CONF_ENTITY] for cfg in cfgs]
        keep = [cfg for cfg in cfgs if _is_foreign(hass, cfg[CONF_ENTITY])]
        kept_ids = {cfg[CONF_ENTITY] for cfg in keep}
        new_ids = kept_ids | set(owned)
        covered = set(present)
        writable = entry.source != SOURCE_IMPORT
        if new_ids != set(present):
            added, dropped = sorted(new_ids - set(present)), sorted(set(present) - new_ids)
            detail = "; ".join(p for p in (f"add {', '.join(added)}" if added else "",
                                          f"remove {', '.join(dropped)}" if dropped else "") if p)
            if not new_ids:
                _LOGGER.warning("Presence: %s would be left with no tracker for %s; not changed", composite_id, name)
            elif not writable:
                yaml_managed.append(composite_id)
                _LOGGER.info("Presence: %s (%s) would change (%s) but is managed by YAML, not changed",
                             composite_id, name, detail)
            else:
                # Inputs that stay keep their place and their own settings; new ones go on the end.
                kept_cfgs = [cfg for cfg in cfgs if cfg[CONF_ENTITY] in new_ids]
                kept_cfgs += [{CONF_ENTITY: t, CONF_ALL_STATES: False, CONF_USE_PICTURE: False} for t in added]
                plan.composite_writes.append((entry, {**entry.options, CONF_ENTITY_ID: kept_cfgs}))
                plan.actions.append(Action("composite", entry.entry_id, f"{composite_id} for {name}: {detail}"))
                covered = new_ids
        # 3. The person's trackers: no mobile_app tracker once the composite covers it (or it is excluded).
        strip = [t for t in current_trackers if t in mobile and (t in covered or t in exclude)]
        if strip:
            new_trackers = [t for t in current_trackers if t not in strip]
            plan.person_writes.append((pid, new_trackers))
            plan.actions.append(Action("person", pid, f"{name}: stop tracking {', '.join(strip)} directly "
                                                      f"(follows {composite_id})"))
    if yaml_managed:
        plan.issues[ISSUE_PRESENCE_COMPOSITE_YAML] = (ISSUE_PRESENCE_COMPOSITE_YAML,
                                                      {"trackers": ", ".join(sorted(set(yaml_managed)))})
    return plan


class Presence:
    """The running module of one config entry."""

    def __init__(self, hass: HomeAssistant, runtime: PushRuntime) -> None:
        self.hass = hass
        self.runtime = runtime
        self._pending: Callable[[], None] | None = None
        self._issues: set[str] = set()
        self._unsubs: list[Callable[[], None]] = []
        self._started: Callable[[], None] | None = None
        self.last_plan: Plan | None = None

    def start(self) -> None:
        hass = self.hass
        self._unsubs.append(hass.bus.async_listen(er.EVENT_ENTITY_REGISTRY_UPDATED, self._registry_event))
        if (data := hass.data.get(PERSON)) is not None:
            self._unsubs.append(data[1].async_add_listener(self._person_changed))
        if hass.state is CoreState.running:
            self.request(0)
        else:
            self._started = hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STARTED, self._on_started)

    @callback
    def _on_started(self, _event: Event) -> None:
        self._started = None  # a listen_once is gone once it fires; removing it again would log an error
        self.request(0)

    def stop(self) -> None:
        if self._started is not None:
            self._started()
            self._started = None
        for unsub in self._unsubs:
            unsub()
        self._unsubs.clear()
        if self._pending is not None:
            self._pending()
            self._pending = None

    @callback
    def _registry_event(self, event: Event) -> None:
        entity_id = event.data.get("entity_id", "")
        if not entity_id.startswith(f"{TRACKER}."):
            return
        action = event.data.get("action")
        if action == "update" and "disabled_by" not in (event.data.get("changes") or {}):
            return
        self.request()

    async def _person_changed(self, *_: Any) -> None:
        self.request()

    @callback
    def request(self, delay: float = SETTLE_SECONDS) -> None:
        """Run a pass after ``delay`` seconds; a request replaces one that is waiting."""
        if self._pending is not None:
            self._pending()

        async def _run(_now: datetime) -> None:
            self._pending = None
            try:
                await self.async_reconcile()
            except Exception:  # noqa: BLE001 - presence must never take the integration down
                _LOGGER.exception("Presence pass failed")

        self._pending = async_call_later(self.hass, delay, _run)

    async def async_reconcile(self) -> list[Action]:
        """One pass in the entry's current mode. Returns the changes made (or, in a dry run, that would be)."""
        enabled, dry_run, _ = settings(self.runtime.options)
        if not enabled:
            self._set_issues({})
            return []
        plan = await async_plan(self.hass, self.runtime.options)
        self.last_plan = plan
        self._set_issues(plan.issues)
        if dry_run:
            for action in plan.actions:
                _LOGGER.info("Presence (dry run) would change %s", action.description)
            return plan.actions
        for entry, options in plan.composite_writes:
            self.hass.config_entries.async_update_entry(entry, options=options)
        persons = self.hass.data.get(PERSON)
        for pid, trackers in plan.person_writes:
            await persons[1].async_update_item(pid, {"device_trackers": trackers})
        for action in plan.actions:
            _LOGGER.info("Presence changed %s", action.description)
        return plan.actions

    def _set_issues(self, wanted: dict[str, tuple[str, dict[str, str]]]) -> None:
        for issue_id, (key, placeholders) in wanted.items():
            ir.async_create_issue(self.hass, DOMAIN, issue_id, is_fixable=False, severity=ir.IssueSeverity.WARNING,
                                  translation_key=key, translation_placeholders=placeholders)
        for gone in self._issues - set(wanted):
            ir.async_delete_issue(self.hass, DOMAIN, gone)
        self._issues = set(wanted)


@callback
def async_start_presence(hass: HomeAssistant, runtime: PushRuntime) -> Presence:
    """Start watching. The module reads the entry's options on every pass, so the switch needs no reload."""
    presence = Presence(hass, runtime)
    presence.start()
    runtime.presence = presence
    return presence
