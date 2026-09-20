"""Printer depreciation — wear cost per printing hour (#694).

A printer with ``purchase_price`` and ``expected_lifetime_hours`` set charges
``purchase_price / expected_lifetime_hours`` for every hour it runs. The value
is a point-in-time snapshot taken at completion from the printer row, so a
later price edit changes future runs only; nothing here ever recalculates.
"""

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.models.archive import PrintArchive
from backend.app.models.print_log import PrintLogEntry
from backend.app.models.printer import Printer

# Matches the precision the energy path stores (energy_cost is rounded to 3).
_COST_DECIMALS = 3


def hourly_depreciation_rate(purchase_price: float | None, expected_lifetime_hours: float | None) -> float | None:
    """The wear rate per printing hour, or None when the feature is off.

    Either input missing or non-positive disables it: a zero lifetime would
    divide by zero, and a zero price is indistinguishable from "not set".
    """
    if not purchase_price or not expected_lifetime_hours:
        return None
    if purchase_price <= 0 or expected_lifetime_hours <= 0:
        return None
    return purchase_price / expected_lifetime_hours


def run_depreciation_cost(
    duration_seconds: int | None,
    purchase_price: float | None,
    expected_lifetime_hours: float | None,
) -> float | None:
    """Wear cost of one run: measured hours × hourly rate.

    None when the printer has no rate or the run has no measured duration.
    A stored ``0`` duration (reconciled completion, #2592) yields ``0.0``,
    consistent with the zero print time it already banks.
    """
    rate = hourly_depreciation_rate(purchase_price, expected_lifetime_hours)
    if rate is None or duration_seconds is None:
        return None
    return round(duration_seconds / 3600 * rate, _COST_DECIMALS)


async def snapshot_run_depreciation(
    db: AsyncSession,
    archive: PrintArchive,
    printer_id: int | None,
    duration_seconds: int | None,
) -> float | None:
    """Compute this run's wear and store the first run's value on the archive.

    Returns the per-run value for the caller to hand to ``write_log_entry``.
    The archive copy follows the ``cost`` / ``energy_cost`` convention (#1378):
    written only when no PrintLogEntry exists for the archive yet — i.e. this
    is its first run — so a reprint never overwrites the source archive's
    figure. Must run BEFORE the run's own log entry is written.
    """
    if printer_id is None:
        return None
    printer = await db.get(Printer, printer_id)
    if printer is None:
        return None
    cost = run_depreciation_cost(duration_seconds, printer.purchase_price, printer.expected_lifetime_hours)
    if cost is None:
        return None
    existing_runs = await db.scalar(select(func.count(PrintLogEntry.id)).where(PrintLogEntry.archive_id == archive.id))
    if not existing_runs:
        archive.depreciation_cost = cost
    return cost
