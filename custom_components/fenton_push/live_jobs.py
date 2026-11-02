"""The job engine inside Home Assistant: listens to the house, sends the cards, answers the buttons.

``engine.Engine`` decides what every device should show; this module feeds it states, sends what it plans to each device by
webhook id through core's ``mobile_app`` notify service (core keeps owning the Live Activity tokens), runs the control map when a card's
button is pressed and keeps problems tied to their Donetick tasks. Spec: ``live-activities-per-job.md`` sections 2 to 7.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import asdict, fields
from typing import Any

from homeassistant.const import EVENT_HOMEASSISTANT_STARTED, EVENT_STATE_CHANGED
from homeassistant.core import CALLBACK_TYPE, Context, CoreState, Event, HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.event import async_call_later, async_track_state_change_event
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util
from homeassistant.util import slugify

from . import donetick, recovery
from .const import (
    CONF_LIVE_DEVICES,
    CONF_LIVE_JOBS,
    CONF_LIVE_JOBS_ENABLED,
    CONF_PERSON_NOTIFY,
    DOMAIN,
    EVENT_ACTION,
    EVENT_DISMISSED,
    SENSOR_JOBS,
)
from .engine import Card, Engine, Plan, Push, Registration, pick, service_data
from .entities import (
    AWAY_STATUS,
    CONFIRM_TIMEOUT,
    DEFAULT_PERSON_NOTIFY,
    DISHWASHER,
    DONETICK_USERS,
    STARTUP_HOLD_SECONDS,
    FAN_ROOMS,
    LAUNDRY,
    ROBOTS,
    SIDES,
    TODO_ENTITY,
    VACATION_MODE,
    coordinator_entities,
    fan_entities,
    resolve_device,
    style_for,
)
from .jobs import Ctx
from .model import Control, Expect, JobState, PersonRef
from .notify import _device_actions, send_to_device
from .problems import ProblemRecord, build_problem, code_for_raw
from .snapshot import States
from .targets import Device, live_devices, people

_LOGGER = logging.getLogger(__name__)

STORE_VERSION = 1
LOOKUP_INTERVAL = 2.5
#: A card that could not be sent is tried again after this long.
RETRY_SECONDS = 30.0


def watched_entities() -> tuple[set[str], tuple[str, ...]]:
    """The entity ids the adapters read, and the prefixes of the ones named at run time (rooms' ``last_cleaned``)."""
    ids: set[str] = {TODO_ENTITY, AWAY_STATUS, VACATION_MODE, *DISHWASHER.values(), *LAUNDRY.values()}
    for room in FAN_ROOMS.values():
        ids.update(v for k, v in fan_entities(room).items() if k != "command")
    prefixes = []
    for robot in ROBOTS.values():
        ids.update((robot.vacuum, robot.status_flag, robot.status_sensor, robot.segment_sensor, robot.error_sensor,
                    robot.mode_select, robot.fallback_helper, robot.error_message_helper))
        if robot.coordinator:
            ids.update(coordinator_entities(robot).values())
            prefixes.append(f"sensor.{robot.coordinator}_")
    for side in SIDES.values():
        ids.update((side.phase, side.timer, side.restore_at, side.requested_by, side.cancel, side.climate))
    return ids, tuple(prefixes)


class LiveJobs:
    """Runs the job engine for one config entry."""

    def __init__(self, hass: HomeAssistant, runtime: Any) -> None:
        self.hass = hass
        self.runtime = runtime
        self.engine = Engine()
        #: Seconds since the epoch; tests replace it.
        self.now = time.time
        self.confirm_timeout = CONFIRM_TIMEOUT
        self.dismiss_delay = 1.0
        self._store: Store = Store(hass, STORE_VERSION, f"{DOMAIN}.live_jobs")
        self._watch, self._prefixes = watched_entities()
        self._unsubs: list[CALLBACK_TYPE] = []
        self._runner: asyncio.Task | None = None
        self._dirty = False
        self._wake: CALLBACK_TYPE | None = None
        self._stopped = False
        self._ready = hass.state is CoreState.running
        self._no_action: set[str] = set()

    # -- options -----------------------------------------------------------------------------------------------------

    @property
    def _options(self) -> dict[str, Any]:
        return self.runtime.options

    @property
    def enabled(self) -> bool:
        return bool(self._options.get(CONF_LIVE_JOBS_ENABLED, True))

    def _enabled_jobs(self) -> frozenset[str] | None:
        jobs = self._options.get(CONF_LIVE_JOBS)
        return None if jobs is None else frozenset(jobs)

    # -- lifecycle ---------------------------------------------------------------------------------------------------

    async def async_start(self) -> None:
        await self._load()
        self._unsubs.append(self.hass.bus.async_listen(EVENT_STATE_CHANGED, self._on_state, event_filter=self._wanted))
        self._unsubs.append(self.hass.bus.async_listen(EVENT_ACTION, self._on_action))
        self._unsubs.append(self.hass.bus.async_listen(EVENT_DISMISSED, self._on_dismissed))
        try:
            from homeassistant.config_entries import SIGNAL_CONFIG_ENTRY_CHANGED  # noqa: PLC0415

            self._unsubs.append(async_dispatcher_connect(self.hass, SIGNAL_CONFIG_ENTRY_CHANGED, self._on_entry_changed))
        except ImportError:  # pragma: no cover - renamed upstream; tokens are then picked up on the next change
            _LOGGER.debug("No config entry change signal; new registrations show cards on the next state change")
        if self._ready:
            self.engine.hold_until = self.now() + STARTUP_HOLD_SECONDS / 2
        else:
            self._unsubs.append(self.hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STARTED, self._on_started))
        self.request()

    @callback
    def _on_started(self, _event: Event) -> None:
        self._ready = True
        self.engine.hold_until = self.now() + STARTUP_HOLD_SECONDS
        self.request()

    async def async_stop(self) -> None:
        """Stop listening. Cards stay on the phones; they end when the job does or after 8 hours."""
        self._stopped = True
        self.hass.states.async_remove(SENSOR_JOBS)
        for unsub in self._unsubs:
            unsub()
        self._unsubs.clear()
        if self._wake:
            self._wake()
            self._wake = None
        if self._runner:
            self._runner.cancel()
        await self._store.async_save(self._data())

    # -- persistence -------------------------------------------------------------------------------------------------

    def _data(self) -> dict[str, Any]:
        cards = [{"webhook_id": wh, "tag": tag, "started_at": c.started_at, "session_key": c.session_key,
                  "critical_sent": c.critical_sent, "relevance": c.relevance}
                 for (wh, tag), c in self.engine.cards.items()]
        return {"cards": cards, "records": [asdict(r) for r in self.engine.records.values()]}

    def _save(self) -> None:
        self._store.async_delay_save(self._data, 1)

    async def _load(self) -> None:
        stored = await self._store.async_load() or {}
        for raw in stored.get("cards", []):
            try:
                self.engine.cards[(raw["webhook_id"], raw["tag"])] = Card(
                    started_at=float(raw["started_at"]), last_push=0.0, content={}, session_key=raw.get("session_key"),
                    critical_sent=bool(raw.get("critical_sent")), relevance=float(raw.get("relevance", 0)))
            except (KeyError, TypeError, ValueError):
                continue
        known = {f.name for f in fields(ProblemRecord)}
        for raw in stored.get("records", []):
            try:
                record = ProblemRecord(**{k: v for k, v in raw.items() if k in known})
            except TypeError:
                continue
            self.engine.records[record.job_id] = record

    # -- scheduling --------------------------------------------------------------------------------------------------

    @callback
    def _wanted(self, data: Any) -> bool:
        entity_id = data["entity_id"]
        return entity_id in self._watch or entity_id.startswith(self._prefixes)

    @callback
    def _on_state(self, event: Event) -> None:
        self.request()

    @callback
    def _on_entry_changed(self, change: Any, entry: Any) -> None:
        if getattr(entry, "domain", None) == "mobile_app":
            self.request()

    def request(self) -> None:
        """Run a cycle soon (cycles never overlap; a request during one runs another straight after)."""
        if self._stopped:
            return
        self._dirty = True
        if not self._ready:
            return
        if self._runner is None or self._runner.done():
            self._runner = self.hass.async_create_task(self._loop())

    async def run_now(self) -> None:
        """Run a cycle and wait until it has finished."""
        self.request()
        if self._runner:
            await asyncio.shield(self._runner)

    async def _loop(self) -> None:
        try:
            while self._dirty and not self._stopped:
                self._dirty = False
                try:
                    await self._cycle()
                except Exception:  # noqa: BLE001 - one bad cycle must not stop the engine
                    _LOGGER.exception("Live job cycle failed")
        finally:
            self._runner = None

    def _set_wake(self, delay: float | None) -> None:
        if self._wake:
            self._wake()
            self._wake = None
        if delay is not None and not self._stopped:
            self._wake = async_call_later(self.hass, max(delay, 0.05) + 0.05, self._on_wake)

    @callback
    def _on_wake(self, _now: Any) -> None:
        self._wake = None
        self.request()

    # -- inputs ------------------------------------------------------------------------------------------------------

    def _snapshot(self) -> States:
        ids = set(self._watch)
        for prefix in self._prefixes:
            ids.update(e for e in self.hass.states.async_entity_ids("sensor") if e.startswith(prefix))
        ids.update(s.entity_id for s in self.hass.states.async_all("person"))
        return States.from_hass(self.hass, ids)

    def _people(self) -> tuple[PersonRef, ...]:
        return tuple(PersonRef(p.entity_id, p.user_id, p.name) for p in people(self.hass))

    def _ctx(self) -> Ctx:
        return Ctx(tz=dt_util.get_default_time_zone(), people=self._people(), enabled=self._enabled_jobs())

    def _devices(self) -> list[Device]:
        if not self.enabled:
            return []
        only = self._options.get(CONF_LIVE_DEVICES) or None
        return live_devices(self.hass, self.runtime.bundle_id, only)

    def registrations(self) -> list[Registration]:
        by_user = {p.user_id: p.entity_id for p in self._people()}
        return [Registration(d.webhook_id, d.user_id, d.name, by_user.get(d.user_id)) for d in self._devices()]

    # -- the cycle ---------------------------------------------------------------------------------------------------

    async def _cycle(self) -> None:
        now = self.now()
        await self._sync_tasks(now)
        states = self._snapshot()
        regs = self.registrations()
        self.engine.refresh(states, now, self._ctx())
        plan = self.engine.plan(regs, now)
        failed = await self._execute(plan, now)
        await self._clear_pushes()
        self._publish()
        self._save()
        wake = plan.wake_in
        if failed:
            wake = RETRY_SECONDS if wake is None else min(wake, RETRY_SECONDS)
        if any(r.awaiting_task(now) for r in self.engine.records.values()):
            wake = LOOKUP_INTERVAL if wake is None else min(wake, LOOKUP_INTERVAL)
        self._set_wake(wake)

    async def _sync_tasks(self, now: float) -> None:
        """Find each problem's Donetick task, and notice when a linked one has been completed."""
        records = [r for r in self.engine.records.values() if r.task_id is not None
                   or (r.awaiting_task(now) and now - r.last_lookup >= LOOKUP_INTERVAL)]
        if not records:
            return
        items = await donetick.open_tasks(self.hass)
        if items is None:
            return  # the todo list isn't there (yet); that is not a lookup
        for record in records:
            if record.task_id is None:
                record.lookups += 1
                record.last_lookup = now
            open_ids = {tid for i in items if (tid := donetick.task_id_from_uid(i.get("uid"))) is not None}
            if record.task_id is None:
                matches = [donetick.task_id_from_uid(i.get("uid")) for i in items if i.get("summary") == record.task_title]
                matches = [m for m in matches if m is not None]
                if matches:
                    record.task_id = max(matches)
            elif record.task_id not in open_ids:
                record.task_completed = True

    async def _execute(self, plan: Plan, now: float) -> bool:
        """Send what the plan asks for. Returns whether any push could not be sent."""
        failed = False
        if not plan.actions:
            return failed
        actions = _device_actions(self.hass)
        devices = {d.webhook_id: d for d in live_devices(self.hass, self.runtime.bundle_id)}
        for action in plan.actions:
            device = devices.get(action.webhook_id)
            try:
                sent = device is not None and await send_to_device(
                    self.hass, device, action.webhook_id, service_data(action), actions)
            except HomeAssistantError as err:
                _LOGGER.warning("Couldn't send %s to %s: %s", action.tag, action.webhook_id, err)
                if isinstance(action, Push):
                    self.engine.push_failed(action.webhook_id, action.tag, now)
                    failed = True
                continue
            if not sent:
                if action.webhook_id not in self._no_action:
                    self._no_action.add(action.webhook_id)
                    _LOGGER.warning("No notify action for %s; its %s card isn't sent", action.webhook_id, action.tag)
                if isinstance(action, Push):
                    self.engine.push_failed(action.webhook_id, action.tag, now)
                    failed = True
        return failed

    async def _clear_pushes(self) -> None:
        """A problem ended: clear the fallback push (same tag) from the phones that got one."""
        for record in self.engine.take_ended_records():
            if not (record.push_tag and record.pushed_to):
                continue
            by_entity = {p.entity_id: p for p in self._people()}
            for entity_id in record.pushed_to:
                if person := by_entity.get(entity_id):
                    await self._notify_person(person, "clear_notification", None, {"tag": record.push_tag})

    def _publish(self) -> None:
        self.hass.states.async_set(SENSOR_JOBS, str(len(self.engine.cards)), {
            "friendly_name": "Fenton live jobs", "icon": "mdi:cards-outline", "tags": self.engine.describe(),
            "devices": sorted({wh for (wh, _) in self.engine.cards})})

    # -- people ------------------------------------------------------------------------------------------------------

    async def _notify_person(self, person: PersonRef, message: str, title: str | None, data: dict[str, Any]) -> bool:
        template = self._options.get(CONF_PERSON_NOTIFY) or DEFAULT_PERSON_NOTIFY
        service = slugify(template.replace("*", slugify(person.first)))
        if not self.hass.services.has_service("notify", service):
            _LOGGER.warning("No notify.%s for %s; the problem notification isn't sent", service, person.name)
            return False
        payload: dict[str, Any] = {"message": message, "data": data}
        if title:
            payload["title"] = title
        try:
            await self.hass.services.async_call("notify", service, payload, blocking=True)
        except HomeAssistantError as err:
            _LOGGER.warning("Couldn't notify %s: %s", person.name, err)
            return False
        return True

    # -- buttons -----------------------------------------------------------------------------------------------------

    @callback
    def _on_action(self, event: Event) -> None:
        self.hass.async_create_task(self._handle_action(event))

    async def _handle_action(self, event: Event) -> None:
        """A card's button (spec section 7, engine side)."""
        tag, control_id = str(event.data.get("tag") or ""), str(event.data.get("control") or "")
        user_id = event.context.user_id
        if not self.enabled or not tag or not control_id:
            return
        if not user_id:
            _LOGGER.warning("Ignoring %s %s: the event has no user", tag, control_id)
            return
        regs = [r for r in self.registrations() if r.user_id == user_id]
        if not regs:
            _LOGGER.warning("Ignoring %s %s: user %s has no card-capable registration", tag, control_id, user_id)
            return
        now = self.now()
        self.engine.refresh(self._snapshot(), now, self._ctx())
        job = pick(self.engine.current, regs[0].person).get(tag)
        control = job.control(control_id) if job else None
        if control is None:
            _LOGGER.info("%s no longer offers %s", tag, control_id)
            self.engine.set_notice(tag, user_id, "No longer available", self.now())
            self.engine.mark_urgent(tag)
            await self.run_now()
            return
        # The state change the press causes is the confirmation: its push goes at priority 10.
        self.engine.mark_urgent(tag)
        if not await self._press(control, job, user_id):
            self.engine.set_notice(tag, user_id, f"Couldn't {control.verb or control.title.lower()}. Try again.",
                                   self.now())
        await self.run_now()

    async def _press(self, control: Control, job: JobState, user_id: str) -> bool:
        context = Context(user_id=user_id)
        try:
            if control.confirm == "task":
                return await self._complete_task(control, job, user_id, context)
            for call in control.calls:
                await self.hass.services.async_call(call.domain, call.service, dict(call.data), blocking=True,
                                                    context=context)
        except HomeAssistantError as err:
            _LOGGER.warning("%s on %s failed: %s", control.id, job.tag, err)
            return False
        return await self.wait_for(control.expect, self.confirm_timeout)

    async def _complete_task(self, control: Control, job: JobState, user_id: str, context: Context) -> bool:
        task_id = control.calls[0].data["task_id"]
        try:
            await donetick.complete_task(self.hass, task_id, DONETICK_USERS.get(user_id), context)
        except HomeAssistantError as err:
            # Already completed is a no-op upstream; carry on and look at the list.
            _LOGGER.info("donetick.complete_task %s: %s", task_id, err)
        deadline = time.monotonic() + self.confirm_timeout
        while True:
            open_ids = await donetick.open_task_ids(self.hass)
            if open_ids is not None and task_id not in open_ids:
                if record := self.engine.records.get(job.job_id):
                    record.task_completed = True
                return True
            if time.monotonic() >= deadline:
                return False
            await asyncio.sleep(min(0.5, max(deadline - time.monotonic(), 0.01)))

    async def wait_for(self, expects: tuple[Expect, ...], timeout: float) -> bool:
        """Wait up to ``timeout`` seconds for every expected state."""
        if not expects:
            return True

        def met() -> bool:
            return all(e.met((s := self.hass.states.get(e.entity_id)) and s.state) for e in expects)

        if met():
            return True
        done: asyncio.Future[bool] = self.hass.loop.create_future()

        @callback
        def _changed(_event: Event) -> None:
            if not done.done() and met():
                done.set_result(True)

        unsub = async_track_state_change_event(self.hass, [e.entity_id for e in expects], _changed)
        try:
            return await asyncio.wait_for(done, timeout)
        except TimeoutError:
            return met()
        finally:
            unsub()

    # -- dismissals --------------------------------------------------------------------------------------------------

    @callback
    def _on_dismissed(self, event: Event) -> None:
        self.hass.async_create_task(self._handle_dismissed(event))

    async def _handle_dismissed(self, event: Event) -> None:
        """The person swiped a card away: leave it away until the job ends (spec section 6)."""
        tag = str(event.data.get("tag") or "")
        user_id = event.context.user_id
        if not tag or not user_id:
            return
        mine = [r.webhook_id for r in self.registrations() if r.user_id == user_id
                and (r.webhook_id, tag) in self.engine.cards]
        if len(mine) > 1:
            # The event does not say which device. Core drops the token of the device that reported the dismissal.
            await asyncio.sleep(self.dismiss_delay)
            gone = [wh for wh in mine if recovery.has_token(self.hass, wh, tag) is False]
            mine = gone or mine
        now = self.now()
        if any([self.engine.dismiss(wh, tag, now) for wh in mine]):
            self._publish()

    # -- fenton_push.problem -----------------------------------------------------------------------------------------

    async def async_problem(self, data: dict[str, Any]) -> None:
        """``fenton_push.problem``: show the card to people with S+H, push everyone else (spec section 5)."""
        job_id = resolve_device(str(data.get("device") or ""))
        if job_id is None:
            raise ServiceValidationError(f"Unknown device {data.get('device')!r}")
        raw = (data.get("raw") or "").strip() or None
        code = (data.get("code") or "").strip().lower() or code_for_raw(raw)
        if not code:
            raise ServiceValidationError("Give the problem a code or the robot's raw error text")
        now = self.now()
        title, message = data.get("title"), data.get("message")
        record = self.engine.raise_problem(
            job_id, code, raw, now, task_title=(data.get("task_title") or None), url=data.get("url") or None,
            push_tag=data.get("tag") or None, title=title, message=message)
        if not title or not message:
            info = build_problem(code, raw, room=record.room, since=None, tz=None, make_button=lambda _: None,
                                 title=title, message=message)
            title = title or f"{style_for(job_id).title}: {info.problem}"
            message = message or info.message
        carded = {r.person for r in self.registrations() if r.person}
        push_data = dict(data.get("data") or {})
        if record.push_tag:
            push_data.setdefault("tag", record.push_tag)
        if record.url:
            push_data.setdefault("url", record.url)
            push_data.setdefault("clickAction", record.url)
        for person in self._people():
            if person.entity_id in carded:
                continue
            if await self._notify_person(person, message, title, push_data) and person.entity_id not in record.pushed_to:
                record.pushed_to.append(person.entity_id)
        await self.run_now()
