"""Config flow, the push endpoint and person targets inside a real Home Assistant, with Apple patched out."""

from __future__ import annotations

from http import HTTPStatus
from unittest.mock import AsyncMock, patch

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
from homeassistant import config_entries
from homeassistant.components.notify import BaseNotificationService
from homeassistant.components.notify.legacy import NOTIFY_SERVICES
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.exceptions import HomeAssistantError
from homeassistant.setup import async_setup_component
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.fenton_home.apns import APNsError
from custom_components.fenton_home.entities import JOB_IDS
from custom_components.fenton_home.const import (
    CONF_BUNDLE_ID,
    CONF_CRITICAL_ALERTS,
    CONF_KEY,
    CONF_KEY_ID,
    CONF_PUSH_BASE_URL,
    CONF_TEAM_ID,
    DOMAIN,
)

from .. import samples

CLIENT = "custom_components.fenton_home.apns.APNsClient"
PEM = ec.generate_private_key(ec.SECP256R1()).private_bytes(
    serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()).decode()
DATA = {CONF_KEY: PEM, CONF_KEY_ID: "ABC123DEFG", CONF_TEAM_ID: "TEAM123456", CONF_BUNDLE_ID: "com.sfenton.home"}
STEPHEN, STEPHANIE = "user-stephen", "user-stephanie"


async def test_flow_checks_the_key_with_apple(hass: HomeAssistant) -> None:
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
    assert result["type"] is FlowResultType.FORM
    form = {CONF_KEY: "nope", CONF_KEY_ID: "abc123defg", CONF_TEAM_ID: "team123456", CONF_BUNDLE_ID: "com.sfenton.home"}
    result = await hass.config_entries.flow.async_configure(result["flow_id"], form)
    assert result["errors"] == {"base": "invalid_key"}
    form[CONF_KEY] = PEM
    for reason, status, error in (("InvalidProviderToken", 403, "key_rejected"),
                                  ("TopicDisallowed", 400, "bundle_rejected"),
                                  ("ConnectError", 0, "cannot_connect")):
        with patch(f"{CLIENT}.verify", AsyncMock(side_effect=APNsError(status, reason))):
            result = await hass.config_entries.flow.async_configure(result["flow_id"], form)
        assert result["errors"] == {"base": error}
    # A PEM pasted into the one-line field loses its line breaks; it is rebuilt.
    flattened = " ".join(PEM.split("\n"))
    with patch(f"{CLIENT}.verify", AsyncMock(return_value=None)), \
         patch("custom_components.fenton_home.async_setup_entry", AsyncMock(return_value=True)):
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {**form, CONF_KEY: flattened})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "com.sfenton.home (key ABC123DEFG)"
    assert result["data"] == {**DATA, CONF_KEY: PEM.strip()}


PUSH_URL = "http://127.0.0.1:8123/api/fenton_home/apns"


class MobileAppStandIn(BaseNotificationService):
    """``mobile_app``'s notify service, which can't load here (it pulls in cloud, assist and their libraries).

    Same targets (device name to webhook id, for devices that can push) and the same refusal of a websocket-only
    device that isn't connected; Home Assistant's own legacy notify code registers its actions, so they're named
    exactly as on a real install: ``notify.mobile_app_<device name>`` and no ``notify.mobile_app``.
    """

    def __init__(self) -> None:
        self.sent: list[dict] = []
        self.unregistered: set[str] = set()  # webhook ids the real service would KeyError on (not in its entries)

    def _registrations(self) -> dict[str, dict]:
        return {e.data["webhook_id"]: dict(e.data) for e in self.hass.config_entries.async_entries("mobile_app")}

    @property
    def targets(self) -> dict[str, str]:
        return {data["device_name"]: webhook_id for webhook_id, data in self._registrations().items()
                if "push_url" in data["app_data"] or data["app_data"].get("push_websocket_channel")}

    async def async_send_message(self, message: str = "", **kwargs) -> None:
        for target in kwargs.get("target") or self.targets.values():
            if target in self.unregistered:
                raise KeyError(target)
            if "push_url" not in self._registrations()[target]["app_data"]:
                raise HomeAssistantError("Device not connected to local push notifications")
            self.sent.append({"target": target, "message": message,
                              **{k: kwargs[k] for k in ("title", "data") if kwargs.get(k) is not None}})


async def mobile_app_actions(hass: HomeAssistant) -> MobileAppStandIn:
    """Register ``mobile_app``'s notify actions the way Home Assistant does for a discovered platform."""
    assert await async_setup_component(hass, "notify", {})
    service = MobileAppStandIn()
    await service.async_setup(hass, "notify", "mobile_app")
    await service.async_register_services()
    hass.data[NOTIFY_SERVICES].setdefault("mobile_app", []).append(service)
    return service


def mobile_app_entry(hass: HomeAssistant, webhook_id: str, user_id: str, *, model: str = "iPhone19,2",
                     app_id: str = "com.sfenton.home", app_data: dict | None = None) -> MockConfigEntry:
    """A mobile_app registration as Home Assistant stores it (mobile_app itself isn't loaded)."""
    if app_data is None:
        app_data = {"push_url": PUSH_URL, "push_token": f"tok-{webhook_id}", "apns_environment": "sandbox"}
    entry = MockConfigEntry(domain="mobile_app", unique_id=webhook_id, data={
        "webhook_id": webhook_id, "user_id": user_id, "app_id": app_id, "app_name": "Fenton Home",
        "app_version": "0.1.0 (1)", "device_id": f"device-{webhook_id}", "device_name": f"{webhook_id} · Fenton",
        "manufacturer": "Apple", "model": model, "os_name": "iOS", "os_version": "27.0", "supports_encryption": False,
        "secret": None, "app_data": app_data,
    })
    entry.add_to_hass(hass)
    return entry


@pytest.fixture
async def configured(hass: HomeAssistant):
    assert await async_setup_component(hass, "http", {})
    hass.states.async_set("person.stephen_fenton", "home", {"user_id": STEPHEN, "friendly_name": "Stephen Fenton"})
    hass.states.async_set("person.stephanie_hobart", "home",
                          {"user_id": STEPHANIE, "friendly_name": "Stephanie Hobart"})
    hass.states.async_set("person.guest", "home", {"friendly_name": "Guest"})
    mobile_app_entry(hass, "hook-stephen-iphone", STEPHEN)
    mobile_app_entry(hass, "hook-stephen-ipad", STEPHEN, model="iPad16,5")
    mobile_app_entry(hass, "hook-stephanie-iphone", STEPHANIE)
    mobile_app_entry(hass, "hook-stephen-signed-out", STEPHEN, app_data={})
    mobile_app_entry(hass, "hook-companion", STEPHEN, app_id="io.robbie.HomeAssistant")
    await mobile_app_actions(hass)
    entry = MockConfigEntry(domain=DOMAIN, data=DATA, unique_id="com.sfenton.home")
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


def body(webhook_id: str = "hook-stephen-iphone", token: str | None = None, sample: dict | None = None) -> dict:
    sample = dict(sample or samples.FRONT_DOOR_UNSECURED)
    sample["push_token"] = token if token is not None else f"tok-{webhook_id}"
    sample["registration_info"] = {**samples.REGISTRATION_INFO, "webhook_id": webhook_id}
    return sample


async def test_config_endpoint(hass: HomeAssistant, configured, hass_client) -> None:
    client = await hass_client()
    answer = await (await client.get("/api/fenton_home/config")).json()
    assert answer["available"] is True and answer["bundle_id"] == "com.sfenton.home"
    assert answer["push_url"].endswith("/api/fenton_home/apns") and answer["critical_alerts"] is False
    hass.config_entries.async_update_entry(configured, options={CONF_PUSH_BASE_URL: "http://10.0.0.5:8123/",
                                                                CONF_CRITICAL_ALERTS: True})
    await hass.async_block_till_done()
    answer = await (await client.get("/api/fenton_home/config")).json()
    assert answer["push_url"] == "http://10.0.0.5:8123/api/fenton_home/apns" and answer["critical_alerts"] is True


async def test_push_forwards_to_apple(hass: HomeAssistant, configured, hass_client_no_auth) -> None:
    client = await hass_client_no_auth()
    with patch(f"{CLIENT}.send", AsyncMock(return_value=None)) as send:
        response = await client.post("/api/fenton_home/apns", json=body())
    assert response.status == HTTPStatus.CREATED
    limits = (await response.json())["rateLimits"]
    assert limits["successful"] == 1 and limits["maximum"] == 1000
    token, request, environment = send.await_args.args
    assert token == "tok-hook-stephen-iphone" and environment == "sandbox"
    assert request.payload["fenton"]["tag"] == "sfenton-front-door-unsecured-42"


async def test_live_activity_goes_to_the_activity_token_and_a_tokenless_update_is_dropped(
        hass: HomeAssistant, configured, hass_client_no_auth) -> None:
    client = await hass_client_no_auth()
    with patch(f"{CLIENT}.send", AsyncMock(return_value=None)) as send:
        response = await client.post("/api/fenton_home/apns", json=body(sample=samples.LIVE_START))
        assert response.status == HTTPStatus.CREATED
        token, request, _ = send.await_args.args
        assert token == "b" * 64, "a Live Activity goes to the token mobile_app resolved, not the device token"
        assert request.push_type == "liveactivity" and request.payload["aps"]["event"] == "start"

        # mobile_app found neither an activity token nor a push-to-start token: it sends the notification as an
        # ordinary one, and Fenton Push must not show it as a banner.
        tokenless = {k: v for k, v in samples.LIVE_UPDATE.items() if k != "live_activity_token"}
        send.reset_mock()
        dropped = await client.post("/api/fenton_home/apns", json=body(sample=tokenless))
        assert dropped.status == HTTPStatus.CREATED
        send.assert_not_awaited()
        assert (await dropped.json())["rateLimits"]["successful"] == 1, "a drop is not a push"


async def test_push_refuses_strangers(hass: HomeAssistant, configured, hass_client_no_auth) -> None:
    client = await hass_client_no_auth()
    with patch(f"{CLIENT}.send", AsyncMock(return_value=None)) as send:
        for refused in (body(token="wrong"), body("hook-nobody"), body("hook-companion"),
                        body("hook-stephen-signed-out", token="tok-hook-stephen-signed-out")):
            assert (await client.post("/api/fenton_home/apns", json=refused)).status == HTTPStatus.FORBIDDEN
        assert (await client.post("/api/fenton_home/apns", data=b"nope")).status == HTTPStatus.BAD_REQUEST
        assert (await client.post("/api/fenton_home/apns", json=[1])).status == HTTPStatus.BAD_REQUEST
    send.assert_not_awaited()


async def test_push_reports_apple_failures(hass: HomeAssistant, configured, hass_client_no_auth) -> None:
    client = await hass_client_no_auth()
    with patch(f"{CLIENT}.send", AsyncMock(side_effect=APNsError(410, "Unregistered"))):
        assert (await client.post("/api/fenton_home/apns", json=body())).status == HTTPStatus.GONE
    with patch(f"{CLIENT}.send", AsyncMock(side_effect=APNsError(403, "InvalidProviderToken"))):
        assert (await client.post("/api/fenton_home/apns", json=body())).status == HTTPStatus.BAD_GATEWAY
    # A token from the other environment is retried there once.
    with patch(f"{CLIENT}.send", AsyncMock(side_effect=[APNsError(400, "BadDeviceToken"), None])) as send:
        assert (await client.post("/api/fenton_home/apns", json=body())).status == HTTPStatus.CREATED
    assert [call.args[2] for call in send.await_args_list] == ["sandbox", "production"]
    bad = body()
    bad["message"] = "y" * 5000
    assert (await client.post("/api/fenton_home/apns", json=bad)).status == HTTPStatus.BAD_REQUEST


async def test_daily_limit(hass: HomeAssistant, configured, hass_client_no_auth) -> None:
    hass.data[DOMAIN][configured.entry_id].counter.limit = 1
    client = await hass_client_no_auth()
    with patch(f"{CLIENT}.send", AsyncMock(return_value=None)):
        assert (await client.post("/api/fenton_home/apns", json=body())).status == HTTPStatus.CREATED
        limited = await client.post("/api/fenton_home/apns", json=body())
    assert limited.status == HTTPStatus.TOO_MANY_REQUESTS
    assert "rateLimits" in await limited.json()


def mobile_app(hass: HomeAssistant) -> MobileAppStandIn:
    return hass.data[NOTIFY_SERVICES]["mobile_app"][0]


def reached(service: MobileAppStandIn, since: int = 0) -> list[str]:
    return [sent["target"] for sent in service.sent[since:]]


async def test_person_targets(hass: HomeAssistant, configured) -> None:
    service = mobile_app(hass)
    for name in ("fenton", "fenton_stephen", "fenton_stephanie", "fenton_everyone"):
        assert hass.services.has_service("notify", name), name
    # A person without a user can't own a device, so has no target.
    assert not hass.services.has_service("notify", "fenton_guest")
    # Only per-device actions: there is no notify.mobile_app to call.
    assert not hass.services.has_service("notify", "mobile_app")
    assert hass.services.has_service("notify", "mobile_app_hook_stephen_iphone_fenton")

    await hass.services.async_call("notify", "fenton_stephen", {"message": "Hi", "title": "T", "data": {"tag": "x"}},
                                   blocking=True)
    assert reached(service) == ["hook-stephen-iphone", "hook-stephen-ipad"]
    assert service.sent[-1] == {"target": "hook-stephen-ipad", "message": "Hi", "title": "T", "data": {"tag": "x"}}

    count = len(service.sent)
    await hass.services.async_call("notify", "fenton_stephen",
                                   {"message": "Phones", "data": {"fenton": {"devices": ["phone"]}}}, blocking=True)
    assert reached(service, count) == ["hook-stephen-iphone"]

    count = len(service.sent)
    await hass.services.async_call("notify", "fenton", {"message": "All"}, blocking=True)
    assert sorted(reached(service, count)) == ["hook-stephanie-iphone", "hook-stephen-ipad", "hook-stephen-iphone"]

    count = len(service.sent)
    await hass.services.async_call("notify", "fenton_stephanie",
                                   {"message": "Tablets", "data": {"fenton": {"devices": "tablet"}}}, blocking=True)
    assert len(service.sent) == count  # Stephanie has no tablet: nothing sent, no error.


async def test_unreachable_device_doesnt_block_the_rest(hass: HomeAssistant, configured, caplog) -> None:
    """mobile_app raises for a websocket-only device that isn't connected; the person's other devices still get it."""
    mobile_app_entry(hass, "hook-stephen-websocket", STEPHEN, app_data={"push_websocket_channel": True})
    service = mobile_app(hass)
    await service.async_register_services()
    await hass.services.async_call("notify", "fenton_stephen", {"message": "Hi"}, blocking=True)
    assert reached(service) == ["hook-stephen-iphone", "hook-stephen-ipad"]
    assert "Couldn't notify hook-stephen-websocket · Fenton: Device not connected" in caplog.text


def share_name(hass: HomeAssistant, webhook_id: str, name: str) -> None:
    entry = next(e for e in hass.config_entries.async_entries("mobile_app") if e.data["webhook_id"] == webhook_id)
    hass.config_entries.async_update_entry(entry, data={**entry.data, "device_name": name})


async def test_devices_sharing_a_name_all_get_it(hass: HomeAssistant, configured, caplog) -> None:
    """mobile_app keeps one action per name; going by webhook id reaches every device regardless."""
    mobile_app_entry(hass, "hook-stephen-old-iphone", STEPHEN)
    mobile_app_entry(hass, "hook-stephen-older-iphone", STEPHEN)
    for webhook in ("hook-stephen-old-iphone", "hook-stephen-older-iphone"):
        share_name(hass, webhook, "hook-stephen-iphone · Fenton")
    service = mobile_app(hass)
    await service.async_register_services()
    await hass.services.async_call("notify", "fenton_stephen", {"message": "Hi", "title": "T", "data": {"tag": "x"}},
                                   blocking=True)
    assert sorted(reached(service)) == ["hook-stephen-ipad", "hook-stephen-iphone", "hook-stephen-old-iphone",
                                        "hook-stephen-older-iphone"]
    assert service.sent[0]["title"] == "T" and service.sent[0]["data"] == {"tag": "x"}
    assert "Couldn't notify" not in caplog.text


async def test_a_device_mobile_app_doesnt_know_is_skipped(hass: HomeAssistant, configured, caplog) -> None:
    mobile_app(hass).unregistered.add("hook-stephen-iphone")
    await hass.services.async_call("notify", "fenton_stephen", {"message": "Hi"}, blocking=True)
    assert reached(mobile_app(hass)) == ["hook-stephen-ipad"]
    assert "mobile_app has no registration for webhook" not in [r.message for r in caplog.records
                                                                if r.levelname == "WARNING"]
    assert "Couldn't notify hook-stephen-iphone · Fenton" in caplog.text


async def test_without_the_service_object_the_name_action_is_the_fallback(hass: HomeAssistant, configured, monkeypatch,
                                                                         caplog) -> None:
    monkeypatch.setattr("custom_components.fenton_home.notify.NOTIFY_SERVICES", None)
    await hass.services.async_call("notify", "fenton_stephen", {"message": "Hi", "title": "T"}, blocking=True)
    service = mobile_app(hass)
    assert reached(service) == ["hook-stephen-iphone", "hook-stephen-ipad"]
    assert "Couldn't notify" not in caplog.text


async def test_the_fallback_logs_a_device_with_no_action(hass: HomeAssistant, configured, monkeypatch, caplog) -> None:
    monkeypatch.setattr("custom_components.fenton_home.notify.NOTIFY_SERVICES", None)
    mobile_app_entry(hass, "hook-stephen-unnamed", STEPHEN)  # its action isn't registered yet
    await hass.services.async_call("notify", "fenton_stephen", {"message": "Hi"}, blocking=True)
    assert reached(mobile_app(hass)) == ["hook-stephen-iphone", "hook-stephen-ipad"]
    assert "Couldn't notify hook-stephen-unnamed · Fenton: Home Assistant has no way to reach it" in caplog.text


async def test_unload(hass: HomeAssistant, configured, hass_client_no_auth) -> None:
    assert await hass.config_entries.async_unload(configured.entry_id)
    await hass.async_block_till_done()
    assert not hass.services.has_service("notify", "fenton_stephen")
    client = await hass_client_no_auth()
    assert (await client.post("/api/fenton_home/apns", json=body())).status == HTTPStatus.SERVICE_UNAVAILABLE


async def test_options_flow(hass: HomeAssistant, configured) -> None:
    result = await hass.config_entries.options.async_init(configured.entry_id)
    assert result["type"] is FlowResultType.FORM
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {CONF_CRITICAL_ALERTS: True, CONF_PUSH_BASE_URL: " http://127.0.0.1:8123 "})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert configured.options == {
        CONF_CRITICAL_ALERTS: True, CONF_PUSH_BASE_URL: "http://127.0.0.1:8123", "live_jobs_enabled": True,
        "live_jobs": list(JOB_IDS), "live_devices": [], "person_notify": "*_s_phone", "sweep_mode": "dry_run",
        "presence_enabled": False, "presence_dry_run": True,
        "presence_exclude": ["device_tracker.iphone_18_pro", "device_tracker.iphone_17_pro", "device_tracker.ipad"]}
    assert hass.data[DOMAIN][configured.entry_id].critical_alerts is True


async def test_options_flow_offers_the_jobs_and_the_devices_with_cards(hass: HomeAssistant, configured) -> None:
    mobile_app_entry(hass, "hook-stephen-s-plus-h", STEPHEN,
                     app_data={"push_url": PUSH_URL, "push_token": "t", "start_live_activity_token": "ab" * 32})
    result = await hass.config_entries.options.async_init(configured.entry_id)
    schema = result["data_schema"].schema
    devices = next(v for k, v in schema.items() if str(k) == "live_devices")
    assert [o["value"] for o in devices.config["options"]] == ["hook-stephen-s-plus-h"]
    jobs = next(v for k, v in schema.items() if str(k) == "live_jobs")
    assert [o["value"] for o in jobs.config["options"]] == list(JOB_IDS)
    result = await hass.config_entries.options.async_configure(result["flow_id"], {
        "live_jobs": ["fan.guest"], "live_devices": ["hook-stephen-s-plus-h"], "live_jobs_enabled": False,
        "person_notify": " *_phone "})
    assert configured.options["live_jobs"] == ["fan.guest"] and configured.options["live_devices"] == ["hook-stephen-s-plus-h"]
    assert configured.options["live_jobs_enabled"] is False and configured.options["person_notify"] == "*_phone"


async def test_unreadable_saved_key(hass: HomeAssistant) -> None:
    entry = MockConfigEntry(domain=DOMAIN, data={**DATA, CONF_KEY: "garbage"}, unique_id="com.sfenton.home")
    entry.add_to_hass(hass)
    assert not await hass.config_entries.async_setup(entry.entry_id)
    assert entry.state is config_entries.ConfigEntryState.SETUP_ERROR
