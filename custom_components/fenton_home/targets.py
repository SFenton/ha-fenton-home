"""Who is signed in where: Fenton Home's ``mobile_app`` registrations and the people they belong to.

Home Assistant stores the user who registered each mobile device on its config entry. The app registers with
the signed-in user's token and clears ``app_data`` on sign-out (``docs/CONTRACT.md``), so "devices that can
receive a push right now" is exactly the registrations below.
"""

from __future__ import annotations

from collections.abc import Collection
from dataclasses import dataclass
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.util import slugify

from .const import MOBILE_APP, TARGET_EVERYONE

DEVICE_KINDS = ("phone", "tablet", "watch")


@dataclass(frozen=True)
class Device:
    """One Fenton Home registration that can receive pushes."""

    webhook_id: str
    user_id: str
    name: str
    model: str

    @property
    def kind(self) -> str:
        model = self.model.lower()
        if model.startswith("ipad"):
            return "tablet"
        if model.startswith("watch"):
            return "watch"
        return "phone"


def _can_push(app_data: dict[str, Any]) -> bool:
    """Mirrors ``mobile_app``'s own test: cloud push credentials, or the websocket channel."""
    return bool(("push_token" in app_data and "push_url" in app_data) or app_data.get("push_websocket_channel"))


def registration(hass: HomeAssistant, webhook_id: str) -> dict[str, Any] | None:
    """The ``mobile_app`` entry data for a webhook id, if there is one."""
    for entry in hass.config_entries.async_entries(MOBILE_APP):
        if entry.data.get("webhook_id") == webhook_id:
            return dict(entry.data)
    return None


def devices(hass: HomeAssistant, bundle_id: str) -> list[Device]:
    """Every Fenton Home registration whose user is signed in to it."""
    found = []
    for entry in hass.config_entries.async_entries(MOBILE_APP):
        data = entry.data
        if data.get("app_id") != bundle_id or not _can_push(data.get("app_data") or {}):
            continue
        found.append(Device(webhook_id=data["webhook_id"], user_id=data.get("user_id", ""),
                            name=data.get("device_name", ""), model=data.get("model", "")))
    return found


def live_devices(hass: HomeAssistant, bundle_id: str, only: Collection[str] | None = None) -> list[Device]:
    """Fenton Home registrations that can show a Live Activity: the app handed over a push-to-start token.

    ``only`` limits them to those webhook ids (the integration's "which registrations get cards" option).
    """
    found = []
    for entry in hass.config_entries.async_entries(MOBILE_APP):
        data = entry.data
        app_data = data.get("app_data") or {}
        if data.get("app_id") != bundle_id or not _can_push(app_data) or not app_data.get("start_live_activity_token"):
            continue
        if only and data["webhook_id"] not in only:
            continue
        found.append(Device(webhook_id=data["webhook_id"], user_id=data.get("user_id", ""),
                            name=data.get("device_name", ""), model=data.get("model", "")))
    return found


@dataclass(frozen=True)
class Person:
    """A person entity linked to a Home Assistant user."""

    entity_id: str
    user_id: str
    name: str


def people(hass: HomeAssistant) -> list[Person]:
    """People who can sign in (a person without a user can't own a device)."""
    found = []
    for state in hass.states.async_all("person"):
        if user_id := state.attributes.get("user_id"):
            found.append(Person(entity_id=state.entity_id, user_id=user_id,
                                name=state.attributes.get("friendly_name") or state.object_id))
    return sorted(found, key=lambda p: p.entity_id)


def target_names(found: list[Person]) -> dict[str, Person]:
    """Notify target name per person: the first name when it is unique, else the person's object id."""
    first = [slugify(p.name.split()[0]) if p.name.split() else slugify(p.entity_id.split(".", 1)[1]) for p in found]
    names: dict[str, Person] = {}
    for person, name in zip(found, first, strict=True):
        if not name or first.count(name) > 1 or name == TARGET_EVERYONE:
            name = slugify(person.entity_id.split(".", 1)[1])
        names[name] = person
    return names


def resolve(hass: HomeAssistant, bundle_id: str, target: str, kinds: list[str] | None = None) -> list[Device]:
    """Devices for one notify target (``everyone``, a target name, a ``person.*`` id or a user id)."""
    registered = devices(hass, bundle_id)
    if target == TARGET_EVERYONE:
        user_ids = {p.user_id for p in people(hass)} or {d.user_id for d in registered}
    else:
        person = target_names(people(hass)).get(target)
        if person is None:
            person = next((p for p in people(hass) if target in (p.entity_id, p.user_id)), None)
        user_ids = {person.user_id} if person else {target}
    chosen = [d for d in registered if d.user_id in user_ids]
    if kinds:
        chosen = [d for d in chosen if d.kind in kinds]
    return chosen
