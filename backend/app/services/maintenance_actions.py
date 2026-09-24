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
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from backend.app.core.database import async_session
from backend.app.models.maintenance import MaintenanceHistory, MaintenanceRun, MaintenanceType, PrinterMaintenance
from backend.app.models.printer import Printer
from backend.app.utils.local_time import local_zone, utcnow_naive
from backend.app.utils.print_jobs import action_for_job
from backend.app.utils.printer_models import has_micro_lidar, has_vision_encoder, is_dual_nozzle_model

logger = logging.getLogger(__name__)

ACTION_CALIBRATION = "calibration"
ACTION_MOTION_PRECISION = "motion_precision"
# Actions a type may carry. A custom type can be created with one of these
# so a second calibration item with its own interval and schedule can live
# next to the seeded one (#3127); the action is fixed at creation.
KNOWN_ACTIONS: tuple[str, ...] = (ACTION_CALIBRATION, ACTION_MOTION_PRECISION)

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

# Start condition (#3127): a run only starts once the bed is below this many
# degrees C. Both actions carry it, stored in ``action_options`` next to the
# calibration flags; absent = no condition. It is read from the item on
# every pass rather than frozen onto the run like the flags, so raising the
# threshold releases a run that is already waiting.
BED_TEMP_BELOW_KEY = "bed_temp_below"
BED_TEMP_BELOW_MAX = 120.0
WAIT_BED_TOO_WARM = "bed_too_warm"
WAIT_BED_TEMP_UNKNOWN = "bed_temp_unknown"

# Runs on one printer go out one at a time, in a fixed order (#3127): the
# levelling calibration before the vision encoder one -- it heats the bed,
# and the cold-bed condition then holds the vision encoder run back on its
# own -- then by start_after, then by id. Every run behind the head of that
# line waits with this reason and the head's item name in waiting_detail.
WAIT_AFTER_OTHER_RUN = "after_other_run"
ACTION_PRIORITY: dict[str, int] = {ACTION_CALIBRATION: 0, ACTION_MOTION_PRECISION: 1}

# The print queue keeps clear of a scheduled run (#3127): a job is only
# dispatched when it is expected to be done this long before the slot, and a
# job whose duration is unknown is held from UNKNOWN_DURATION_HOLD before it.
SCHEDULE_MARGIN = timedelta(minutes=15)
UNKNOWN_DURATION_HOLD = timedelta(hours=2)

# How the two maintenance holds start on a queue row. The scheduler's
# busy-only test and the frontend's parser both key on these exact strings,
# so a reword lands in all three places at once. On an "Any <model>" row the
# hold ends with the printers it is about, after QUEUE_HOLD_PRINTERS_JOINER.
QUEUE_HOLD_RUN_PREFIX = "Maintenance run pending: "
QUEUE_HOLD_SCHEDULE_PREFIX = "Scheduled maintenance at "
QUEUE_HOLD_PRINTERS_JOINER = " — "

# What a run's state reads as inside the queue hold, in English; the
# frontend maps these phrases back to its own translations.
QUEUE_HOLD_RUN_PHRASES: dict[str, str] = {
    "printer_offline": "printer offline",
    "printer_busy": "printer busy",
    "awaiting_plate_clear": "plate not released yet",
    "already_drying": "AMS drying in progress",
    WAIT_BED_TOO_WARM: "bed still warm",
    WAIT_BED_TEMP_UNKNOWN: "bed temperature unknown",
}
QUEUE_HOLD_RUN_QUEUED = "queued"
QUEUE_HOLD_RUN_RUNNING = "running"

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


def action_applies_to_printer(action: str | None, printer_model: str | None) -> bool:
    """Can this printer perform the action at all?

    The model gate a custom type with an action inherits from the seeded one
    (#3127): only the H2 series has the vision encoder, every printer can be
    told to level its bed. A reminder type (no action) applies everywhere.
    """
    if action == ACTION_MOTION_PRECISION:
        return has_vision_encoder(printer_model)
    return True


def normalize_bed_temp_below(value: object) -> float | None:
    """The validated threshold, or None for absent/null. Raises ValueError."""
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{BED_TEMP_BELOW_KEY} must be a number")
    threshold = float(value)
    if not (0 < threshold <= BED_TEMP_BELOW_MAX):
        raise ValueError(f"{BED_TEMP_BELOW_KEY} must be above 0 and at most {BED_TEMP_BELOW_MAX:.0f}")
    if round(threshold, 1) != threshold:
        raise ValueError(f"{BED_TEMP_BELOW_KEY} allows one decimal at most")
    return threshold


def bed_temp_below(options: dict | None) -> float | None:
    """The stored start condition; anything unusable reads as no condition."""
    if not isinstance(options, dict):
        return None
    try:
        return normalize_bed_temp_below(options.get(BED_TEMP_BELOW_KEY))
    except ValueError:
        return None


def stored_action_options(action: str | None, options: dict) -> dict:
    """What a PATCH stores for ``action`` from a validated option payload.

    The calibration flags are filled in for the action that has them; the
    start condition is kept when set and dropped when null or absent.
    """
    stored: dict = normalize_calibration_options(options) if has_options(action) else {}
    threshold = normalize_bed_temp_below(options.get(BED_TEMP_BELOW_KEY))
    if threshold is not None:
        stored[BED_TEMP_BELOW_KEY] = threshold
    return stored


def response_action_options(action: str | None, options: dict | None) -> dict | None:
    """The option set the overview reports: flags plus the start condition.

    None for an action without options and without a condition, so a card
    that has nothing to show gets nothing.
    """
    out: dict = normalize_calibration_options(options) if has_options(action) else {}
    threshold = bed_temp_below(options)
    if threshold is not None:
        out[BED_TEMP_BELOW_KEY] = threshold
    return out or None


def current_bed_temperature(state: object) -> float | None:
    """The bed temperature the printer last reported, or None when it has not.

    Same source as the bed-cooled notification: ``PrinterState.temperatures
    ["bed"]``, fed by ``bed_temper`` in every push_status.
    """
    temps = getattr(state, "temperatures", None)
    if not isinstance(temps, dict):
        return None
    value = temps.get("bed")
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def bed_condition_wait(threshold: float | None, bed_temp: float | None) -> tuple[str, dict | None] | None:
    """Why the bed condition holds a run back, as (reason, detail), or None.

    "Below" is strict: a bed at exactly the threshold is not below it.
    """
    if threshold is None:
        return None
    if bed_temp is None:
        return WAIT_BED_TEMP_UNKNOWN, None
    if bed_temp >= threshold:
        return WAIT_BED_TOO_WARM, {"bed_temp": round(bed_temp, 1), "threshold": threshold}
    return None


def set_waiting(run: MaintenanceRun, reason: str | None, detail: dict | None = None) -> None:
    """Record why ``run`` is still pending; reason and detail move together."""
    run.waiting_reason = reason
    run.waiting_detail = detail if reason is not None else None


# ============== Order on one printer ==============


def run_item_name(run: MaintenanceRun) -> str:
    """The stored type name of the run's item (``printer_maintenance.maintenance_type`` loaded)."""
    return run.printer_maintenance.maintenance_type.name


def run_order_key(run: MaintenanceRun) -> tuple[int, datetime, int]:
    """Where ``run`` stands in its printer's line; lower goes first.

    Action priority, then ``start_after`` (none first), then id. An action
    without a priority entry sorts last. ``run.printer_maintenance
    .maintenance_type`` must be loaded.
    """
    action = run.printer_maintenance.maintenance_type.action or ""
    return (ACTION_PRIORITY.get(action, len(ACTION_PRIORITY)), run.start_after or datetime.min, run.id)


def head_runs(runs: list[MaintenanceRun], now: datetime) -> dict[int, MaintenanceRun]:
    """The run at the head of each printer's line, by printer id.

    A running run heads its printer whatever its action: nothing else can go
    out while it is on the printer. Otherwise the first pending run whose
    ``start_after`` has passed, in :func:`run_order_key` order. A pending run
    whose ``start_after`` is still ahead heads nothing -- it could not be
    dispatched yet, so it neither holds the other runs nor the print queue.
    """
    heads: dict[int, MaintenanceRun] = {}
    for run in runs:
        if run.status == "running":
            heads.setdefault(run.printer_id, run)
    for run in sorted(runs, key=run_order_key):
        if run.status == "pending" and (run.start_after is None or run.start_after <= now):
            heads.setdefault(run.printer_id, run)
    return heads


# ============== Holds on the print queue ==============

_WEEKDAYS = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")


def schedule_hold_blocks(next_at: datetime, now: datetime, estimate_seconds: int | None) -> bool:
    """Would a job started ``now`` still be on the printer at ``next_at``?

    Both naive UTC. With an estimate the job has to be done SCHEDULE_MARGIN
    before the slot; without one it is held once the slot is
    UNKNOWN_DURATION_HOLD or less away.
    """
    if not estimate_seconds or estimate_seconds <= 0:
        return next_at - now <= UNKNOWN_DURATION_HOLD
    return now + timedelta(seconds=estimate_seconds) + SCHEDULE_MARGIN > next_at


def format_duration(seconds: int) -> str:
    hours, minutes = divmod(max(0, int(seconds)) // 60, 60)
    return f"{hours}h {minutes}m" if hours else f"{minutes}m"


def queue_hold_for_run(run: MaintenanceRun) -> str:
    """The queue row's wording for a printer a maintenance run reserves.

    "Maintenance run pending: <item> (<state>)", the state being the run's
    waiting reason as a phrase -- with the bed temperature for bed_too_warm
    -- or "queued" / "running".
    """
    if run.status == "running":
        state = QUEUE_HOLD_RUN_RUNNING
    elif run.waiting_reason is None:
        state = QUEUE_HOLD_RUN_QUEUED
    else:
        state = QUEUE_HOLD_RUN_PHRASES.get(run.waiting_reason, run.waiting_reason)
        bed_temp = (run.waiting_detail or {}).get("bed_temp") if run.waiting_reason == WAIT_BED_TOO_WARM else None
        if isinstance(bed_temp, (int, float)):
            state = f"{state}, {bed_temp:g} °C"
    return f"{QUEUE_HOLD_RUN_PREFIX}{run_item_name(run)} ({state})"


def queue_hold_for_schedule(next_at: datetime, estimate_seconds: int | None) -> str:
    """The queue row's wording for a job that would run into the slot at ``next_at`` (naive UTC).

    The slot is written on the server's local clock, the way the card shows
    the schedule time; the weekday is always English here and translated
    by the frontend.
    """
    local = next_at.replace(tzinfo=timezone.utc).astimezone(local_zone())
    when = f"{_WEEKDAYS[local.weekday()]} {local:%H:%M}"
    if not estimate_seconds or estimate_seconds <= 0:
        return f"{QUEUE_HOLD_SCHEDULE_PREFIX}{when} — this job would run into it (duration unknown)"
    return (
        f"{QUEUE_HOLD_SCHEDULE_PREFIX}{when} — this job would run into it "
        f"(estimated {format_duration(estimate_seconds)})"
    )


def queue_hold_clauses(reserved: list[tuple[str, str]]) -> list[str]:
    """The waiting-reason clauses for the printers a model-based item was kept off.

    One clause per distinct hold, in the order the holds were met, naming
    the printers under it the way the neighbouring "Busy: A, B" does:
    "Maintenance run pending: Printer Calibration (queued) — H2S-01, H2S-02".
    Four printers all scheduled for Sunday noon give one sentence, not four.
    """
    names_by_hold: dict[str, list[str]] = {}
    for name, hold in reserved:
        names_by_hold.setdefault(hold, []).append(name)
    return [f"{hold}{QUEUE_HOLD_PRINTERS_JOINER}{', '.join(names)}" for hold, names in names_by_hold.items()]


def is_queue_hold(clause: str) -> bool:
    """Is this waiting-reason clause one of the two maintenance holds?

    Both resolve by themselves -- the run closes, the slot passes -- so the
    scheduler files them with the busy-only reasons: no "job waiting"
    notification, and the item stays on the queue forecast.
    """
    return clause.startswith(QUEUE_HOLD_RUN_PREFIX) or clause.startswith(QUEUE_HOLD_SCHEDULE_PREFIX)


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

    Nozzle offset only exists on dual-nozzle printers, and the Micro Lidar only
    on the X1 series -- offering either elsewhere asks for a calibration the
    machine cannot run. The remaining flags are offered everywhere: the
    firmware ignores bits for hardware it does not have, and unlike those two
    there is no model list to hide them by. The high-temperature bed is the
    open one: it is a P2S/H2 feature in practice, but not from any source
    solid enough to gate on.
    """
    flags = list(CALIBRATION_FLAGS)
    if not is_dual_nozzle_model(printer_model):
        flags.remove("nozzle_offset")
    if not has_micro_lidar(printer_model):
        flags.remove("micro_lidar")
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


async def fail_stale_running_runs(db: AsyncSession, now: datetime | None = None) -> list[MaintenanceRun]:
    """Close runs that have been "running" longer than any calibration takes.

    Covers the restart case: a run dispatched before Bambuddy went down whose
    completion event was never seen. Flushed, not committed. The closed rows
    come back with their item, type and printer loaded, ready for
    :func:`notify_run_finished` once the caller has committed.
    """
    now = now or utcnow_naive()
    cutoff = now - STALE_RUNNING_AFTER
    result = await db.execute(
        select(MaintenanceRun)
        .where(MaintenanceRun.status == "running")
        .where(MaintenanceRun.started_at < cutoff)
        .options(
            selectinload(MaintenanceRun.printer_maintenance).selectinload(PrinterMaintenance.maintenance_type),
            selectinload(MaintenanceRun.printer),
        )
    )
    rows = list(result.scalars().all())
    for run in rows:
        run.status = "failed"
        run.error_message = "Lost track of the run: no completion was reported"
        run.completed_at = now
        logger.warning(
            "Maintenance run %d on printer %d never reported completion; marked failed", run.id, run.printer_id
        )
    return rows


def _cancel_runs(runs: list[MaintenanceRun], now: datetime) -> list[MaintenanceRun]:
    """Close ``runs`` as cancelled, exactly as the Cancel button would."""
    for run in runs:
        run.status = "cancelled"
        set_waiting(run, None)
        run.completed_at = now
        logger.info("Maintenance run %d cancelled: its item is no longer active", run.id)
    return runs


async def cancel_pending_runs_for_items(
    db: AsyncSession, item_ids: Sequence[int], *, now: datetime | None = None
) -> list[MaintenanceRun]:
    """Cancel the pending runs of items that have just been switched off (#3127).

    Switching an item off -- on its card, by unticking its printer on the
    types tab, or by hiding the whole type -- takes its card off the page,
    and with it the only Cancel button there is. A run left pending would go
    on holding the printer's print queue and would still be dispatched once
    its wait cleared, for something the user has just turned off, so it goes
    with the card. A run already *running* is left alone: the command is on
    the printer and the printer's own completion event closes it. Flushed,
    not committed.
    """
    if not item_ids:
        return []
    result = await db.execute(
        select(MaintenanceRun)
        .where(MaintenanceRun.printer_maintenance_id.in_(set(item_ids)))
        .where(MaintenanceRun.status == "pending")
    )
    return _cancel_runs(list(result.scalars().all()), now or utcnow_naive())


async def cancel_orphaned_pending_runs(db: AsyncSession, now: datetime | None = None) -> list[MaintenanceRun]:
    """Cancel pending runs whose item is switched off or whose type is hidden.

    The routes close these the moment the user switches the item off; this
    sweep is what catches the rows an older version left behind and any a
    concurrent request slipped past. Flushed, not committed.
    """
    result = await db.execute(
        select(MaintenanceRun)
        .join(MaintenanceRun.printer_maintenance)
        .join(PrinterMaintenance.maintenance_type)
        .where(MaintenanceRun.status == "pending")
        .where(or_(PrinterMaintenance.enabled.is_(False), MaintenanceType.is_deleted.is_(True)))
    )
    return _cancel_runs(list(result.scalars().all()), now or utcnow_naive())


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


async def notify_run_finished(db: AsyncSession, run: MaintenanceRun) -> bool:
    """Tell the notification providers that ``run`` closed, unless its item is muted.

    Called after the closing commit from every path that ends a run -- the
    printer's completion event, the stale sweep, a dispatch the printer
    refused and a Cancel pressed in Bambuddy -- so a run that was queued
    reports exactly once, whichever way it ended. ``run`` must carry its item
    (with type) and printer. Never raises: a provider being
    down must not undo the run's bookkeeping, so the caller's commit comes
    first and a failure here is only logged. Returns True when the event was
    handed to the notification service.
    """
    item = run.printer_maintenance
    if not item.notifications_enabled:
        return False
    from backend.app.services.notification_service import notification_service

    try:
        await notification_service.on_maintenance_run(
            run.printer_id, run.printer.name, item.maintenance_type.name, run.status, run.error_message, db
        )
    except Exception as e:
        logger.warning("Failed to send the maintenance run notification for run %d: %s", run.id, e)
        return False
    return True


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
        set_waiting(run, None)
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
        await notify_run_finished(db, run)
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
