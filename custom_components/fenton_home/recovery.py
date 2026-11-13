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
import time
from collections.abc import Callable
from typing import Any

from homeassistant.const import __version__ as HA_VERSION  # noqa: N812
from homeassistant.core import CALLBACK_TYPE, HomeAssistant

_LOGGER = logging.getLogger(__name__)

STORE_MODULE = "homeassistant.components.mobile_app.live_activity.store"
CONST_MODULE = "homeassistant.components.mobile_app.const"
WEBHOOK_MODULE = "homeassistant.components.mobile_app.live_activity.webhook"
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


def stored_tokens(hass: HomeAssistant) -> dict[str, dict[str, str]] | None:
    """Core's unexpired per-activity tokens as ``{webhook_id: {tag: token}}``; None when the store can't be read."""
    if not _supported():
        return None
    try:
        const = importlib.import_module(CONST_MODULE)
        held: dict[str, dict[str, dict[str, Any]]] = hass.data[const.DOMAIN][const.DATA_LIVE_ACTIVITY_TOKENS]
        token_key = getattr(const, "ATTR_TOKEN", "token")
        expiry_key = getattr(const, "ATTR_LIVE_ACTIVITY_EXPIRES_AT", "expires_at")
        now = time.time()
        found: dict[str, dict[str, str]] = {}
        for webhook_id, tags in held.items():
            live = {tag: str(item[token_key]) for tag, item in tags.items() if item[expiry_key] > now}
            if live:
                found[webhook_id] = live
        return found
    except (ImportError, AttributeError, KeyError, TypeError) as err:
        _warn_once(str(err))
        return None


# -- Watching token reports -------------------------------------------------------------------------------------------

#: Called as ``listener(webhook_id, tag)`` whenever a phone reports an activity's update token.
Listener = Callable[[str, str], None]
_listeners: list[Listener] = []
_patched: dict[str, Any] = {}


def _wrap_store(original: Callable[..., Any]) -> Callable[..., Any]:
    def store_live_activity_token(hass: HomeAssistant, webhook_id: str, activity_tag: str, token: str,
                                  expires_at: float) -> None:
        original(hass, webhook_id, activity_tag, token, expires_at)
        for listener in list(_listeners):
            try:
                listener(webhook_id, activity_tag)
            except Exception:  # noqa: BLE001 - a listener must never break core's webhook
                _LOGGER.exception("Live Activity token listener failed")

    return store_live_activity_token


def watch_reports(listener: Listener) -> CALLBACK_TYPE | None:
    """Call ``listener(webhook_id, tag)`` after core stores a token a phone reported; returns the way to stop.

    Core fires no event when the app posts ``live_activity_token``, so this wraps the function its webhook handler
    calls to store it. Guarded like the rest of this module: None (and one log line) when core doesn't look like
    2026.7's, and the engine's own cycles still find orphans, only later.
    """
    if not _supported():
        return None
    if not _patched:
        try:
            module = importlib.import_module(WEBHOOK_MODULE)
            original = module.store_live_activity_token
            names = list(inspect.signature(original).parameters)
            if len(names) != 5:
                raise TypeError(f"store_live_activity_token takes {names}")
        except (ImportError, AttributeError, TypeError, ValueError) as err:
            _warn_once(str(err))
            return None
        wrapper = _wrap_store(original)
        module.store_live_activity_token = wrapper
        _patched.update(module=module, original=original, wrapper=wrapper)
    _listeners.append(listener)

    def stop() -> None:
        if listener in _listeners:
            _listeners.remove(listener)
        if not _listeners and _patched:
            module = _patched.pop("module")
            original, wrapper = _patched.pop("original"), _patched.pop("wrapper")
            if getattr(module, "store_live_activity_token", None) is wrapper:
                module.store_live_activity_token = original

    return stop
