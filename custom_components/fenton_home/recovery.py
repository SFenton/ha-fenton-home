"""Dead Live Activity tokens (spec section 9, item 4) and a peek at core's token store.

Home Assistant core turns a start for a tag that still has a stored token into an update. When the person swiped the
activity away (or it ran out) while Home Assistant did not hear, Apple answers 410 to that update and core keeps the
token, so the tag could not start again until the token expired (8 hours). On a 410 for a Live Activity push, the relay
removes the tag's token through core's ``mobile_app.live_activity`` store and retries once as a start.

Core's store is private, so every use is guarded: the module must import, its function must still have the shape we
call, and the Home Assistant version must be one that has Live Activities (2026.7). If core changes, this logs once and
the rest of the integration carries on. The upstream issue that makes this unnecessary is drafted in the pull request.
"""

from __future__ import annotations

import importlib
import inspect
import logging
from typing import Any

from homeassistant.const import __version__ as HA_VERSION  # noqa: N812
from homeassistant.core import HomeAssistant

_LOGGER = logging.getLogger(__name__)

STORE_MODULE = "homeassistant.components.mobile_app.live_activity.store"
CONST_MODULE = "homeassistant.components.mobile_app.const"
#: The first Home Assistant release with ``mobile_app`` Live Activities.
MIN_VERSION = (2026, 7)

_warned: set[str] = set()


def _warn_once(reason: str) -> None:
    if reason not in _warned:
        _warned.add(reason)
        _LOGGER.warning("Can't use Home Assistant's Live Activity token store (%s); a dead token will expire on its own",
                        reason)


def _version() -> tuple[int, ...]:
    parts = []
    for piece in HA_VERSION.split(".")[:2]:
        digits = "".join(ch for ch in piece if ch.isdigit())
        parts.append(int(digits) if digits else 0)
    return tuple(parts)


def _supported() -> bool:
    if _version() < MIN_VERSION:
        _warn_once(f"Home Assistant {HA_VERSION} predates Live Activities")
        return False
    return True


def remove_token(hass: HomeAssistant, webhook_id: str, tag: str) -> bool:
    """Drop the stored token for ``tag`` on a registration. Returns whether the store was reachable."""
    if not _supported():
        return False
    try:
        store = importlib.import_module(STORE_MODULE)
        remove = store.remove_live_activity_token
        names = list(inspect.signature(remove).parameters)
        if len(names) != 3:
            raise TypeError(f"remove_live_activity_token takes {names}")
    except (ImportError, AttributeError, TypeError, ValueError) as err:
        _warn_once(str(err))
        return False
    try:
        remove(hass, webhook_id, tag)
    except Exception as err:  # noqa: BLE001 - private API: anything may have changed
        _warn_once(f"{type(err).__name__}: {err}")
        return False
    return True


def has_token(hass: HomeAssistant, webhook_id: str, tag: str) -> bool | None:
    """Whether core still holds a Live Activity token for the tag; None when the store can't be read."""
    if not _supported():
        return None
    try:
        const = importlib.import_module(CONST_MODULE)
        tokens: dict[str, Any] = hass.data[const.DOMAIN][const.DATA_LIVE_ACTIVITY_TOKENS]
        return tag in tokens.get(webhook_id, {})
    except (ImportError, AttributeError, KeyError, TypeError) as err:
        _warn_once(str(err))
        return None
