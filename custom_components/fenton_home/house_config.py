"""The house configuration document: one file under ``/config/fenton_home`` served to the app over the websocket.

The integration does not know what is in the file. It checks that the document is shaped like a configuration
(a mapping with an integer ``schema`` of at least 1, whose top-level values are mappings or lists, under
``MAX_BYTES``), hashes it, and serves it to any signed-in user. Field-level validation belongs to the app, whose
schema is versioned by the ``schema`` number. A bad file raises a Repairs issue and the last good document stays in
service. ``house.yaml`` wins over ``house.json`` when both exist; neither means "not configured".
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import voluptuous as vol
import yaml
from homeassistant.components import websocket_api
from homeassistant.core import HomeAssistant, ServiceCall, callback
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.service import async_register_admin_service

from .const import (
    DOMAIN,
    HOUSE_CONFIG_DIR,
    HOUSE_CONFIG_FILES,
    HOUSE_CONFIG_MAX_BYTES,
    ISSUE_HOUSE_CONFIG_INVALID,
    SERVICE_RELOAD_HOUSE_CONFIG,
    WS_HOUSE_CONFIG,
    WS_HOUSE_CONFIG_SUBSCRIBE,
)

_LOGGER = logging.getLogger(__name__)

#: ``hass.data`` key of the loaded document (kept apart from ``hass.data[DOMAIN]``, which is keyed by entry id).
DATA_HOUSE_CONFIG = f"{DOMAIN}_house_config"

NOT_CONFIGURED = {"configured": False}


class HouseConfigError(Exception):
    """The file is unreadable or not shaped like a configuration. ``reason`` is a short code, never file content."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True)
class HouseDocument:
    """A validated document: ``payload`` is exactly what the websocket sends."""

    payload: dict[str, Any]


def find_file(directory: Path) -> Path | None:
    """The configuration file to use: ``house.yaml`` before ``house.json``; ``None`` when neither exists."""
    for name in HOUSE_CONFIG_FILES:
        if (path := directory / name).is_file():
            return path
    return None


def parse_document(text: str, *, is_json: bool) -> dict[str, Any]:
    """Parse ``text`` and apply the shallow checks. Raises ``HouseConfigError``."""
    try:
        return _parse(text, is_json)
    except RecursionError as err:  # a file nested thousands deep
        raise HouseConfigError("too_deep") from err


def _parse(text: str, is_json: bool) -> dict[str, Any]:
    try:
        data = json.loads(text) if is_json else yaml.safe_load(text)
    except (ValueError, yaml.YAMLError) as err:
        raise HouseConfigError("unparseable") from err
    if not isinstance(data, dict):
        raise HouseConfigError("not_a_mapping")
    schema = data.get("schema")
    if isinstance(schema, bool) or not isinstance(schema, int) or schema < 1:
        raise HouseConfigError("bad_schema")
    config = {key: value for key, value in data.items() if key != "schema"}
    if not all(isinstance(value, (dict, list)) for value in config.values()):
        raise HouseConfigError("bad_section")
    try:
        canonical = json.dumps(config, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)
    except (TypeError, ValueError) as err:  # YAML dates, NaN and Infinity
        raise HouseConfigError("not_json") from err
    digest = hashlib.sha256(f"{schema}:{canonical}".encode()).hexdigest()
    return {"schema": schema, "hash": digest, "config": config}


def load_file(directory: Path) -> dict[str, Any] | None:
    """Blocking: read and validate the configuration file. ``None`` when not configured. Raises ``HouseConfigError``."""
    try:
        path = find_file(directory)
        if path is None:
            return None
        with path.open("rb") as handle:
            raw = handle.read(HOUSE_CONFIG_MAX_BYTES + 1)  # bounded: never reads more than the limit plus one byte
        if len(raw) > HOUSE_CONFIG_MAX_BYTES:
            raise HouseConfigError("too_large")
        text = raw.decode("utf-8")
    except (OSError, UnicodeDecodeError) as err:
        raise HouseConfigError("unreadable") from err
    return parse_document(text, is_json=path.suffix == ".json")


class HouseConfig:
    """The last good document and the websocket subscribers that follow it."""

    def __init__(self, hass: HomeAssistant) -> None:
        self.hass = hass
        self.document: dict[str, Any] | None = None
        self._listeners: list[Callable[[dict[str, Any]], None]] = []
        #: False once the config entry is unloaded: the commands then answer "not configured".
        self.active = True
        self._lock = asyncio.Lock()

    @property
    def payload(self) -> dict[str, Any]:
        """What the commands answer: the document, or ``{"configured": false}``."""
        return self.document if self.active and self.document is not None else NOT_CONFIGURED

    async def async_reload(self) -> bool:
        """Read the file off the event loop. On success replace the document and tell subscribers; on any problem
        raise the Repairs issue and keep the last good document. Never raises. Returns whether the file was accepted.
        Reloads are serialised, so two at once cannot finish out of order."""
        async with self._lock:
            if not self.active:
                return False
            directory = Path(self.hass.config.path(HOUSE_CONFIG_DIR))
            try:
                document = await self.hass.async_add_executor_job(load_file, directory)
            except HouseConfigError as err:
                reason = err.reason
            except Exception:  # noqa: BLE001 - a house file must never take the integration down
                _LOGGER.exception("house configuration could not be loaded")
                reason = "unexpected"
            else:
                ir.async_delete_issue(self.hass, DOMAIN, ISSUE_HOUSE_CONFIG_INVALID)
                self.document = document
                self._notify()
                return True
            _LOGGER.warning("house configuration rejected (%s); keeping the last good document", reason)
            ir.async_create_issue(
                self.hass, DOMAIN, ISSUE_HOUSE_CONFIG_INVALID, is_fixable=False, severity=ir.IssueSeverity.ERROR,
                translation_key=ISSUE_HOUSE_CONFIG_INVALID,
                translation_placeholders={"reason": reason, "directory": f"{HOUSE_CONFIG_DIR}/"})
            return False

    def _notify(self) -> None:
        for listener in list(self._listeners):
            try:
                listener(self.payload)
            except Exception:  # noqa: BLE001 - one dead subscriber must not stop the rest
                _LOGGER.exception("house configuration subscriber failed")

    async def async_activate(self) -> None:
        """The config entry loaded: serve (again) and read the file."""
        self.active = True
        await self.async_reload()

    @callback
    def async_deactivate(self) -> None:
        """The config entry unloaded: subscribers hear "not configured" and are dropped; the commands answer the
        same until the entry loads again."""
        self.active = False
        self.document = None
        self._notify()
        self._listeners.clear()
        ir.async_delete_issue(self.hass, DOMAIN, ISSUE_HOUSE_CONFIG_INVALID)

    @callback
    def async_subscribe(self, listener: Callable[[dict[str, Any]], None]) -> Callable[[], None]:
        """Call ``listener`` with the payload after each successful reload. Returns an idempotent unsubscribe."""
        self._listeners.append(listener)

        @callback
        def _unsubscribe() -> None:
            if listener in self._listeners:
                self._listeners.remove(listener)

        return _unsubscribe


def get(hass: HomeAssistant) -> HouseConfig:
    """The shared ``HouseConfig`` for ``hass``."""
    return hass.data[DATA_HOUSE_CONFIG]


@websocket_api.websocket_command({vol.Required("type"): WS_HOUSE_CONFIG})
@callback
def _ws_get(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]) -> None:
    connection.send_result(msg["id"], get(hass).payload)


@websocket_api.websocket_command({vol.Required("type"): WS_HOUSE_CONFIG_SUBSCRIBE})
@callback
def _ws_subscribe(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]) -> None:
    msg_id = msg["id"]

    @callback
    def _forward(payload: dict[str, Any]) -> None:
        connection.send_message(websocket_api.event_message(msg_id, payload))

    connection.subscriptions[msg_id] = get(hass).async_subscribe(_forward)
    connection.send_result(msg_id)
    _forward(get(hass).payload)


async def async_setup_house_config(hass: HomeAssistant) -> None:
    """Load the document, register the two websocket commands (any signed-in user may read) and the admin
    reload service. Safe to call again."""
    if DATA_HOUSE_CONFIG in hass.data:
        return
    house = hass.data[DATA_HOUSE_CONFIG] = HouseConfig(hass)
    websocket_api.async_register_command(hass, _ws_get)
    websocket_api.async_register_command(hass, _ws_subscribe)

    async def _reload(_: ServiceCall) -> None:
        await house.async_reload()

    async_register_admin_service(hass, DOMAIN, SERVICE_RELOAD_HOUSE_CONFIG, _reload)
    await house.async_reload()
