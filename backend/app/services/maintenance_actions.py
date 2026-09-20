"""Actionable maintenance: calibration runs Bambuddy performs itself (#3127).

A maintenance type can carry an ``action`` the printer performs on its own
once told to: ``calibration`` (bed levelling, vibration compensation, motor
noise, and the model-specific extras, chosen per item) and
``motion_precision`` (the H2 series' vision encoder calibration, which has no
options). Each printer item of such a type has a trigger mode:

* ``manual`` -- only the "Run now" button queues a run;
* ``when_due`` -- the item falling due queues one;
* ``schedule`` -- weekdays plus an earliest time of day queue one.

A queued run is a :class:`MaintenanceRun` row. ``PrintScheduler`` dispatches it
on its regular pass once the printer is idle -- honouring the plate-clear gate,
because levelling with parts on the plate is a crash -- and the printer's own
completion event closes it here, marking the item performed exactly as the
"Reset" button would.

This module holds everything that is not the dispatch loop itself: the flag
vocabulary, schedule arithmetic, due-state calculation shared with the routes,
run creation, and the completion handler ``on_print_complete`` hands internal
jobs to.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from backend.app.core.database import async_session
from backend.app.models.maintenance import MaintenanceHistory, MaintenanceRun, MaintenanceType, PrinterMaintenance
from backend.app.models.printer import Printer
from backend.app.utils.local_time import local_zone, utcnow_naive
from backend.app.utils.print_jobs import action_for_job
from backend.app.utils.printer_models import has_vision_encoder, is_dual_nozzle_model

logger = logging.getLogger(__name__)

ACTION_CALIBRATION = "calibration"
ACTION_MOTION_PRECISION = "motion_precision"

# Keyword names of BambuMQTTClient.start_calibration, in bit order.
CALIBRATION_FLAGS: tuple[str, ...] = (
    "micro_lidar",
    "bed_leveling",
    "vibration",
    "motor_noise",
    "nozzle_offset",
    "high_temp_heatbed",
    "nozzle_clumping",
)
DEFAULT_CALIBRATION_OPTIONS: dict[str, bool] = {"bed_leveling": True, "vibration": True, "motor_noise": True}

TRIGGER_MODES: tuple[str, ...] = ("manual", "when_due", "schedule")
RUN_SOURCES: tuple[str, ...] = ("manual", "due", "schedule")
RUN_ACTIVE_STATUSES: tuple[str, ...] = ("pending", "running")
RUN_TERMINAL_STATUSES: tuple[str, ...] = ("completed", "failed", "cancelled")

# print_error the firmware reports when a calibration is cancelled from the
# printer screen: 0x0300400C, "The task was canceled." (H2S capture).
CALIBRATION_CANCELLED_PRINT_ERROR = 50348044

# The cancel code does not travel with the FAILED edge. In the H2S capture the
# first FAILED report still says print_error 0 and the 0300_400C echo follows
# six to eight seconds later, by which time ``on_print_complete`` has long
# fired. A FAILED calibration without a code is therefore held open for this
# long before it is judged, and the echo the MQTT client stamped in the
# meantime (``PrinterState.last_cancel_echo_at``) decides cancelled vs failed.
# An echo up to this many seconds *before* the edge counts too, in case other
# firmware orders the two the other way round.
CANCEL_ECHO_GRACE_SECONDS = 15.0
CANCEL_ECHO_LOOKBACK_SECONDS = 60.0

# A calibration takes minutes. A run still "running" this long after dispatch
# has lost its completion event -- a restart mid-run with the printer offline
# since, say -- and is closed as failed so the item is not blocked forever.
STALE_RUNNING_AFTER = timedelta(hours=2)

# Window after dispatch during which the printer is treated as busy by the
# print queue even though it may still report IDLE: the same command-to-RUNNING
# lag the queue guards against with its own "printing" seed.
DISPATCH_BUSY_WINDOW = timedelta(seconds=120)

AUTO_RUN_NOTES = "Automatic calibration"

# The vision encoder calibration is started as a system gcode file. The
# directory under /usr/etc/print/ is model-specific and the printer reports
# it with every internal job it runs (``PrinterState.internal_gcode_dir``);
# this map is the fallback for a printer that has never reported one. Only
# the H2S entry is verified on hardware (H2S capture, #3127).
MOTION_PRECISION_GCODE_NAME = "calibrate_motion_precision.gcode"
INTERNAL_GCODE_DIR_BY_MODEL: dict[str, str] = {
    "H2S": "O1S",
    "O1S": "O1S",
    "H2D": "O1D",
    "O1D": "O1D",
    "H2DPRO": "O1D",
    "O1E": "O1D",
    "O2D": "O1D",
    "H2C": "O1C",
    "O1C": "O1C",
    "O1C2": "O1C",
}


# ============== Options ==============


def normalize_calibration_options(options: dict | None) -> dict[str, bool]:
    """Every flag as a bool; None (never configured) means the defaults."""
    if not isinstance(options, dict):
        options = DEFAULT_CALIBRATION_OPTIONS
    return {flag: bool(options.get(flag, False)) for flag in CALIBRATION_FLAGS}


def selected_calibration_flags(options: dict | None) -> list[str]:
    return [flag for flag, on in normalize_calibration_options(options).items() if on]


def has_options(action: str | None) -> bool:
    """Does this action carry a per-item option set?"""
    return action == ACTION_CALIBRATION


def run_options(action: str | None, options: dict | None) -> dict[str, bool] | None:
    """The option set a run of ``action`` is dispatched with.

    Raises ValueError when the action has options and none is selected --
    the firmware would refuse the command, so the run must not be queued.
    Actions without options get None.
    """
    if not has_options(action):
        return None
    normalized = normalize_calibration_options(options)
    if not any(normalized.values()):
        raise ValueError("No calibration option selected")
    return normalized


def available_calibration_options(printer_model: str | None) -> list[str]:
    """Flags the card should offer for this model.

    Nozzle offset only exists on dual-nozzle printers. The other flags are
    offered everywhere: the firmware ignores bits the machine has no hardware
    for, and there is no model table for the Micro Lidar or the
    high-temperature bed to hide them by.
    """
    flags = list(CALIBRATION_FLAGS)
    if not is_dual_nozzle_model(printer_model):
        flags.remove("nozzle_offset")
    return flags


def motion_precision_gcode_path(printer_model: str | None, learned_dir: str | None) -> str | None:
    """Path of the vision encoder calibration for this printer, or None.

    The directory the printer last reported for one of its own jobs wins;
    otherwise the model map, with a warning because that entry may be a guess.
    None means the model has no vision encoder and the run must be refused.
    """
    if not has_vision_encoder(printer_model):
        return None
    if learned_dir:
        return f"/usr/etc/print/{learned_dir}/{MOTION_PRECISION_GCODE_NAME}"
    normalized = (printer_model or "").strip().upper().replace(" ", "").replace("-", "")
    fallback = INTERNAL_GCODE_DIR_BY_MODEL.get(normalized)
    if fallback is None:
        return None
    logger.warning(
        "Printer model %s has not reported its internal gcode directory yet; assuming /usr/etc/print/%s/",
        printer_model,
        fallback,
    )
    return f"/usr/etc/print/{fallback}/{MOTION_PRECISION_GCODE_NAME}"


# ============== Schedule ==============


def parse_schedule_time(time_str: str | None) -> tuple[int, int] | None:
    """ "HH:MM" -> (hour, minute), or None when it is not one."""
    if not time_str:
        return None
    parts = time_str.strip().split(":")
    if len(parts) != 2:
        return None
    try:
        hour, minute = int(parts[0]), int(parts[1])
    except ValueError:
        return None
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        return None
    return hour, minute


def compute_schedule_next_at(
    days: list[int] | None,
    time_str: str | None,
    *,
    now: datetime | None = None,
) -> datetime | None:
    """Next occurrence of ``time_str`` on one of ``days``, as naive UTC.

    Days are weekday ints, 0 = Monday, and the time of day is read in the
    container's local zone like the local backup schedule. The candidate is
    built on the local wall clock and converted afterwards, so it stays at the
    configured hour across a DST change; ``fold=0`` picks the earlier of an
    ambiguous fall-back hour. Strictly after ``now``, compared in UTC: a time
    equal to now has already been queued by the pass that saw it, and a
    wall-clock comparison would ignore ``fold`` and call the first 02:30 of a
    fall-back night "later" than a ``now`` in the repeated hour, queueing the
    same slot twice.
    """
    parsed = parse_schedule_time(time_str)
    weekdays = {int(d) for d in (days or []) if 0 <= int(d) <= 6}
    if parsed is None or not weekdays:
        return None
    hour, minute = parsed

    now_utc = now if now is not None else datetime.now(timezone.utc)
    if now_utc.tzinfo is None:
        now_utc = now_utc.replace(tzinfo=timezone.utc)
    tz = local_zone()
    now_local = now_utc.astimezone(tz)

    for offset in range(8):
        candidate = (now_local + timedelta(days=offset)).replace(
            hour=hour, minute=minute, second=0, microsecond=0, fold=0
        )
        candidate_utc = candidate.astimezone(timezone.utc)
        if candidate.weekday() in weekdays and candidate_utc > now_utc:
            return candidate_utc.replace(tzinfo=None)
    return None


def refresh_schedule(item: PrinterMaintenance, *, now: datetime | None = None) -> None:
    """Recompute ``schedule_next_at`` from the item's schedule settings."""
    if item.trigger_mode == "schedule" and item.enabled:
        item.schedule_next_at = compute_schedule_next_at(item.schedule_days, item.schedule_time, now=now)
    else:
        item.schedule_next_at = None


# ============== Due state ==============


@dataclass(frozen=True)
class DueState:
    hours_since: float
    hours_until: float
    days_since: float | None
    days_until: float | None
    is_due: bool
    is_warning: bool


def compute_due_state(
    interval: float,
    interval_type: str,
    total_hours: float,
    last_performed_hours: float,
    last_performed_at: datetime | None,
    now: datetime,
) -> DueState:
    """The item's position in its interval, for the overview and the triggers.

    ``days`` intervals count calendar days since the last reset (never reset =
    due); ``hours`` intervals count print hours since it. Warning = within the
    last 10% of the interval.
    """
    if last_performed_at is not None and last_performed_at.tzinfo is None:
        # DB stores naive datetimes; treat as UTC for comparison
        last_performed_at = last_performed_at.replace(tzinfo=timezone.utc)
    days_since_performed = (now - last_performed_at).total_seconds() / 86400.0 if last_performed_at else None
    hours_since = total_hours - last_performed_hours

    if interval_type == "days":
        days_since = days_since_performed if days_since_performed is not None else interval + 1
        days_until = interval - days_since
        is_due = days_until <= 0
        is_warning = days_until <= (interval * 0.1) and not is_due
        return DueState(hours_since, 0, days_since, days_until, is_due, is_warning)

    hours_until = interval - hours_since
    is_due = hours_until <= 0
    is_warning = hours_until <= (interval * 0.1) and not is_due
    return DueState(hours_since, hours_until, None, None, is_due, is_warning)


async def get_printer_total_hours(db: AsyncSession, printer_id: int) -> float:
    """Calculate total active hours for a printer from runtime counter plus offset.

    Uses the runtime_seconds counter which tracks actual machine active time
    (RUNNING state only — paused time is excluded since maintenance intervals
    measure mechanical wear, not wall-clock active time, see #1521).
    """
    result = await db.execute(
        select(Printer.runtime_seconds, Printer.print_hours_offset).where(Printer.id == printer_id)
    )
    row = result.one_or_none()
    if not row:
        return 0.0

    runtime_seconds = row[0] or 0
    offset = row[1] or 0.0
    return runtime_seconds / 3600.0 + offset


# ============== Runs ==============


async def get_active_run(db: AsyncSession, item_id: int) -> MaintenanceRun | None:
    result = await db.execute(
        select(MaintenanceRun)
        .where(MaintenanceRun.printer_maintenance_id == item_id)
        .where(MaintenanceRun.status.in_(RUN_ACTIVE_STATUSES))
        .order_by(MaintenanceRun.id.desc())
        .limit(1)
    )
    return result.scalar_one_or_none()


async def create_run(
    db: AsyncSession,
    item: PrinterMaintenance,
    source: str,
    *,
    start_after: datetime | None = None,
) -> MaintenanceRun:
    """Queue a run for ``item``. Flushed, not committed; the caller commits.

    Raises ValueError when the item's action has options and none is
    selected -- the firmware would refuse the command, so the run must not
    be queued. ``item.maintenance_type`` must be loaded.
    """
    options = run_options(item.maintenance_type.action, item.action_options)
    run = MaintenanceRun(
        printer_maintenance_id=item.id,
        printer_id=item.printer_id,
        status="pending",
        source=source,
        options=options,
        start_after=start_after,
    )
    db.add(run)
    await db.flush()
    return run


async def _latest_run(db: AsyncSession, item_id: int) -> MaintenanceRun | None:
    result = await db.execute(
        select(MaintenanceRun)
        .where(MaintenanceRun.printer_maintenance_id == item_id)
        .order_by(MaintenanceRun.id.desc())
        .limit(1)
    )
    return result.scalar_one_or_none()


async def _actionable_items(db: AsyncSession) -> list[PrinterMaintenance]:
    result = await db.execute(
        select(PrinterMaintenance)
        .join(PrinterMaintenance.maintenance_type)
        .join(PrinterMaintenance.printer)
        .where(MaintenanceType.action.is_not(None))
        .where(MaintenanceType.is_deleted.is_(False))
        .where(PrinterMaintenance.enabled.is_(True))
        .where(PrinterMaintenance.trigger_mode.in_(("when_due", "schedule")))
        .where(Printer.is_active.is_(True))
        .options(selectinload(PrinterMaintenance.maintenance_type))
        .order_by(PrinterMaintenance.id)
    )
    return list(result.scalars().all())


async def queue_triggered_runs(db: AsyncSession, now: datetime | None = None) -> list[MaintenanceRun]:
    """Create the runs the ``when_due`` and ``schedule`` triggers call for.

    Flushed, not committed. One active run per item: an item with a pending or
    running run gets nothing new. A due item whose latest run was already
    queued for this due period (created after the last reset) is left alone
    too -- a failed or cancelled run must not be retried on every pass, and a
    completed one resets the item so it stops being due anyway.
    """
    now = now or utcnow_naive()
    now_aware = now.replace(tzinfo=timezone.utc)
    created: list[MaintenanceRun] = []
    total_hours_cache: dict[int, float] = {}

    for item in await _actionable_items(db):
        if await get_active_run(db, item.id) is not None:
            continue

        if item.trigger_mode == "when_due":
            if item.printer_id not in total_hours_cache:
                total_hours_cache[item.printer_id] = await get_printer_total_hours(db, item.printer_id)
            maint_type = item.maintenance_type
            interval = item.custom_interval_hours or maint_type.default_interval_hours
            interval_type = item.custom_interval_type or maint_type.interval_type or "hours"
            due = compute_due_state(
                interval,
                interval_type,
                total_hours_cache[item.printer_id],
                item.last_performed_hours or 0.0,
                item.last_performed_at,
                now_aware,
            )
            if not due.is_due:
                continue
            latest = await _latest_run(db, item.id)
            if latest is not None and (
                item.last_performed_at is None or (latest.created_at or now) >= item.last_performed_at
            ):
                continue
            try:
                created.append(await create_run(db, item, "due"))
            except ValueError:
                logger.warning("Maintenance item %d is due but has no calibration option selected", item.id)
            continue

        # schedule
        if item.schedule_next_at is None:
            refresh_schedule(item, now=now_aware)
        if item.schedule_next_at is None or item.schedule_next_at > now:
            continue
        start_after = item.schedule_next_at
        item.schedule_next_at = compute_schedule_next_at(item.schedule_days, item.schedule_time, now=now_aware)
        try:
            created.append(await create_run(db, item, "schedule", start_after=start_after))
        except ValueError:
            logger.warning("Maintenance item %d is scheduled but has no calibration option selected", item.id)

    return created


async def fail_stale_running_runs(db: AsyncSession, now: datetime | None = None) -> int:
    """Close runs that have been "running" longer than any calibration takes.

    Covers the restart case: a run dispatched before Bambuddy went down whose
    completion event was never seen. Flushed, not committed.
    """
    now = now or utcnow_naive()
    cutoff = now - STALE_RUNNING_AFTER
    result = await db.execute(
        select(MaintenanceRun).where(MaintenanceRun.status == "running").where(MaintenanceRun.started_at < cutoff)
    )
    rows = list(result.scalars().all())
    for run in rows:
        run.status = "failed"
        run.error_message = "Lost track of the run: no completion was reported"
        run.completed_at = now
        logger.warning(
            "Maintenance run %d on printer %d never reported completion; marked failed", run.id, run.printer_id
        )
    return len(rows)


# ============== Completion ==============


async def record_performed(
    db: AsyncSession,
    item: PrinterMaintenance,
    notes: str | None,
    *,
    now: datetime | None = None,
) -> MaintenanceHistory:
    """Mark ``item`` performed now: history row plus counter reset.

    Shared by the "Reset" button and the automatic completion so the two
    cannot drift. Flushed, not committed.
    """
    current_hours = await get_printer_total_hours(db, item.printer_id)
    history = MaintenanceHistory(
        printer_maintenance_id=item.id,
        hours_at_maintenance=current_hours,
        notes=notes,
    )
    db.add(history)
    item.last_performed_at = now or utcnow_naive()
    item.last_performed_hours = current_hours
    await db.flush()
    return history


async def _publish_reset(printer_id: int, printer_name: str, type_name: str) -> None:
    try:
        from backend.app.services.mqtt_relay import mqtt_relay

        await mqtt_relay.on_maintenance_reset(
            printer_id=printer_id, printer_name=printer_name, maintenance_type=type_name
        )
    except Exception:
        pass  # Don't fail if MQTT fails


def resolve_run_outcome(final_status: str | None, print_error: int | None) -> str:
    """Terminal run status for a calibration that ended with ``final_status``.

    FINISH is ``completed``. A cancel from the printer screen reports FAILED
    with the "task was canceled" print_error, and an abort to IDLE reports
    ``aborted``; both are ``cancelled``, not a failure of the machine.
    """
    if final_status == "completed":
        return "completed"
    if final_status in ("aborted", "cancelled") or print_error == CALIBRATION_CANCELLED_PRINT_ERROR:
        return "cancelled"
    return "failed"


async def on_internal_job_finished(
    printer_id: int,
    filename: str | None,
    subtask_name: str | None,
    final_status: str | None,
    print_error: int | None = None,
) -> bool:
    """Close the running maintenance run the printer's job belonged to.

    Called from ``on_print_complete`` for every internal printer job. Only a
    run whose action matches the finished job is closed -- a bed-levelling
    run is not over because the vision encoder calibration finished. Returns
    True when a run was closed; False when the job belongs to no action or
    nothing was waiting for it, which is also every calibration started by
    hand from the screen.
    """
    action = action_for_job(filename, subtask_name)
    if action is None:
        return False

    async with async_session() as db:
        result = await db.execute(
            select(MaintenanceRun)
            .join(MaintenanceRun.printer_maintenance)
            .join(PrinterMaintenance.maintenance_type)
            .where(MaintenanceRun.printer_id == printer_id)
            .where(MaintenanceRun.status == "running")
            .where(MaintenanceType.action == action)
            .options(
                selectinload(MaintenanceRun.printer_maintenance).selectinload(PrinterMaintenance.maintenance_type),
                selectinload(MaintenanceRun.printer),
            )
            .order_by(MaintenanceRun.started_at.desc().nullslast(), MaintenanceRun.id.desc())
            .limit(1)
        )
        run = result.scalar_one_or_none()
        if run is None:
            return False

        now = utcnow_naive()
        outcome = resolve_run_outcome(final_status, print_error)
        run.status = outcome
        run.completed_at = now
        run.waiting_reason = None
        item = run.printer_maintenance

        if outcome == "completed":
            await record_performed(db, item, AUTO_RUN_NOTES, now=now)
            item.last_auto_run_at = now
        elif outcome == "failed":
            run.error_message = (
                f"Calibration failed (print_error {print_error})" if print_error else "Calibration failed"
            )
        refresh_schedule(item, now=now.replace(tzinfo=timezone.utc))
        await db.commit()

        logger.info(
            "Maintenance run %d on printer %d finished as %s (status=%s, print_error=%s)",
            run.id,
            printer_id,
            outcome,
            final_status,
            print_error,
        )
        if outcome == "completed":
            await _publish_reset(printer_id, run.printer.name, item.maintenance_type.name)
        return True


def cancel_echo_seen(printer_id: int, failed_at: float, *, now: float | None = None) -> bool:
    """Did the printer echo a user cancel around the FAILED edge at ``failed_at``?

    ``failed_at`` is a ``time.monotonic()`` stamp; the MQTT client records the
    echo on the same clock. Anything from ``CANCEL_ECHO_LOOKBACK_SECONDS``
    before the edge up to now counts.
    """
    from backend.app.services.printer_manager import printer_manager

    state = printer_manager.get_status(printer_id)
    echo_at = getattr(state, "last_cancel_echo_at", None) if state else None
    if echo_at is None:
        return False
    now = time.monotonic() if now is None else now
    return failed_at - CANCEL_ECHO_LOOKBACK_SECONDS <= echo_at <= now


async def on_internal_job_failed(
    printer_id: int,
    filename: str | None,
    subtask_name: str | None,
    failed_at: float,
) -> bool:
    """Close a calibration that reported FAILED without a print_error.

    Waits ``CANCEL_ECHO_GRACE_SECONDS`` for the cancel echo the firmware sends
    after the edge, then closes the run as cancelled when it came and as
    failed when it did not. Meant to run as a background task from
    ``on_print_complete``; a Cancel pressed in the UI meanwhile closes the row
    first and this then finds nothing to do.
    """
    if action_for_job(filename, subtask_name) is None:
        return False
    await asyncio.sleep(CANCEL_ECHO_GRACE_SECONDS)
    cancelled = cancel_echo_seen(printer_id, failed_at)
    logger.info(
        "Calibration on printer %d reported FAILED without a code; cancel echo %s",
        printer_id,
        "seen, closing as cancelled" if cancelled else "not seen, closing as failed",
    )
    return await on_internal_job_finished(
        printer_id,
        filename,
        subtask_name,
        "failed",
        CALIBRATION_CANCELLED_PRINT_ERROR if cancelled else None,
    )
