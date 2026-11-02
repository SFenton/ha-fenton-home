"""Device lifecycle (1.1): the unregister endpoint, state purge on removal, the sweep, the executor fix."""

from __future__ import annotations

import logging
from datetime import timedelta
from http import HTTPStatus
from unittest.mock import patch

from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import issue_registry as ir
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.fenton_home.const import (
    CONF_SWEEP_MODE,
    DOMAIN,
    ISSUE_SWEEP_CANDIDATES,
    SWEEP_DRY_RUN,
    SWEEP_OFF,
    SWEEP_ON,
)
from custom_components.fenton_home.engine import Card
from custom_components.fenton_home.sweep import NO_PUSH, SUPERSEDED, async_sweep, find_candidates

import pytest
from pytest_homeassistant_custom_component.common import MockModule, mock_integration

from .test_integration import STEPHANIE, STEPHEN, configured, mobile_app_entry  # noqa: F401

@pytest.fixture(autouse=True)
def mobile_app_stub(hass: HomeAssistant) -> None:
    """Removing a registration calls ``mobile_app``'s own cleanup; the real component needs a full Home Assistant."""
    async def _remove(hass: HomeAssistant, entry: MockConfigEntry) -> None:
        return None

    mock_integration(hass, MockModule("mobile_app", async_remove_entry=_remove))


URL = "/api/fenton_home/devices/unregister"
OLD = timedelta(days=20)


def _age(hass: HomeAssistant, entry: MockConfigEntry, by: timedelta) -> None:
    """Make an entry look ``by`` older (creation and last change)."""
    stamp = dt_util.utcnow() - by
    object.__setattr__(entry, "created_at", stamp)
    object.__setattr__(entry, "modified_at", stamp)


def _ids(hass: HomeAssistant) -> set[str]:
    return {e.data["webhook_id"] for e in hass.config_entries.async_entries("mobile_app")}


# -- endpoint ----------------------------------------------------------------------------------------------------------


async def test_unregister_removes_the_registration(hass, configured, hass_client_no_auth, caplog) -> None:
    caplog.set_level(logging.INFO)
    client = await hass_client_no_auth()
    resp = await client.post(URL, json={"webhook_id": "hook-stephen-ipad"})
    assert resp.status == HTTPStatus.OK and await resp.json() == {"removed": True}
    assert "hook-stephen-ipad" not in _ids(hass) and "hook-stephen-iphone" in _ids(hass)
    assert "hook-stephen-ipad · Fenton" in caplog.text and "hook-stephen-ipad" not in caplog.text.replace(
        "hook-stephen-ipad · Fenton", "").replace("hook-s…", "")
    # Doing it again: already gone counts as done.
    resp = await client.post(URL, json={"webhook_id": "hook-stephen-ipad"})
    assert resp.status == HTTPStatus.GONE and await resp.json() == {"error": "unknown_webhook"}


async def test_unknown_webhook_and_foreign_app_are_410(hass, configured, hass_client_no_auth) -> None:
    client = await hass_client_no_auth()
    assert (await client.post(URL, json={"webhook_id": "nope"})).status == HTTPStatus.GONE
    resp = await client.post(URL, json={"webhook_id": "hook-companion"})
    assert resp.status == HTTPStatus.GONE and "hook-companion" in _ids(hass)


async def test_bad_bodies_are_400(hass, configured, hass_client_no_auth) -> None:
    client = await hass_client_no_auth()
    for payload in ({}, {"webhook_id": ""}, {"webhook_id": 5}, [1], "x", None):
        assert (await client.post(URL, json=payload)).status == HTTPStatus.BAD_REQUEST
    assert (await client.post(URL, data="not json")).status == HTTPStatus.BAD_REQUEST
    assert len(_ids(hass)) == 5


async def test_a_login_must_be_the_registrations_user(hass, configured, hass_client, hass_admin_user) -> None:
    mine = mobile_app_entry(hass, "hook-admin", hass_admin_user.id)
    client = await hass_client()
    resp = await client.post(URL, json={"webhook_id": "hook-stephen-iphone"})
    assert resp.status == HTTPStatus.FORBIDDEN and "hook-stephen-iphone" in _ids(hass)
    resp = await client.post(URL, json={"webhook_id": "hook-admin"})
    assert resp.status == HTTPStatus.OK and hass.config_entries.async_get_entry(mine.entry_id) is None


async def test_unauthenticated_calls_are_rate_limited(hass, configured, hass_client_no_auth) -> None:
    client = await hass_client_no_auth()
    codes = [(await client.post(URL, json={"webhook_id": f"guess-{i}"})).status for i in range(12)]
    assert codes[:10] == [HTTPStatus.GONE] * 10 and codes[10:] == [HTTPStatus.TOO_MANY_REQUESTS] * 2
    # Even the right id is refused while limited: probing can't outrun the cap.
    resp = await client.post(URL, json={"webhook_id": "hook-stephen-ipad"})
    assert resp.status == HTTPStatus.TOO_MANY_REQUESTS and resp.headers["Retry-After"] == "60"
    assert "hook-stephen-ipad" in _ids(hass)


async def test_logged_in_calls_are_not_rate_limited(hass, configured, hass_client) -> None:
    client = await hass_client()
    codes = {(await client.post(URL, json={"webhook_id": f"guess-{i}"})).status for i in range(12)}
    assert codes == {HTTPStatus.GONE}


async def test_unregister_while_not_loaded_is_503(hass, configured, hass_client_no_auth) -> None:
    client = await hass_client_no_auth()
    assert await hass.config_entries.async_unload(configured.entry_id)
    assert (await client.post(URL, json={"webhook_id": "hook-stephen-ipad"})).status == HTTPStatus.SERVICE_UNAVAILABLE


# -- purge -------------------------------------------------------------------------------------------------------------


async def test_removal_drops_our_state_for_that_webhook(hass, configured, hass_client_no_auth) -> None:
    runtime = hass.data[DOMAIN][configured.entry_id]
    for wh in ("hook-stephen-ipad", "hook-stephen-iphone"):
        runtime.counter.sent[wh] += 3
        runtime.counter.errors[wh] += 1
        runtime.timestamps.next(f"{wh}:tag", 100.0)
        runtime.live.engine.cards[(wh, "tag")] = Card(started_at=1.0, last_push=0.0, content={})
        runtime.live.engine.coalescer.sent((wh, "tag"), 5.0)
        runtime.live.engine.dismissed[(wh, "tag")] = False
    runtime.live._no_action.add("hook-stephen-ipad")
    runtime.live._stopped = True   # keep the planning loop (which ends cards of ineligible devices) out of this check
    # An admin removes it (not the endpoint): the signal does the work.
    entry = next(e for e in hass.config_entries.async_entries("mobile_app") if e.data["webhook_id"] == "hook-stephen-ipad")
    await hass.config_entries.async_remove(entry.entry_id)
    await hass.async_block_till_done()
    engine = runtime.live.engine
    assert "hook-stephen-ipad" not in runtime.counter.sent and "hook-stephen-ipad" not in runtime.counter.errors
    assert runtime.timestamps._last.keys() == {"hook-stephen-iphone:tag"}
    assert list(engine.cards) == [("hook-stephen-iphone", "tag")] and list(engine.dismissed) == [("hook-stephen-iphone", "tag")]
    assert engine.coalescer.last(("hook-stephen-ipad", "tag")) is None and engine.coalescer.last(("hook-stephen-iphone", "tag")) == 5.0
    assert "hook-stephen-ipad" not in runtime.live._no_action
    assert runtime.counter.sent["hook-stephen-iphone"] == 3


# -- sweep -------------------------------------------------------------------------------------------------------------


async def _scenario(hass: HomeAssistant, configured) -> dict[str, MockConfigEntry]:
    """Old signed-out S+H, old superseded S+H, a fresh pair, a signed-out companion app."""
    for entry in list(hass.config_entries.async_entries("mobile_app")):
        await hass.config_entries.async_remove(entry.entry_id)
    e = {
        "signed_out_old": mobile_app_entry(hass, "w-out-old", STEPHEN, app_data={}),
        "signed_out_new": mobile_app_entry(hass, "w-out-new", STEPHEN, app_data={}, model="Watch7,1"),
        "old_phone": mobile_app_entry(hass, "w-old-phone", STEPHEN),
        "new_phone": mobile_app_entry(hass, "w-new-phone", STEPHEN),
        "other_model": mobile_app_entry(hass, "w-ipad", STEPHEN, model="iPad16,5"),
        "other_user": mobile_app_entry(hass, "w-other-user", STEPHANIE),
        "companion": mobile_app_entry(hass, "w-companion", STEPHEN, app_id="io.robbie.HomeAssistant", app_data={}),
    }
    _age(hass, e["signed_out_old"], timedelta(days=15))
    _age(hass, e["signed_out_new"], timedelta(days=13))
    _age(hass, e["old_phone"], timedelta(days=8))
    _age(hass, e["new_phone"], timedelta(days=1))
    _age(hass, e["other_model"], timedelta(days=30))
    _age(hass, e["other_user"], timedelta(days=30))
    _age(hass, e["companion"], timedelta(days=90))
    return e


async def test_sweep_rules(hass, configured) -> None:
    e = await _scenario(hass, configured)
    found = {c.entry_id: c.reason for c in find_candidates(hass, "com.sfenton.home")}
    assert found == {e["signed_out_old"].entry_id: NO_PUSH, e["old_phone"].entry_id: SUPERSEDED}
    # The companion app's registration is never a candidate, nor are a lone model, a lone user, or a fresh one.


async def test_superseded_needs_seven_days_and_a_newer_same_user_same_model(hass, configured) -> None:
    e = await _scenario(hass, configured)
    _age(hass, e["old_phone"], timedelta(days=6))
    assert find_candidates(hass, "com.sfenton.home")[0].entry_id == e["signed_out_old"].entry_id
    assert len(find_candidates(hass, "com.sfenton.home")) == 1
    # A newer registration of another user's doesn't supersede it.
    _age(hass, e["old_phone"], timedelta(days=8))
    object.__setattr__(e["new_phone"], "data", {**e["new_phone"].data, "user_id": STEPHANIE})
    ids = {c.entry_id for c in find_candidates(hass, "com.sfenton.home")}
    assert e["old_phone"].entry_id not in ids and e["other_user"].entry_id in ids   # now Stephanie's is the superseded one


async def test_last_update_prefers_entity_states(hass, configured) -> None:
    e = await _scenario(hass, configured)
    registry = er.async_get(hass)
    ent = registry.async_get_or_create("sensor", "mobile_app", "battery-old", config_entry=e["signed_out_old"])
    hass.states.async_set(ent.entity_id, "80")   # updated just now
    assert e["signed_out_old"].entry_id not in {c.entry_id for c in find_candidates(hass, "com.sfenton.home")}
    # An entity with no state falls back to the entry's own change time.
    hass.states.async_remove(ent.entity_id)
    assert e["signed_out_old"].entry_id in {c.entry_id for c in find_candidates(hass, "com.sfenton.home")}


async def test_dry_run_removes_nothing_and_lists_candidates(hass, configured, caplog) -> None:
    caplog.set_level(logging.INFO)
    e = await _scenario(hass, configured)
    runtime = hass.data[DOMAIN][configured.entry_id]
    assert runtime.options.get(CONF_SWEEP_MODE, SWEEP_DRY_RUN) == SWEEP_DRY_RUN   # the default
    before = _ids(hass)
    found = await async_sweep(hass, runtime)
    assert len(found) == 2 and _ids(hass) == before
    assert "would remove" in caplog.text and "w-out-old" in caplog.text
    issue = ir.async_get(hass).async_get_issue(DOMAIN, ISSUE_SWEEP_CANDIDATES)
    assert issue is not None and issue.translation_placeholders["count"] == "2"
    assert "w-out-old" in issue.translation_placeholders["devices"]
    # Once they are gone the issue clears.
    for key in ("signed_out_old", "old_phone"):
        await hass.config_entries.async_remove(e[key].entry_id)
    assert await async_sweep(hass, runtime) == []
    assert ir.async_get(hass).async_get_issue(DOMAIN, ISSUE_SWEEP_CANDIDATES) is None


async def test_on_removes_candidates_only_and_purges(hass, configured, caplog) -> None:
    caplog.set_level(logging.INFO)
    e = await _scenario(hass, configured)
    runtime = hass.data[DOMAIN][configured.entry_id]
    runtime.counter.sent["w-out-old"] = 4
    runtime.options = {CONF_SWEEP_MODE: SWEEP_ON}
    await async_sweep(hass, runtime)
    await hass.async_block_till_done()
    assert _ids(hass) == {"w-out-new", "w-new-phone", "w-ipad", "w-other-user", "w-companion"}
    assert "Sweep removed abandoned registration w-out-old" in caplog.text
    assert "w-out-old" not in runtime.counter.sent
    assert ir.async_get(hass).async_get_issue(DOMAIN, ISSUE_SWEEP_CANDIDATES) is None


async def test_off_does_nothing_and_clears_the_issue(hass, configured) -> None:
    await _scenario(hass, configured)
    runtime = hass.data[DOMAIN][configured.entry_id]
    await async_sweep(hass, runtime)
    assert ir.async_get(hass).async_get_issue(DOMAIN, ISSUE_SWEEP_CANDIDATES) is not None
    runtime.options = {CONF_SWEEP_MODE: SWEEP_OFF}
    before = _ids(hass)
    assert await async_sweep(hass, runtime) == [] and _ids(hass) == before
    assert ir.async_get(hass).async_get_issue(DOMAIN, ISSUE_SWEEP_CANDIDATES) is None


async def test_the_sweep_runs_at_startup_and_daily(hass, configured) -> None:
    from pytest_homeassistant_custom_component.common import async_fire_time_changed  # noqa: PLC0415

    await _scenario(hass, configured)
    runtime = hass.data[DOMAIN][configured.entry_id]
    runtime.options = {CONF_SWEEP_MODE: SWEEP_ON}
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(days=1, minutes=1))
    await hass.async_block_till_done()
    assert "w-out-old" not in _ids(hass)


async def test_a_fresh_load_sweeps_at_startup(hass, configured, caplog) -> None:
    caplog.set_level(logging.INFO)
    await _scenario(hass, configured)
    assert await hass.config_entries.async_reload(configured.entry_id)
    await hass.async_block_till_done()
    assert "would remove abandoned registration w-out-old" in caplog.text   # default mode: dry run
    assert ir.async_get(hass).async_get_issue(DOMAIN, ISSUE_SWEEP_CANDIDATES) is not None


async def test_a_failing_sweep_is_logged_not_raised(hass, configured, caplog) -> None:
    from pytest_homeassistant_custom_component.common import async_fire_time_changed  # noqa: PLC0415

    with patch("custom_components.fenton_home.sweep.find_candidates", side_effect=RuntimeError("boom")):
        async_fire_time_changed(hass, dt_util.utcnow() + timedelta(days=1, minutes=1))
        await hass.async_block_till_done()
    assert "Registration sweep failed" in caplog.text


async def test_options_flow_has_the_sweep_mode(hass, configured) -> None:
    result = await hass.config_entries.options.async_init(configured.entry_id)
    schema = result["data_schema"].schema
    mode = next(v for k, v in schema.items() if str(k) == CONF_SWEEP_MODE)
    assert mode.config["options"] == ["off", "dry_run", "on"]
    result = await hass.config_entries.options.async_configure(result["flow_id"], {CONF_SWEEP_MODE: "on"})
    assert configured.options[CONF_SWEEP_MODE] == "on"
    assert "sweep_mode" not in MockConfigEntry(domain=DOMAIN).options   # unset: the runtime default is dry_run


# -- the executor fix --------------------------------------------------------------------------------------------------


async def test_the_alias_service_file_is_read_in_the_executor(hass: HomeAssistant) -> None:
    from custom_components.fenton_home.legacy import async_register_alias_service  # noqa: PLC0415
    from unittest.mock import AsyncMock  # noqa: PLC0415

    calls: list[tuple] = []
    real = hass.async_add_executor_job

    def spy(func, *args):
        calls.append((func.__name__, args))
        return real(func, *args)

    with patch.object(hass, "async_add_executor_job", spy), \
            patch("custom_components.fenton_home.legacy.service_helper.async_set_service_schema"):
        await async_register_alias_service(hass, AsyncMock(), None)
    assert calls and calls[0][0] == "load_yaml_dict" and calls[0][1][0].endswith("services.yaml")
