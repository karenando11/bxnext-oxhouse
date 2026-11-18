"""Small helpers for loading many rows efficiently."""

from __future__ import annotations

from typing import Any

BATCH_SIZE = 500


def upsert(
    model: type[Any],
    existing: dict[Any, Any],
    wanted: dict[Any, dict[str, Any]],
    create_with: dict[Any, dict[str, Any]],
) -> tuple[int, int]:
    """Create the missing objects and update the existing ones, in bulk.

    ``existing`` maps a key to a stored object, ``wanted`` maps a key to the values it should have,
    and ``create_with`` supplies the identifying values for a new object.
    Returns (created, updated).
    """
    to_create, to_update = [], []
    update_fields: set[str] = set()
    for key, values in wanted.items():
        obj = existing.get(key)
        if obj is None:
            to_create.append(model(**create_with[key], **values))
            continue
        for name, value in values.items():
            setattr(obj, name, value)
        update_fields.update(values)
        to_update.append(obj)
    if to_create:
        model.objects.bulk_create(to_create, batch_size=BATCH_SIZE)
    if to_update:
        model.objects.bulk_update(to_update, sorted(update_fields), batch_size=BATCH_SIZE)
    return len(to_create), len(to_update)
