"""Service for writing independent print log entries.

Log entries are written to a separate table and never touch archives or queue items.
"""

import logging
from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.models.print_log import PrintLogEntry
from backend.app.models.print_queue import PrintQueueItem

logger = logging.getLogger(__name__)


def wear_cost_for_run(duration_seconds: int | None, wear_cost_per_hour: float | None) -> float | None:
    """Printer wear for one run: its duration at the printer's hourly rate (#694).

    None when the printer has no rate or the run has no measured duration (a
    reconciled run stores 0 because its real end time is unknown).
    """
    if not isinstance(wear_cost_per_hour, int | float) or not isinstance(duration_seconds, int):
        return None
    if wear_cost_per_hour <= 0 or duration_seconds <= 0:
        return None
    return round(duration_seconds / 3600 * wear_cost_per_hour, 3)


async def write_log_entry(
    db: AsyncSession,
    *,
    status: str,
    archive_id: int | None = None,
    queue_item_id: int | None = None,
    print_name: str | None = None,
    printer_name: str | None = None,
    printer_id: int | None = None,
    started_at: datetime | None = None,
    completed_at: datetime | None = None,
    filament_type: str | None = None,
    filament_color: str | None = None,
    filament_used_grams: float | None = None,
    cost: float | None = None,
    energy_kwh: float | None = None,
    energy_cost: float | None = None,
    wear_cost_per_hour: float | None = None,
    failure_reason: str | None = None,
    thumbnail_path: str | None = None,
    created_by_id: int | None = None,
    created_by_username: str | None = None,
    reconciled: bool = False,
) -> PrintLogEntry:
    """Write a print log entry.

    ``reconciled`` marks a synthetic completion written when a stale
    ``status="printing"`` archive is closed out at reconnect. Its real end time
    is unknown — the print stopped somewhere during the disconnect and
    ``completed_at`` is only the reconnect moment — so ``completed_at -
    started_at`` would bank the entire disconnect gap as print time, adding
    hundreds of fictitious hours across a farm of stale rows (#2592). For those
    entries we store an explicit ``0`` ("no measured runtime") rather than a
    fabricated duration; the stats total trusts a stored 0 instead of
    recomputing from the stale timestamps.
    """
    # The run's own running number, read from the queue row that produced it
    # (#2603). Read here rather than back off the archive at display time: a
    # quantity>1 order and every reprint share one archive row, and the archive
    # can only carry whichever number printed first — so the log would file
    # every later run under the first one's number. Resolved from
    # ``queue_item_id`` rather than passed in, so no caller can forget it; the
    # queue row is still there at this point, it is deleted later.
    job_number: str | None = None
    if queue_item_id is not None:
        job_number = (
            await db.execute(select(PrintQueueItem.job_number).where(PrintQueueItem.id == queue_item_id))
        ).scalar_one_or_none()

    if reconciled:
        duration: int | None = 0
    elif started_at and completed_at:
        duration = int((completed_at - started_at).total_seconds())
    else:
        duration = None

    entry = PrintLogEntry(
        archive_id=archive_id,
        queue_item_id=queue_item_id,
        job_number=job_number,
        print_name=print_name,
        printer_name=printer_name,
        printer_id=printer_id,
        status=status,
        started_at=started_at,
        completed_at=completed_at,
        duration_seconds=duration,
        filament_type=filament_type,
        filament_color=filament_color,
        filament_used_grams=filament_used_grams,
        cost=cost,
        energy_kwh=energy_kwh,
        energy_cost=energy_cost,
        wear_cost=wear_cost_for_run(duration, wear_cost_per_hour),
        failure_reason=failure_reason,
        thumbnail_path=thumbnail_path,
        created_by_id=created_by_id,
        created_by_username=created_by_username,
    )
    db.add(entry)
    await db.flush()
    return entry


async def record_archive_wear(db: AsyncSession, archive, entry: PrintLogEntry) -> None:
    """Copy a run's wear cost onto its archive when it is the archive's first run.

    Same rule as energy (#1378): the archive shows its first print, and a
    reprint's wear stays on its own log entry (#694). ``entry`` must already be
    flushed, so it counts as one of the archive's runs.
    """
    if entry.wear_cost is None:
        return
    runs = await db.scalar(select(func.count(PrintLogEntry.id)).where(PrintLogEntry.archive_id == archive.id))
    if (runs or 0) <= 1:
        archive.wear_cost = entry.wear_cost
