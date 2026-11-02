"""HTTP endpoints.

The pre-1.0 paths ``/api/fenton_push/config`` and ``/api/fenton_push/apns`` are aliases on the same handlers.

- ``GET /api/fenton_home/config`` (Home Assistant access token): tells the app which ``push_url`` to register.
- ``POST /api/fenton_home/devices/unregister`` (see ``UnregisterView``): the app removes its own registration.
- ``POST /api/fenton_home/apns`` (no token): the ``push_url`` itself. ``mobile_app`` posts here without
  credentials, so a request is only honoured when its webhook id and push token match a real Fenton Home
  registration; both are secrets Home Assistant and the app share.

Answers follow what ``mobile_app`` expects from a push server: 201 with ``rateLimits`` when sent, 429 once a
device's daily allowance is spent, otherwise an error status with a ``message`` that Home Assistant logs.
"""

from __future__ import annotations

import copy
import hmac
import logging
import time
from collections import defaultdict
from datetime import UTC, datetime, timedelta
from http import HTTPStatus
from typing import TYPE_CHECKING, Any

from aiohttp import web
from homeassistant.components.http import KEY_HASS, KEY_HASS_USER, HomeAssistantView
from homeassistant.core import HomeAssistant
from homeassistant.helpers.network import NoURLAvailableError, get_url

from . import recovery
from .devices import short
from .apns import ENVIRONMENTS, APNsError
from .const import (
    API_CONFIG,
    API_PUSH,
    API_UNREGISTER,
    APP_DATA_ENVIRONMENT,
    CONF_PUSH_BASE_URL,
    DAILY_LIMIT,
    DOMAIN,
    LEGACY_API_CONFIG,
    LEGACY_API_PUSH,
    MOBILE_APP,
    UNREGISTER_PER_MINUTE,
)
from .payload import PayloadError, build
from .targets import registration

if TYPE_CHECKING:
    from . import PushRuntime

_LOGGER = logging.getLogger(__name__)
_REGISTERED = f"{DOMAIN}_views"


def register_views(hass: HomeAssistant) -> None:
    """Register the endpoints once per Home Assistant run."""
    if hass.data.get(_REGISTERED):
        return
    hass.http.register_view(ConfigView())
    hass.http.register_view(PushView())
    hass.http.register_view(UnregisterView())
    hass.data[_REGISTERED] = True


def _runtime(hass: HomeAssistant) -> PushRuntime | None:
    """The loaded entry's runtime (one bundle id is expected)."""
    return next(iter((hass.data.get(DOMAIN) or {}).values()), None)


def push_url(hass: HomeAssistant, runtime: PushRuntime) -> str:
    """Where ``mobile_app`` should post: the configured base, else Home Assistant's internal URL."""
    base = (runtime.options.get(CONF_PUSH_BASE_URL) or "").strip().rstrip("/")
    if not base:
        try:
            base = get_url(hass, allow_external=False, allow_cloud=False, prefer_external=False)
        except NoURLAvailableError:
            base = f"http://127.0.0.1:{hass.http.server_port or 8123}"
    return f"{base}{API_PUSH}"


class DailyCounter:
    """Pushes per device per UTC day, reset at midnight like Home Assistant's own relay."""

    def __init__(self, limit: int = DAILY_LIMIT) -> None:
        self.limit = limit
        self._day = datetime.now(UTC).date()
        self.sent: dict[str, int] = defaultdict(int)
        self.errors: dict[str, int] = defaultdict(int)

    def _roll(self, now: datetime) -> None:
        if now.date() != self._day:
            self._day = now.date()
            self.sent.clear()
            self.errors.clear()

    def forget(self, key: str) -> None:
        self.sent.pop(key, None)
        self.errors.pop(key, None)

    def exhausted(self, key: str, now: datetime | None = None) -> bool:
        self._roll(now or datetime.now(UTC))
        return self.sent[key] >= self.limit

    def rate_limits(self, key: str) -> dict[str, Any]:
        resets = datetime.combine(self._day + timedelta(days=1), datetime.min.time(), tzinfo=UTC)
        return {"successful": self.sent[key], "errors": self.errors[key], "maximum": self.limit,
                "resetsAt": resets.isoformat()}


class CallLimiter:
    """At most ``limit`` calls per client address per sliding minute (a light guard against probing webhook ids)."""

    def __init__(self, limit: int = UNREGISTER_PER_MINUTE, window: float = 60.0) -> None:
        self.limit = limit
        self.window = window
        self._calls: dict[str, list[float]] = {}

    def allow(self, key: str, now: float | None = None) -> bool:
        now = time.monotonic() if now is None else now
        recent = [t for t in self._calls.get(key, []) if now - t < self.window]
        allowed = len(recent) < self.limit
        if allowed:
            recent.append(now)
        if recent:
            self._calls[key] = recent
        else:
            self._calls.pop(key, None)
        if len(self._calls) > 1024:  # forget quiet addresses so the table can't grow without bound
            for stale in [k for k, v in self._calls.items() if now - v[-1] >= self.window]:
                del self._calls[stale]
        return allowed


def _message(text: str, status: HTTPStatus) -> web.Response:
    return web.json_response({"message": text}, status=status)


class ConfigView(HomeAssistantView):
    """What the app needs before it registers."""

    url = API_CONFIG
    #: The pre-1.0 path, served by the same handler while registrations and the app move.
    extra_urls = [LEGACY_API_CONFIG]
    name = "api:fenton_home:config"

    async def get(self, request: web.Request) -> web.Response:
        hass = request.app[KEY_HASS]
        runtime = _runtime(hass)
        if runtime is None:
            return self.json({"available": False, "push_url": None, "bundle_id": None, "critical_alerts": False})
        return self.json({"available": True, "push_url": push_url(hass, runtime), "bundle_id": runtime.bundle_id,
                          "critical_alerts": runtime.critical_alerts})


class PushView(HomeAssistantView):
    """Receives ``mobile_app``'s push posts and forwards them to Apple."""

    url = API_PUSH
    extra_urls = [LEGACY_API_PUSH]
    name = "api:fenton_home:apns"
    requires_auth = False

    async def post(self, request: web.Request) -> web.Response:
        hass = request.app[KEY_HASS]
        runtime = _runtime(hass)
        if runtime is None:
            return _message("S+H Home Integration isn't set up", HTTPStatus.SERVICE_UNAVAILABLE)
        try:
            body: Any = await request.json()
        except ValueError:
            return _message("Body isn't JSON", HTTPStatus.BAD_REQUEST)
        if not isinstance(body, dict):
            return _message("Body isn't a JSON object", HTTPStatus.BAD_REQUEST)

        info = body.get("registration_info") if isinstance(body.get("registration_info"), dict) else {}
        webhook_id = str(info.get("webhook_id") or "")
        entry = registration(hass, webhook_id) if webhook_id else None
        app_data = (entry or {}).get("app_data") or {}
        token = str(body.get("push_token") or "")
        if (entry is None or entry.get("app_id") != runtime.bundle_id or not token
                or not hmac.compare_digest(token, str(app_data.get("push_token") or ""))):
            return _message("Unknown Fenton Home registration", HTTPStatus.FORBIDDEN)

        counter = runtime.counter
        if counter.exhausted(webhook_id):
            return web.json_response({"message": f"Daily limit of {counter.limit} reached for this device",
                                      "rateLimits": counter.rate_limits(webhook_id)},
                                     status=HTTPStatus.TOO_MANY_REQUESTS)
        tag = str((body.get("data") or {}).get("tag") or "") if isinstance(body.get("data"), dict) else ""
        try:
            apns_request = build(body, critical_enabled=runtime.critical_alerts, timestamps=runtime.timestamps,
                                 ts_key=f"{webhook_id}:{tag}")
        except PayloadError as err:
            counter.errors[webhook_id] += 1
            return _message(str(err), HTTPStatus.BAD_REQUEST)
        for note in apns_request.notes:
            _LOGGER.debug("%s: %s", entry.get("device_name"), note)
        if apns_request.dropped:
            # Nothing to send, and not a failure: core must not retry or log an error for it.
            return self.json({"rateLimits": counter.rate_limits(webhook_id)}, status_code=HTTPStatus.CREATED)

        device_token = str(body.get(apns_request.token_key) or "")
        environment = app_data.get(APP_DATA_ENVIRONMENT)
        environment = environment if environment in ENVIRONMENTS else "production"
        try:
            await runtime.client.send(device_token, apns_request, environment)
        except APNsError as err:
            if apns_request.push_type == "liveactivity" and err.status == 410:
                if (answer := await self._recover_activity(hass, runtime, webhook_id, tag, body, app_data,
                                                           environment)) is not None:
                    return answer
            if err.reason == "BadDeviceToken":
                # A token from the other APNs environment (an Xcode build reporting production, or the reverse).
                other = "sandbox" if environment == "production" else "production"
                try:
                    await runtime.client.send(device_token, apns_request, other)
                except APNsError as retry_err:
                    err = retry_err
                else:
                    counter.sent[webhook_id] += 1
                    return self.json({"rateLimits": counter.rate_limits(webhook_id)}, status_code=HTTPStatus.CREATED)
            counter.errors[webhook_id] += 1
            return self._apns_failure(entry, err)
        counter.sent[webhook_id] += 1
        return self.json({"rateLimits": counter.rate_limits(webhook_id)}, status_code=HTTPStatus.CREATED)

    async def _recover_activity(self, hass: HomeAssistant, runtime: PushRuntime, webhook_id: str, tag: str,
                                body: dict[str, Any], app_data: dict[str, Any], environment: str) -> web.Response | None:
        """Apple says this activity is gone (410): drop core's dead token for the tag and, for an update, start again.

        Core turns a start for a tag that still has a stored token into an update, so without this the tag could not
        start again until the token expired. An ``end`` that finds the activity already gone has done its job. Returns
        the answer for core, or None to report the 410 as usual.
        """
        counter = runtime.counter
        event = str((body.get("data") or {}).get("event") or "")
        removed = recovery.remove_token(hass, webhook_id, tag) if tag else False
        _LOGGER.info("Live activity %s on %s is gone (410); stored token %s", tag, webhook_id,
                     "removed" if removed else "left in place")
        if event == "end":
            return self.json({"rateLimits": counter.rate_limits(webhook_id)}, status_code=HTTPStatus.CREATED)
        start_token = str(app_data.get("start_live_activity_token") or "")
        if event != "update" or not start_token:
            return None
        retry = copy.deepcopy(body)
        retry["live_activity_token"] = start_token
        retry["data"]["event"] = "start"
        try:
            request = build(retry, critical_enabled=runtime.critical_alerts, timestamps=runtime.timestamps,
                            ts_key=f"{webhook_id}:{tag}")
            await runtime.client.send(start_token, request, environment)
        except (PayloadError, APNsError) as err:
            _LOGGER.warning("Couldn't restart %s on %s after its activity ended: %s", tag, webhook_id, err)
            return None
        counter.sent[webhook_id] += 1
        return self.json({"rateLimits": counter.rate_limits(webhook_id)}, status_code=HTTPStatus.CREATED)

    @staticmethod
    def _apns_failure(entry: dict[str, Any], err: APNsError) -> web.Response:
        name = entry.get("device_name", "device")
        if err.dead_token:
            return _message(f"Apple no longer accepts {name}'s push token ({err.reason}); open Fenton Home on it "
                            "to refresh it", HTTPStatus.GONE)
        if err.bad_credentials:
            _LOGGER.error("Apple rejected the S+H Home Integration key (%s); reconfigure the integration", err.reason)
            return _message(f"Apple rejected the push key ({err.reason})", HTTPStatus.BAD_GATEWAY)
        if err.status == 0:
            return _message(f"Couldn't reach Apple ({err.reason})", HTTPStatus.BAD_GATEWAY)
        return _message(f"Apple refused the push ({err.status} {err.reason})", HTTPStatus.BAD_GATEWAY)


class UnregisterView(HomeAssistantView):
    """The app removes its own ``mobile_app`` registration (sign-out, or a retry after a failed sign-out).

    ``POST {"webhook_id": "<id>"}``. The webhook id is the proof, with the same trust as ``mobile_app``'s own webhooks,
    so no login is needed and a retry still works after the access token is gone. It can only remove a registration of
    this integration's app (``app_id`` equals the bundle id). When the request does carry a valid Home Assistant login,
    that user must own the registration (403 otherwise). Answers: 200 ``{"removed": true}``; 410 ``unknown_webhook``
    when there is no such S+H registration (already gone counts as done); 400 for a bad body; 429 past the per-address
    cap on calls without a login; 503 while the integration isn't loaded.
    """

    url = API_UNREGISTER
    name = "api:fenton_home:devices:unregister"
    requires_auth = False

    def __init__(self) -> None:
        self.limiter = CallLimiter()

    async def post(self, request: web.Request) -> web.Response:
        hass = request.app[KEY_HASS]
        runtime = _runtime(hass)
        if runtime is None:
            return web.json_response({"error": "unavailable"}, status=HTTPStatus.SERVICE_UNAVAILABLE)
        user = request.get(KEY_HASS_USER)
        if user is None and not self.limiter.allow(request.remote or "unknown"):
            return web.json_response({"error": "rate_limited"}, status=HTTPStatus.TOO_MANY_REQUESTS,
                                     headers={"Retry-After": "60"})
        try:
            body: Any = await request.json()
        except ValueError:
            body = None
        webhook_id = body.get("webhook_id") if isinstance(body, dict) else None
        if not isinstance(webhook_id, str) or not webhook_id:
            return web.json_response({"error": "bad_request"}, status=HTTPStatus.BAD_REQUEST)

        entry = next((e for e in hass.config_entries.async_entries(MOBILE_APP)
                      if e.data.get("app_id") == runtime.bundle_id
                      and hmac.compare_digest(str(e.data.get("webhook_id") or ""), webhook_id)), None)
        if entry is None:
            return web.json_response({"error": "unknown_webhook"}, status=HTTPStatus.GONE)
        if user is not None and user.id != entry.data.get("user_id"):
            return web.json_response({"error": "forbidden"}, status=HTTPStatus.FORBIDDEN)
        name = entry.data.get("device_name", "device")
        await hass.config_entries.async_remove(entry.entry_id)
        _LOGGER.info("Unregistered %s (webhook %s…) at the app's request", name, short(webhook_id))
        return web.json_response({"removed": True})
