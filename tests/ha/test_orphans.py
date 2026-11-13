"""An activity a phone holds a token for that the engine does not show is ended at once (core's token store is faked).

The incident: the engine ended a card before the phone had reported the card's update token, so the end never reached
it. Core stores the token when the phone reports it, which fires no event; ``recovery.watch_reports`` hears it and the
cycle ends what the engine does not show.
"""

from __future__ import annotations

import sys
import time
import types

import pytest
from homeassistant.core import HomeAssistant

from custom_components.fenton_home import recovery
from custom_components.fenton_home.live_jobs import ORPHAN_RETRY_SECONDS

from .test_live_jobs import (  # noqa: F401  (fixtures and helpers, used by name)
    no_startup_hold,
    sent,
    settle,
    start_fan,
    world,
)

IPHONE, IPAD = "hook-stephen-iphone", "hook-stephen-ipad"
TAG = "job.fan.guest"


class Core:
    """Core's ``mobile_app`` token store and the function its webhook handler stores a reported token with."""

    def __init__(self, hass: HomeAssistant) -> None:
        self.hass = hass
        self.tokens: dict[str, dict[str, dict]] = {}
        hass.data["mobile_app"] = {"live_activity_tokens": self.tokens}

    def store(self, hass: HomeAssistant, webhook_id: str, activity_tag: str, token: str, expires_at: float) -> None:
        self.tokens.setdefault(webhook_id, {})[activity_tag] = {"token": token, "expires_at": expires_at}

    def report(self, webhook_id: str, tag: str, token: str = "cd" * 16, expires_in: float = 8 * 3600) -> None:
        """What the app's ``live_activity_token`` webhook does, through the function the integration wraps."""
        sys.modules[recovery.WEBHOOK_MODULE].store_live_activity_token(
            self.hass, webhook_id, tag, token, time.time() + expires_in)


@pytest.fixture
def core(hass: HomeAssistant, monkeypatch) -> Core:
    """Core 2026.7+: set up before ``world`` so the integration finds the store and wraps the webhook's function."""
    fake = Core(hass)
    webhook = types.ModuleType(recovery.WEBHOOK_MODULE)
    webhook.store_live_activity_token = fake.store
    const = types.ModuleType(recovery.CONST_MODULE)
    const.DOMAIN, const.DATA_LIVE_ACTIVITY_TOKENS = "mobile_app", "live_activity_tokens"
    const.ATTR_TOKEN, const.ATTR_LIVE_ACTIVITY_EXPIRES_AT = "token", "expires_at"
    monkeypatch.setitem(sys.modules, recovery.WEBHOOK_MODULE, webhook)
    monkeypatch.setitem(sys.modules, recovery.CONST_MODULE, const)
    monkeypatch.setattr(recovery, "HA_VERSION", "2026.10.1")
    monkeypatch.setattr(recovery, "_warned", set())
    return fake


def ends(world, webhook: str | None = None, since: int = 0) -> list[tuple[str, str]]:
    return [(s["target"], s["data"]["tag"]) for s in sent(world, webhook, since) if s["message"] == "clear_notification"]


async def test_a_token_for_a_card_the_engine_does_not_show_is_ended_at_once(hass: HomeAssistant, core, world) -> None:
    core.report(IPAD, TAG)
    await settle(hass)
    assert ends(world) == [(IPAD, TAG)]


async def test_a_card_the_engine_just_started_is_not_ended_when_its_token_arrives(hass: HomeAssistant, core,
                                                                                  world) -> None:
    start_fan(hass)
    await settle(hass)
    count = len(world.service.sent)
    core.report(IPHONE, TAG)
    core.report(IPAD, TAG)
    await settle(hass)
    assert ends(world, since=count) == []
    assert set(world.live.engine.cards) == {(IPHONE, TAG), (IPAD, TAG)}


async def test_the_end_that_went_out_before_the_token_is_repeated_when_the_token_arrives(hass: HomeAssistant, core,
                                                                                         world, caplog) -> None:
    """The 2026-11-13 incident: the job ended at once, before the app reported the card's update token."""
    start_fan(hass)
    await settle(hass)
    hass.states.async_set("input_boolean.guest_bathroom_fan_timer_pending", "off")
    await settle(hass)
    assert {t for _, t in ends(world)} == {TAG} and world.live.engine.cards == {}
    count = len(world.service.sent)
    core.report(IPHONE, TAG)  # the phone shows the card and reports its token late
    await settle(hass)
    assert ends(world, since=count) == [(IPHONE, TAG)]
    assert "card end tag=job.fan.guest reason=orphan" in caplog.text


async def test_nothing_is_ended_while_home_assistant_starts_and_it_is_after_the_hold(hass: HomeAssistant, core,
                                                                                     world) -> None:
    world.live.engine.hold_until = world.live.now() + 60
    core.report(IPAD, TAG)
    await settle(hass)
    assert ends(world) == []
    world.live.engine.hold_until = 0
    world.live.request()
    await settle(hass)
    assert ends(world) == [(IPAD, TAG)]


async def test_a_card_the_person_dismissed_is_ended_if_its_token_turns_up_again(hass: HomeAssistant, core,
                                                                                world) -> None:
    start_fan(hass)
    await settle(hass)
    assert world.live.engine.dismiss(IPAD, TAG, world.live.now() + 60)
    count = len(world.service.sent)
    core.report(IPAD, TAG)
    await settle(hass)
    assert ends(world, since=count) == [(IPAD, TAG)]
    assert (IPAD, TAG) not in world.live.engine.cards and (IPAD, TAG) in world.live.engine.dismissed


async def test_other_tags_expired_tokens_and_other_apps_are_left_alone(hass: HomeAssistant, core, world) -> None:
    core.report(IPAD, "fenton-home")  # the aggregate Home activity is not a job's
    core.report(IPAD, "job.fan.master", expires_in=-5)  # expired: core would not use it either
    core.report("hook-not-ours", TAG)  # no registration of this app
    await settle(hass)
    assert ends(world) == []
    assert world.live.engine.cards == {}


async def test_a_registration_that_cannot_start_a_card_still_has_its_orphan_ended(hass: HomeAssistant, core,
                                                                                  world) -> None:
    core.report("hook-stephanie-iphone", TAG)
    await settle(hass)
    assert ends(world) == [("hook-stephanie-iphone", TAG)]


async def test_a_token_that_stays_is_not_ended_again_until_the_retry_time(hass: HomeAssistant, core, world) -> None:
    core.report(IPAD, TAG)
    await settle(hass)
    assert len(ends(world)) == 1
    start_fan(hass, "master")  # any cycle
    await settle(hass)
    assert ends(world) == [(IPAD, TAG)], "the token is still stored, but its end was sent a moment ago"
    clock = world.live.now()
    world.live.now = lambda: clock + ORPHAN_RETRY_SECONDS + 1
    world.live.request()
    await settle(hass)
    assert ends(world) == [(IPAD, TAG), (IPAD, TAG)]
    core.report(IPAD, TAG, token="ef" * 16)  # a new activity with a new token is ended at once
    await settle(hass)
    assert len(ends(world)) == 3


async def test_without_core_s_store_nothing_is_ended(hass: HomeAssistant, world) -> None:
    start_fan(hass)
    await settle(hass)
    assert ends(world) == []


async def test_stopping_the_engine_gives_core_its_own_function_back(hass: HomeAssistant, core, world) -> None:
    webhook = sys.modules[recovery.WEBHOOK_MODULE]
    assert webhook.store_live_activity_token is not core.store
    await world.live.async_stop()
    assert webhook.store_live_activity_token == core.store


# -- recovery.py ------------------------------------------------------------------------------------------------------


def test_stored_tokens_are_the_unexpired_ones(hass: HomeAssistant, core) -> None:
    core.store(hass, "hook", "job.a", "t1", time.time() + 100)
    core.store(hass, "hook", "job.b", "t2", time.time() - 100)
    core.store(hass, "gone", "job.c", "t3", time.time() - 100)
    assert recovery.stored_tokens(hass) == {"hook": {"job.a": "t1"}}


def test_stored_tokens_say_none_when_core_cannot_be_read(hass: HomeAssistant, monkeypatch, caplog) -> None:
    monkeypatch.setattr(recovery, "HA_VERSION", "2026.6.4")
    assert recovery.stored_tokens(hass) is None
    assert recovery.watch_reports(lambda *_: None) is None
    monkeypatch.setattr(recovery, "HA_VERSION", "2026.10.1")
    monkeypatch.setattr(recovery, "_warned", set())
    assert recovery.stored_tokens(hass) is None  # nothing under hass.data


def test_a_changed_core_webhook_is_survived(hass: HomeAssistant, core, monkeypatch, caplog) -> None:
    webhook = sys.modules[recovery.WEBHOOK_MODULE]
    webhook.store_live_activity_token = lambda a, b: None  # a different signature
    assert recovery.watch_reports(lambda *_: None) is None
    assert "store_live_activity_token takes" in caplog.text
    monkeypatch.setitem(sys.modules, recovery.WEBHOOK_MODULE, types.ModuleType(recovery.WEBHOOK_MODULE))
    assert recovery.watch_reports(lambda *_: None) is None


def test_a_listener_that_raises_does_not_break_the_webhook(hass: HomeAssistant, core, caplog) -> None:
    def broken(webhook_id: str, tag: str) -> None:
        raise RuntimeError("boom")

    stop = recovery.watch_reports(broken)
    core.report("hook", "job.a")
    assert core.tokens["hook"]["job.a"]["token"] and "token listener failed" in caplog.text
    stop()
    assert sys.modules[recovery.WEBHOOK_MODULE].store_live_activity_token == core.store
