"""A read-only snapshot of the entity states the job adapters look at.

The adapters in ``jobs.py`` are pure functions over a ``States``. Tests build one from recorded values; the engine
builds one from Home Assistant with ``States.from_hass``. No Home Assistant imports here.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, tzinfo
from typing import Any

UNAVAILABLE = frozenset({"unavailable", "unknown", "none", ""})


@dataclass(frozen=True)
class Ent:
    """One entity: its state, attributes and ``last_changed`` (Unix seconds)."""

    state: str
    attrs: Mapping[str, Any] = field(default_factory=dict)
    changed: float = 0.0


class States:
    """Entity states by id. Missing entities read as ``unavailable``."""

    def __init__(self, ents: Mapping[str, Ent] | None = None) -> None:
        self._ents: dict[str, Ent] = dict(ents or {})

    @classmethod
    def build(cls, spec: Mapping[str, Any]) -> States:
        """``{entity_id: state | (state, attrs) | (state, attrs, changed)}``, for tests."""
        ents: dict[str, Ent] = {}
        for entity_id, value in spec.items():
            if isinstance(value, Ent):
                ents[entity_id] = value
            elif isinstance(value, tuple):
                ents[entity_id] = Ent(str(value[0]), dict(value[1]) if len(value) > 1 else {},
                                      float(value[2]) if len(value) > 2 else 0.0)
            else:
                ents[entity_id] = Ent(str(value))
        return cls(ents)

    @classmethod
    def from_hass(cls, hass: Any, entity_ids: Iterable[str]) -> States:
        ents: dict[str, Ent] = {}
        for entity_id in entity_ids:
            if (state := hass.states.get(entity_id)) is not None:
                ents[entity_id] = Ent(state.state, dict(state.attributes), state.last_changed.timestamp())
        return cls(ents)

    def with_(self, **changes: Any) -> States:
        """A copy with entities replaced; ``__`` stands for ``.`` in the keyword names (tests)."""
        merged = dict(self._ents)
        for key, value in changes.items():
            entity_id = key.replace("__", ".", 1)
            merged.update(States.build({entity_id: value})._ents)
        return States(merged)

    def __contains__(self, entity_id: str) -> bool:
        return entity_id in self._ents

    def get(self, entity_id: str) -> Ent | None:
        return self._ents.get(entity_id)

    def state(self, entity_id: str, default: str = "unavailable") -> str:
        ent = self._ents.get(entity_id)
        return ent.state if ent is not None else default

    def known(self, entity_id: str) -> str | None:
        """The state unless it is missing, ``unavailable``, ``unknown`` or empty."""
        state = self.state(entity_id, "")
        return None if state.lower() in UNAVAILABLE else state

    def is_on(self, entity_id: str) -> bool:
        return self.state(entity_id, "off") == "on"

    def attr(self, entity_id: str, key: str, default: Any = None) -> Any:
        ent = self._ents.get(entity_id)
        if ent is None:
            return default
        value = ent.attrs.get(key)
        return default if value is None else value

    def changed(self, entity_id: str) -> float | None:
        ent = self._ents.get(entity_id)
        return ent.changed if ent is not None and ent.changed else None

    def number(self, entity_id: str) -> float | None:
        known = self.known(entity_id)
        if known is None:
            return None
        try:
            return float(known)
        except ValueError:
            return None

    def timestamp(self, entity_id: str, tz: tzinfo | None = None) -> float | None:
        """A timestamp-valued state (ISO 8601, as Home Connect and the timer entities give) in Unix seconds.

        A time with no offset is read in ``tz`` (default UTC).
        """
        known = self.known(entity_id)
        return None if known is None else parse_time(known, tz)

    def entity_ids(self) -> list[str]:
        return sorted(self._ents)


def parse_time(value: Any, tz: tzinfo | None = None) -> float | None:
    """Unix seconds from an ISO 8601 string, or a number (milliseconds recognised); None when it isn't a time."""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        if value <= 0:
            return None
        return value / 1000 if value > 1e11 else float(value)
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    try:
        return parse_time(float(text))
    except ValueError:
        return _iso(text, tz)


def _iso(text: str, tz: tzinfo | None) -> float | None:
    try:
        moment = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=tz or UTC)
    return moment.timestamp()
