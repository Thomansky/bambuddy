"""Keeping the library in step with the directory it lives in (#3160).

In managed mode the database and the disk cannot disagree: nothing but Bambuddy
writes there. Point the library at a share and that guarantee is gone — the
whole reason for the mode is that somebody works in Explorer — so *Scan* stops
being the one-off it is for an external folder and becomes the thing that keeps
the library true.

Doing it by hand, per folder, is the wrong shape for that. This runs the same
reconciliation the Scan button runs, across the tree's top-level folders, on an
interval the owner sets. Off by default, because a walk of a mounted share is
real network IO and nobody should pay for it without asking.
"""

from __future__ import annotations

import asyncio
import logging
import time

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.models.library import LibraryFolder
from backend.app.services.library_storage import (
    configured_storage_root,
    is_inside_tree,
    storage_path_problem,
)

logger = logging.getLogger(__name__)

SETTING_INTERVAL = "library_autoscan_minutes"

# How often the loop wakes to look at the clock. The interval itself is the
# setting; this only decides how soon a change to it takes effect.
_TICK_SECONDS = 60


async def autoscan_once(db: AsyncSession) -> dict:
    """Reconcile every top-level folder of the library's tree. Returns a summary.

    ``skipped`` says why nothing happened, which is the answer to both "the
    library is not in a directory" and "the share is not there right now" — the
    second must not be an error, or a NAS rebooting would fill the log with
    tracebacks every minute.
    """
    root = await configured_storage_root(db)
    if root is None:
        return {"scanned": 0, "added": 0, "removed": 0, "skipped": "not a directory library"}

    problem = storage_path_problem(str(root))
    if problem:
        logger.info("Library auto-scan skipped: the storage path %s", problem)
        return {"scanned": 0, "added": 0, "removed": 0, "skipped": problem}

    # A share that dropped off leaves the bind-mount target behind as an empty
    # directory, and a scan of "everything is gone" removes every row -- taking
    # the tags, the project links and the print history with them while the
    # files sit untouched on a NAS nobody can reach. An empty tree is therefore
    # never reconciled; it is reported and left alone.
    try:
        if not any(root.iterdir()):
            logger.warning("Library auto-scan skipped: %s is empty — is the share still mounted?", root)
            return {"scanned": 0, "added": 0, "removed": 0, "skipped": "the library directory is empty"}
    except OSError as exc:
        logger.warning("Library auto-scan skipped: %s could not be listed: %s", root, exc)
        return {"scanned": 0, "added": 0, "removed": 0, "skipped": f"the library directory could not be read: {exc}"}

    from backend.app.api.routes.library import scan_external_folder

    folders = (
        (
            await db.execute(
                select(LibraryFolder).where(
                    LibraryFolder.parent_id.is_(None),
                    LibraryFolder.is_external.is_(True),
                )
            )
        )
        .scalars()
        .all()
    )
    # Only the library's own tree. A folder mounted from somewhere else is
    # somebody's separate decision and keeps its manual button.
    targets = [f for f in folders if is_inside_tree(root, f.external_path)]

    added = removed = 0
    for folder in targets:
        try:
            # The route function, not a copy of it: two implementations of
            # "reconcile this folder" would drift, and this one is the tested one.
            result = await scan_external_folder(folder_id=folder.id, db=db, _=None)
        except Exception as exc:  # noqa: BLE001 - one bad folder must not stop the rest
            logger.warning("Library auto-scan failed for folder %s: %s", folder.id, exc)
            continue
        added += result.get("added", 0)
        removed += result.get("removed", 0)

    if added or removed:
        logger.info(
            "Library auto-scan: %d folder(s), %d file(s) added, %d removed",
            len(targets),
            added,
            removed,
        )
    return {"scanned": len(targets), "added": added, "removed": removed, "skipped": None}


async def _interval_minutes(db: AsyncSession) -> int:
    from backend.app.api.routes.settings import get_setting

    raw = await get_setting(db, SETTING_INTERVAL)
    try:
        return max(0, int(raw))
    except (TypeError, ValueError):
        return 0


async def _loop() -> None:
    from backend.app.core.database import async_session

    last_run = 0.0
    while True:
        try:
            async with async_session() as db:
                minutes = await _interval_minutes(db)
                if minutes > 0 and time.monotonic() - last_run >= minutes * 60:
                    last_run = time.monotonic()
                    await autoscan_once(db)
        except asyncio.CancelledError:
            break
        except Exception as exc:  # noqa: BLE001 - the loop outlives its failures
            logger.warning("Library auto-scan loop error: %s", exc)
        await asyncio.sleep(_TICK_SECONDS)


_task: asyncio.Task | None = None


def start_library_autoscan() -> None:
    """Start the loop. Harmless when the setting is off — it only reads a row."""
    global _task
    if _task is None:
        _task = asyncio.create_task(_loop())
        logger.info("Library auto-scan loop started")


def stop_library_autoscan() -> None:
    global _task
    if _task is not None:
        _task.cancel()
        _task = None
