"""Operator-owned additive plugin and skill installs for individual Managers."""

from typing import Any

from ..db import manager_installs as db
from ..db.connection import get_pool
from .catalog import CatalogConflict, CatalogError


def _id(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise CatalogError(f"{name} must be a nonempty string")
    return value


async def _view(manager_id: str, conn: Any) -> dict[str, Any]:
    rows = [(row, True) for row in await db.inherited(manager_id, conn=conn)]
    rows += [(row, False) for row in await db.list_installs(manager_id, conn=conn)]
    return {"manager_id": manager_id, "installs": [
        {"item_id": row["item_id"], "kind": row["kind"], "name": row["name"], "enabled": row["enabled"],
         "inherited": inherited, "updated_at": row["updated_at"]} for row, inherited in rows]}


async def list_installs(manager_id: str) -> dict[str, Any]:
    manager_id = _id(manager_id, "manager_id")
    async with get_pool().acquire() as conn, conn.transaction():
        return await _view(manager_id, conn)


async def put_install(manager_id: str, item_id: str, enabled: bool = True) -> dict[str, Any]:
    manager_id, item_id = _id(manager_id, "manager_id"), _id(item_id, "item_id")
    async with get_pool().acquire() as conn, conn.transaction():
        if enabled is False and not any(row["item_id"] == item_id
                                        for row in await db.list_installs(manager_id, conn=conn)):
            if any(row["item_id"] == item_id for row in await db.inherited(manager_id, conn=conn)):
                raise CatalogConflict("an inherited global install cannot be disabled for one Manager; add an explicit install first")
        await db.put_install(manager_id, item_id, enabled, conn=conn)
        return await _view(manager_id, conn)


async def delete_install(manager_id: str, item_id: str) -> dict[str, Any]:
    manager_id, item_id = _id(manager_id, "manager_id"), _id(item_id, "item_id")
    async with get_pool().acquire() as conn, conn.transaction():
        await db.delete_install(manager_id, item_id, conn=conn)
        return await _view(manager_id, conn)
