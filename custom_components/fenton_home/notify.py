"""Person targets: ``notify.fenton_<name>`` sends to every device where that person is signed in to Fenton Home.

Each matching device is reached by its webhook id through ``mobile_app``'s own notification service object
(``send_to_device``), so ``mobile_app`` still decides between its websocket channel and our ``push_url`` and handles
Live Activity tokens. Going by webhook id matters: ``mobile_app`` keeps one ``notify.mobile_app_<device name>`` action
per name, so devices that share a name (three registrations called "Stephen's iPhone · Fenton") can't be told apart
by action. One call per device means a websocket-only device that isn't connected, which ``mobile_app`` raises for,
is logged and the rest still get the notification. When the service object isn't reachable, the name-based action is
the fallback. The notification data is passed through untouched; ``data.fenton.devices`` (``phone``, ``tablet``, ``watch``) narrows
the devices.
"""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.notify import ATTR_DATA, ATTR_TARGET, ATTR_TITLE, BaseNotificationService
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.typing import ConfigType, DiscoveryInfoType
from homeassistant.util import slugify

from .const import DOMAIN, MOBILE_APP, TARGET_EVERYONE
from .targets import DEVICE_KINDS, Device, people, resolve, target_names

try:
    from homeassistant.components.notify.legacy import NOTIFY_SERVICES
except ImportError:  # Moved or renamed in a later Home Assistant; fall back to the action naming rule.
    NOTIFY_SERVICES = None

_LOGGER = logging.getLogger(__name__)


def _device_actions(hass: HomeAssistant) -> dict[str, str] | None:
    """Webhook id to ``mobile_app``'s action for that device, as ``mobile_app`` registered them.

    ``None`` when Home Assistant doesn't expose them; two devices with the same name share one action, which
    reaches only the newer, so the older one has none here.
    """
    if NOTIFY_SERVICES is None or (services := hass.data.get(NOTIFY_SERVICES)) is None:
        return None
    actions: dict[str, str] = {}
    for service in services.get(MOBILE_APP, []):
        for action, webhook_id in (getattr(service, "registered_targets", None) or {}).items():
            actions[webhook_id] = action
    return actions


def _mobile_app_service(hass: HomeAssistant) -> Any | None:
    """``mobile_app``'s notification service object, when Home Assistant exposes it."""
    if NOTIFY_SERVICES is None or (services := hass.data.get(NOTIFY_SERVICES)) is None:
        return None
    for service in services.get(MOBILE_APP, []):
        if callable(getattr(service, "async_send_message", None)):
            return service
    return None


async def send_to_device(hass: HomeAssistant, device: Device | None, webhook_id: str, service_data: dict[str, Any],
                         actions: dict[str, str] | None = None) -> bool:
    """Send one notification to one device by webhook id. ``False`` when there is no way to reach it.

    Raises ``HomeAssistantError`` when ``mobile_app`` refuses (a websocket-only device that isn't connected).
    ``device`` is only needed for the by-name fallback; ``actions`` is ``_device_actions(hass)`` if the caller has it.
    """
    if (service := _mobile_app_service(hass)) is not None:
        kwargs = {k: v for k, v in service_data.items() if k != "message"}
        try:
            await service.async_send_message(service_data.get("message", ""), target=[webhook_id], **kwargs)
        except KeyError:  # mobile_app has no registration under this webhook id (removed, disabled, or not loaded yet)
            # Debug only: the callers warn, and the live-card retry would otherwise repeat this every 30 s.
            _LOGGER.debug("mobile_app has no registration for webhook %s", webhook_id)
            return False
        return True
    if device is None:
        return False
    if (action := _device_action(hass, actions if actions is not None else _device_actions(hass), device)) is None:
        return False
    await hass.services.async_call("notify", action, service_data, blocking=True)
    return True


def _device_action(hass: HomeAssistant, actions: dict[str, str] | None, device: Device) -> str | None:
    if actions is not None:
        action = actions.get(device.webhook_id)
    else:
        action = slugify(f"{MOBILE_APP}_{device.name}")
    return action if action and hass.services.has_service("notify", action) else None


async def async_get_service(hass: HomeAssistant, config: ConfigType,
                            discovery_info: DiscoveryInfoType | None = None) -> FentonNotificationService | None:
    """Set up the person targets (loaded by discovery from ``async_setup_entry``)."""
    if discovery_info is None:
        return None
    return FentonNotificationService(discovery_info["entry_id"])


def _kinds(data: Any) -> list[str] | None:
    fenton = data.get("fenton") if isinstance(data, dict) else None
    wanted = fenton.get("devices") if isinstance(fenton, dict) else None
    if isinstance(wanted, str):
        wanted = [wanted]
    if not isinstance(wanted, list):
        return None
    kinds = [k for k in wanted if k in DEVICE_KINDS]
    return kinds or None


class FentonNotificationService(BaseNotificationService):
    """``notify.fenton`` (everyone) plus one target per person with a Home Assistant user."""

    def __init__(self, entry_id: str) -> None:
        self._entry_id = entry_id

    @property
    def targets(self) -> dict[str, str]:
        names = {name: name for name in target_names(people(self.hass))}
        names[TARGET_EVERYONE] = TARGET_EVERYONE
        return names

    def _bundle_id(self) -> str | None:
        runtime = self.hass.data.get(DOMAIN, {}).get(self._entry_id)
        return runtime.bundle_id if runtime else None

    async def async_send_message(self, message: str = "", **kwargs: Any) -> None:
        bundle_id = self._bundle_id()
        if bundle_id is None:
            _LOGGER.warning("S+H Home Integration isn't loaded; notification dropped")
            return
        data = kwargs.get(ATTR_DATA)
        kinds = _kinds(data)
        devices: dict[str, Device] = {}
        for target in kwargs.get(ATTR_TARGET) or [TARGET_EVERYONE]:
            for device in resolve(self.hass, bundle_id, target, kinds):
                devices.setdefault(device.webhook_id, device)
        if not devices:
            # Nobody is signed in to Fenton Home for this target yet; groups that also list the official app
            # still reach people, so this isn't an error.
            _LOGGER.debug("No signed-in Fenton Home device for %s", kwargs.get(ATTR_TARGET) or TARGET_EVERYONE)
            return
        service_data: dict[str, Any] = {"message": message}
        if (title := kwargs.get(ATTR_TITLE)) is not None:
            service_data[ATTR_TITLE] = title
        if data is not None:
            service_data[ATTR_DATA] = data
        actions = _device_actions(self.hass)
        for device in devices.values():
            try:
                sent = await send_to_device(self.hass, device, device.webhook_id, service_data, actions)
            except HomeAssistantError as err:
                _LOGGER.warning("Couldn't notify %s: %s", device.name, err)
                continue
            if not sent:
                _LOGGER.warning("Couldn't notify %s: Home Assistant has no way to reach it (is it registered with "
                                "mobile_app?)", device.name)
