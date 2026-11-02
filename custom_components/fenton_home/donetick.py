"""Donetick, through Home Assistant: find the task that belongs to a problem, and complete it.

The vacuum error automation creates a Donetick task named ``"<Vacuum name> · <mapped title>"`` in
``todo.all_tasks_internal`` and stores no id. The engine finds it by title: ``todo.get_items`` with status
``needs_action``, matched on ``summary``, where ``uid`` is ``"<task id>--<random>"`` (spec section 5).
"""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.core import Context, HomeAssistant
from homeassistant.exceptions import HomeAssistantError

from .entities import TODO_ENTITY

_LOGGER = logging.getLogger(__name__)


def task_id_from_uid(uid: Any) -> int | None:
    """``"123--6a1f..."`` to ``123``."""
    try:
        return int(str(uid).split("--", 1)[0])
    except (TypeError, ValueError):
        return None


async def open_tasks(hass: HomeAssistant) -> list[dict[str, Any]] | None:
    """Every task that still needs doing, or None when the todo list can't be read."""
    if hass.states.get(TODO_ENTITY) is None:
        return None
    try:
        response = await hass.services.async_call(
            "todo", "get_items", {"entity_id": TODO_ENTITY, "status": "needs_action"}, blocking=True,
            return_response=True)
    except HomeAssistantError as err:
        _LOGGER.debug("Couldn't read %s: %s", TODO_ENTITY, err)
        return None
    items = (response or {}).get(TODO_ENTITY, {}).get("items", [])
    return [item for item in items if isinstance(item, dict)]


async def find_task(hass: HomeAssistant, summary: str) -> int | None:
    """The id of the open task titled ``summary`` (the newest when there are several), else None."""
    items = await open_tasks(hass)
    ids = [task_id for item in items or [] if item.get("summary") == summary
           and (task_id := task_id_from_uid(item.get("uid"))) is not None]
    return max(ids) if ids else None


async def open_task_ids(hass: HomeAssistant) -> set[int] | None:
    items = await open_tasks(hass)
    if items is None:
        return None
    return {task_id for item in items if (task_id := task_id_from_uid(item.get("uid"))) is not None}


async def complete_task(hass: HomeAssistant, task_id: int, completed_by: int | None = None,
                        context: Context | None = None) -> None:
    """``donetick.complete_task``. Raises ``HomeAssistantError``; completing a finished task is a no-op upstream."""
    data: dict[str, Any] = {"task_id": task_id}
    if completed_by is not None:
        data["completed_by"] = completed_by
    await hass.services.async_call("donetick", "complete_task", data, blocking=True, context=context)
