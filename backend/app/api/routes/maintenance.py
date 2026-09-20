"""Maintenance tracking API routes."""

import logging
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from backend.app.api.routes.settings import get_setting, setting_is_true
from backend.app.core.auth import RequirePermissionIfAuthEnabled
from backend.app.core.database import get_db
from backend.app.core.permissions import Permission
from backend.app.models.maintenance import MaintenanceHistory, MaintenanceRun, MaintenanceType, PrinterMaintenance
from backend.app.models.printer import Printer
from backend.app.models.user import User
from backend.app.schemas.maintenance import (
    CurrentRun,
    MaintenanceHistoryResponse,
    MaintenanceRunResponse,
    MaintenanceStatus,
    MaintenanceTypeCreate,
    MaintenanceTypeResponse,
    MaintenanceTypeUpdate,
    PerformMaintenanceRequest,
    PrinterMaintenanceOverview,
    PrinterMaintenanceResponse,
    PrinterMaintenanceUpdate,
)
from backend.app.services import maintenance_actions
from backend.app.services.maintenance_actions import CALIBRATION_FLAGS, get_printer_total_hours
from backend.app.services.notification_service import notification_service
from backend.app.services.printer_manager import printer_manager
from backend.app.utils.local_time import utcnow_naive
from backend.app.utils.print_jobs import matches_action
from backend.app.utils.printer_models import get_rod_type, has_vision_encoder

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/maintenance", tags=["maintenance"])

# Default maintenance types
DEFAULT_MAINTENANCE_TYPES = [
    # Carbon rod models only (X1/P1)
    # Note: carbon rods must NOT be lubricated — they use plain bearings
    # and lubrication degrades print quality. Only cleaning is offered.
    {
        "name": "Clean Carbon Rods",
        "description": "Wipe carbon rods with a dry cloth",
        "default_interval_hours": 100.0,
        "icon": "Sparkles",
    },
    # Steel rod models only (P2S)
    {
        "name": "Lubricate Steel Rods",
        "description": "Apply lubricant to steel rods for smooth motion",
        "default_interval_hours": 50.0,
        "icon": "Droplet",
    },
    {
        "name": "Clean Steel Rods",
        "description": "Wipe steel rods with a dry cloth",
        "default_interval_hours": 100.0,
        "icon": "Sparkles",
    },
    # Linear rail models only (A1/H2)
    {
        "name": "Lubricate Linear Rails",
        "description": "Apply lubricant to linear rails for smooth motion",
        "default_interval_hours": 50.0,
        "icon": "Droplet",
    },
    {
        "name": "Clean Linear Rails",
        "description": "Wipe linear rails with a dry cloth to remove dust and debris",
        "default_interval_hours": 100.0,
        "icon": "Sparkles",
    },
    # Universal (all models)
    {
        "name": "Clean Nozzle/Hotend",
        "description": "Clean nozzle exterior and perform cold pull if needed",
        "default_interval_hours": 100.0,
        "icon": "Flame",
    },
    {
        "name": "Check Belt Tension",
        "description": "Verify and adjust belt tension for X/Y axes",
        "default_interval_hours": 200.0,
        "icon": "Ruler",
    },
    {
        "name": "Clean Build Plate",
        "description": "Deep clean build plate with IPA or soap",
        "default_interval_hours": 25.0,
        "icon": "Square",
    },
    {
        "name": "Check PTFE Tube",
        "description": "Inspect PTFE tube for wear or discoloration",
        "default_interval_hours": 500.0,
        "icon": "Cable",
    },
    # Performed by the printer itself when Bambuddy asks (#3127)
    {
        "name": "Printer Calibration",
        "description": "Bed leveling, vibration compensation and motor noise cancellation",
        "default_interval_hours": 100.0,
        "icon": "Target",
        "action": maintenance_actions.ACTION_CALIBRATION,
    },
    # H2 series only (vision encoder)
    {
        "name": "Vision Encoder Calibration",
        "description": "Motion precision calibration of the vision encoder",
        "default_interval_hours": 7.0,
        "interval_type": "days",
        "icon": "ScanEye",
        "action": maintenance_actions.ACTION_MOTION_PRECISION,
    },
]

# System types that only apply to printers with a specific rod/rail type.
# "carbon" = X1/P1 series (carbon rods), "steel_rod" = P2S (steel rods),
# "linear_rail" = A1/H2 series. Types not listed here apply to all printers.
_ROD_TYPE_REQUIREMENTS: dict[str, str] = {
    "Clean Carbon Rods": "carbon",
    "Lubricate Steel Rods": "steel_rod",
    "Clean Steel Rods": "steel_rod",
    "Lubricate Linear Rails": "linear_rail",
    "Clean Linear Rails": "linear_rail",
}


# System types that need hardware only some models have (#3127).
_VISION_ENCODER_TYPES = frozenset({"Vision Encoder Calibration"})


def _should_apply_to_printer(type_name: str, printer_model: str | None) -> bool:
    """Check if a system maintenance type should apply to a given printer model."""
    if type_name in _VISION_ENCODER_TYPES:
        return has_vision_encoder(printer_model)

    rod_requirement = _ROD_TYPE_REQUIREMENTS.get(type_name)
    if rod_requirement is None:
        return True  # Not model-specific, applies to all

    rod_type = get_rod_type(printer_model)
    if rod_type is None:
        # Unknown model — default to carbon rods (legacy behavior)
        return rod_requirement == "carbon"

    return rod_type == rod_requirement


async def ensure_default_types(db: AsyncSession) -> None:
    """Ensure default maintenance types exist, remove stale/duplicate ones."""
    result = await db.execute(
        select(MaintenanceType).where(MaintenanceType.is_system.is_(True)).order_by(MaintenanceType.id)
    )
    existing = result.scalars().all()

    default_names = {t["name"] for t in DEFAULT_MAINTENANCE_TYPES}

    # Remove stale system types no longer in defaults (e.g. renamed types)
    # and deduplicate: if concurrent requests created the same type twice,
    # keep only the first (lowest id) and delete the rest.
    seen_names: set[str] = set()
    actions_by_name = {t["name"]: t.get("action") for t in DEFAULT_MAINTENANCE_TYPES}
    for t in existing:
        if t.name not in default_names or t.name in seen_names:
            await db.delete(t)
        else:
            seen_names.add(t.name)
            # The action is what makes the type executable; it is not user
            # editable, so keep it in step with the definition.
            if t.action != actions_by_name.get(t.name):
                t.action = actions_by_name.get(t.name)

    # Create any missing default types
    for type_def in DEFAULT_MAINTENANCE_TYPES:
        if type_def["name"] not in seen_names:
            new_type = MaintenanceType(
                name=type_def["name"],
                description=type_def["description"],
                default_interval_hours=type_def["default_interval_hours"],
                interval_type=type_def.get("interval_type", "hours"),
                icon=type_def["icon"],
                is_system=True,
                action=type_def.get("action"),
            )
            db.add(new_type)

    await db.commit()


# ============== Maintenance Types ==============


@router.get("/types", response_model=list[MaintenanceTypeResponse])
async def get_maintenance_types(
    db: AsyncSession = Depends(get_db),
    _: User | None = RequirePermissionIfAuthEnabled(Permission.MAINTENANCE_READ),
):
    """Get all maintenance types."""
    await ensure_default_types(db)
    result = await db.execute(
        select(MaintenanceType)
        .where(MaintenanceType.is_deleted.is_(False))
        .order_by(MaintenanceType.is_system.desc(), MaintenanceType.name)
    )
    return result.scalars().all()


@router.post("/types", response_model=MaintenanceTypeResponse)
async def create_maintenance_type(
    data: MaintenanceTypeCreate,
    db: AsyncSession = Depends(get_db),
    _: User | None = RequirePermissionIfAuthEnabled(Permission.MAINTENANCE_CREATE),
):
    """Create a custom maintenance type."""
    new_type = MaintenanceType(
        name=data.name,
        description=data.description,
        default_interval_hours=data.default_interval_hours,
        interval_type=data.interval_type,
        icon=data.icon,
        wiki_url=data.wiki_url,
        is_system=False,
    )
    db.add(new_type)
    await db.commit()
    await db.refresh(new_type)
    return new_type


@router.patch("/types/{type_id}", response_model=MaintenanceTypeResponse)
async def update_maintenance_type(
    type_id: int,
    data: MaintenanceTypeUpdate,
    db: AsyncSession = Depends(get_db),
    _: User | None = RequirePermissionIfAuthEnabled(Permission.MAINTENANCE_UPDATE),
):
    """Update a maintenance type."""
    result = await db.execute(select(MaintenanceType).where(MaintenanceType.id == type_id))
    maint_type = result.scalar_one_or_none()
    if not maint_type:
        raise HTTPException(status_code=404, detail="Maintenance type not found")

    update_data = data.model_dump(exclude_unset=True)
    for key, value in update_data.items():
        setattr(maint_type, key, value)

    await db.commit()
    await db.refresh(maint_type)
    return maint_type


@router.delete("/types/{type_id}")
async def delete_maintenance_type(
    type_id: int,
    db: AsyncSession = Depends(get_db),
    _: User | None = RequirePermissionIfAuthEnabled(Permission.MAINTENANCE_DELETE),
):
    """Delete a maintenance type."""
    result = await db.execute(select(MaintenanceType).where(MaintenanceType.id == type_id))
    maint_type = result.scalar_one_or_none()
    if not maint_type:
        raise HTTPException(status_code=404, detail="Maintenance type not found")

    if maint_type.is_system:
        maint_type.is_deleted = True
        await db.commit()
        return {"status": "deleted"}

    await db.delete(maint_type)
    await db.commit()
    return {"status": "deleted"}


@router.post("/types/restore-defaults")
async def restore_default_maintenance_types(
    db: AsyncSession = Depends(get_db),
    _: User | None = RequirePermissionIfAuthEnabled(Permission.MAINTENANCE_DELETE),
):
    """Restore deleted default maintenance types."""
    await ensure_default_types(db)
    result = await db.execute(
        select(MaintenanceType).where(MaintenanceType.is_system.is_(True)).where(MaintenanceType.is_deleted.is_(True))
    )
    deleted_types = result.scalars().all()
    for maint_type in deleted_types:
        maint_type.is_deleted = False

    await db.commit()
    return {"restored": len(deleted_types)}


# ============== Printer Maintenance ==============


async def _require_plate_clear(db: AsyncSession) -> bool:
    """The plate-clear gate the automatic calibration triggers wait behind.

    Default False like the scheduler's own read (#1865): a missing row is
    the gate being off.
    """
    return setting_is_true(await get_setting(db, "require_plate_clear"))


async def _get_printer_maintenance_internal(
    printer_id: int,
    db: AsyncSession,
    commit: bool = True,
    require_plate_clear: bool | None = None,
) -> PrinterMaintenanceOverview:
    """Internal helper to get maintenance overview for a specific printer.

    ``require_plate_clear`` is read from the settings when not given; the
    all-printers overview reads it once and passes it in.
    """
    await ensure_default_types(db)
    if require_plate_clear is None:
        require_plate_clear = await _require_plate_clear(db)

    # Get printer
    result = await db.execute(select(Printer).where(Printer.id == printer_id))
    printer = result.scalar_one_or_none()
    if not printer:
        raise HTTPException(status_code=404, detail="Printer not found")

    total_hours = await get_printer_total_hours(db, printer_id)

    # Get all maintenance types
    result = await db.execute(select(MaintenanceType).where(MaintenanceType.is_deleted.is_(False)))
    all_types = result.scalars().all()

    # Get printer's maintenance items
    result = await db.execute(
        select(PrinterMaintenance)
        .where(PrinterMaintenance.printer_id == printer_id)
        .options(selectinload(PrinterMaintenance.maintenance_type))
    )
    existing_items = {item.maintenance_type_id: item for item in result.scalars().all()}

    # Newest run per item, one query for the printer (#3127): the active one
    # drives the card's status line, a finished one is its "last result".
    result = await db.execute(
        select(MaintenanceRun).where(MaintenanceRun.printer_id == printer_id).order_by(MaintenanceRun.id.desc())
    )
    latest_runs: dict[int, MaintenanceRun] = {}
    for run in result.scalars().all():
        latest_runs.setdefault(run.printer_maintenance_id, run)

    maintenance_items = []
    due_count = 0
    warning_count = 0

    now = datetime.now(timezone.utc)

    for maint_type in all_types:
        # Skip system types that don't apply to this printer model
        # (e.g., "Clean Carbon Rods" for H2D which has steel rods)
        if maint_type.is_system and not _should_apply_to_printer(maint_type.name, printer.model):
            continue

        item = existing_items.get(maint_type.id)
        default_interval_type = getattr(maint_type, "interval_type", "hours") or "hours"

        if item:
            interval = item.custom_interval_hours or maint_type.default_interval_hours
            # Use custom interval type if set, otherwise use type's default
            interval_type = getattr(item, "custom_interval_type", None) or default_interval_type
            enabled = item.enabled
            notifications_enabled = item.notifications_enabled
            last_performed_hours = item.last_performed_hours
            last_performed_at = item.last_performed_at
            item_id = item.id
        else:
            # Only auto-create maintenance items for system types
            # Custom types need to be manually assigned per printer
            if not maint_type.is_system:
                continue

            # Create default entry for this printer/type
            item = PrinterMaintenance(
                printer_id=printer_id,
                maintenance_type_id=maint_type.id,
                enabled=True,
                last_performed_hours=0.0,
            )
            db.add(item)
            await db.flush()

            interval = maint_type.default_interval_hours
            interval_type = default_interval_type
            enabled = True
            notifications_enabled = True
            last_performed_hours = 0.0
            last_performed_at = None
            item_id = item.id

        due = maintenance_actions.compute_due_state(
            interval, interval_type, total_hours, last_performed_hours, last_performed_at, now
        )
        if last_performed_at is not None and last_performed_at.tzinfo is None:
            last_performed_at = last_performed_at.replace(tzinfo=timezone.utc)

        if enabled:
            if due.is_due:
                due_count += 1
            elif due.is_warning:
                warning_count += 1

        latest_run = latest_runs.get(item_id)
        current_run = (
            CurrentRun(
                id=latest_run.id,
                status=latest_run.status,
                source=latest_run.source,
                waiting_reason=latest_run.waiting_reason,
                waiting_detail=latest_run.waiting_detail,
                started_at=latest_run.started_at,
            )
            if latest_run is not None and latest_run.status in maintenance_actions.RUN_ACTIVE_STATUSES
            else None
        )
        last_run = (
            MaintenanceRunResponse.model_validate(latest_run)
            if latest_run is not None and current_run is None
            else None
        )

        maintenance_items.append(
            MaintenanceStatus(
                id=item_id,
                printer_id=printer_id,
                printer_name=printer.name,
                printer_model=printer.model,
                maintenance_type_id=maint_type.id,
                maintenance_type_name=maint_type.name,
                maintenance_type_icon=maint_type.icon,
                maintenance_type_wiki_url=getattr(maint_type, "wiki_url", None),
                enabled=enabled,
                notifications_enabled=notifications_enabled,
                interval_hours=interval,
                interval_type=interval_type,
                current_hours=total_hours,
                hours_since_maintenance=due.hours_since,
                hours_until_due=due.hours_until,
                days_since_maintenance=due.days_since,
                days_until_due=due.days_until,
                is_due=due.is_due,
                is_warning=due.is_warning,
                last_performed_at=last_performed_at,
                action=maint_type.action,
                action_options=maintenance_actions.response_action_options(maint_type.action, item.action_options),
                action_available_options=(
                    maintenance_actions.available_calibration_options(printer.model)
                    if maintenance_actions.has_options(maint_type.action)
                    else None
                ),
                trigger_mode=item.trigger_mode or "manual",
                schedule_days=item.schedule_days,
                schedule_time=item.schedule_time,
                schedule_next_at=item.schedule_next_at,
                current_run=current_run,
                last_run=last_run,
            )
        )

    if commit:
        await db.commit()

    return PrinterMaintenanceOverview(
        printer_id=printer_id,
        printer_name=printer.name,
        printer_model=printer.model,
        total_print_hours=total_hours,
        maintenance_items=maintenance_items,
        due_count=due_count,
        warning_count=warning_count,
        require_plate_clear=require_plate_clear,
    )


@router.get("/printers/{printer_id}", response_model=PrinterMaintenanceOverview)
async def get_printer_maintenance(
    printer_id: int,
    db: AsyncSession = Depends(get_db),
    _: User | None = RequirePermissionIfAuthEnabled(Permission.MAINTENANCE_READ),
):
    """Get maintenance overview for a specific printer."""
    return await _get_printer_maintenance_internal(printer_id, db, commit=True)


@router.get("/overview", response_model=list[PrinterMaintenanceOverview])
async def get_all_maintenance_overview(
    db: AsyncSession = Depends(get_db),
    _: User | None = RequirePermissionIfAuthEnabled(Permission.MAINTENANCE_READ),
):
    """Get maintenance overview for all active printers."""
    await ensure_default_types(db)

    result = await db.execute(select(Printer).where(Printer.is_active.is_(True)))
    printers = result.scalars().all()
    require_plate_clear = await _require_plate_clear(db)

    overviews = []
    for printer in printers:
        # Don't commit after each printer, commit once at the end
        overview = await _get_printer_maintenance_internal(
            printer.id, db, commit=False, require_plate_clear=require_plate_clear
        )
        overviews.append(overview)

    # Commit any new maintenance items created
    await db.commit()

    return overviews


@router.patch("/items/{item_id}", response_model=PrinterMaintenanceResponse)
async def update_printer_maintenance(
    item_id: int,
    data: PrinterMaintenanceUpdate,
    db: AsyncSession = Depends(get_db),
    _: User | None = RequirePermissionIfAuthEnabled(Permission.MAINTENANCE_UPDATE),
):
    """Update a printer maintenance item (e.g., custom interval, enabled, action settings)."""
    result = await db.execute(
        select(PrinterMaintenance)
        .where(PrinterMaintenance.id == item_id)
        .options(selectinload(PrinterMaintenance.maintenance_type))
    )
    item = result.scalar_one_or_none()
    if not item:
        raise HTTPException(status_code=404, detail="Maintenance item not found")

    update_data = data.model_dump(exclude_unset=True)
    action = item.maintenance_type.action
    action_keys = {"action_options", "trigger_mode", "schedule_days", "schedule_time"}
    if action_keys & update_data.keys() and not action:
        raise HTTPException(status_code=400, detail="This maintenance type has no automatic action")
    if "action_options" in update_data:
        options = update_data.pop("action_options")
        if not maintenance_actions.has_options(action) and any(flag in options for flag in CALIBRATION_FLAGS):
            raise HTTPException(status_code=400, detail="This maintenance action has no options")
        item.action_options = maintenance_actions.stored_action_options(action, options)
    for key, value in update_data.items():
        setattr(item, key, value)

    if action:
        # Cross-field rules against the merged state, so a PATCH that only
        # flips the trigger is judged with the days and time already stored.
        if item.trigger_mode == "schedule" and (not item.schedule_days or not item.schedule_time):
            raise HTTPException(status_code=400, detail="A schedule needs at least one weekday and a time")
        if (
            item.trigger_mode != "manual"
            and maintenance_actions.has_options(action)
            and not maintenance_actions.selected_calibration_flags(item.action_options)
        ):
            raise HTTPException(status_code=400, detail="Select at least one calibration option")
        maintenance_actions.refresh_schedule(item)

    await db.commit()
    await db.refresh(item)
    return item


@router.post("/printers/{printer_id}/assign/{type_id}", response_model=PrinterMaintenanceResponse)
async def assign_maintenance_type(
    printer_id: int,
    type_id: int,
    db: AsyncSession = Depends(get_db),
    _: User | None = RequirePermissionIfAuthEnabled(Permission.MAINTENANCE_CREATE),
):
    """Assign a maintenance type to a specific printer (for custom types)."""
    # Verify printer exists
    result = await db.execute(select(Printer).where(Printer.id == printer_id))
    printer = result.scalar_one_or_none()
    if not printer:
        raise HTTPException(status_code=404, detail="Printer not found")

    # Verify maintenance type exists
    result = await db.execute(select(MaintenanceType).where(MaintenanceType.id == type_id))
    maint_type = result.scalar_one_or_none()
    if not maint_type:
        raise HTTPException(status_code=404, detail="Maintenance type not found")

    # Check if already assigned
    result = await db.execute(
        select(PrinterMaintenance).where(
            PrinterMaintenance.printer_id == printer_id,
            PrinterMaintenance.maintenance_type_id == type_id,
        )
    )
    existing = result.scalar_one_or_none()
    if existing:
        raise HTTPException(status_code=400, detail="Maintenance type already assigned to this printer")

    # Create the assignment
    item = PrinterMaintenance(
        printer_id=printer_id,
        maintenance_type_id=type_id,
        enabled=True,
        last_performed_hours=0.0,
    )
    db.add(item)
    await db.commit()

    # Re-fetch with relationship loaded for response serialization
    from sqlalchemy.orm import selectinload

    result = await db.execute(
        select(PrinterMaintenance)
        .options(selectinload(PrinterMaintenance.maintenance_type))
        .where(PrinterMaintenance.id == item.id)
    )
    item = result.scalar_one()

    return item


@router.delete("/items/{item_id}")
async def remove_maintenance_item(
    item_id: int,
    db: AsyncSession = Depends(get_db),
    _: User | None = RequirePermissionIfAuthEnabled(Permission.MAINTENANCE_DELETE),
):
    """Remove a maintenance item (unassign a custom type from a printer)."""
    result = await db.execute(
        select(PrinterMaintenance)
        .where(PrinterMaintenance.id == item_id)
        .options(selectinload(PrinterMaintenance.maintenance_type))
    )
    item = result.scalar_one_or_none()
    if not item:
        raise HTTPException(status_code=404, detail="Maintenance item not found")

    # Only allow removing custom (non-system) types
    if item.maintenance_type.is_system:
        raise HTTPException(status_code=400, detail="Cannot remove system maintenance types")

    await db.delete(item)
    await db.commit()

    return {"status": "removed"}


@router.post("/items/{item_id}/perform", response_model=MaintenanceStatus)
async def perform_maintenance(
    item_id: int,
    data: PerformMaintenanceRequest,
    db: AsyncSession = Depends(get_db),
    _: User | None = RequirePermissionIfAuthEnabled(Permission.MAINTENANCE_UPDATE),
):
    """Mark maintenance as performed (reset the counter)."""
    result = await db.execute(
        select(PrinterMaintenance)
        .where(PrinterMaintenance.id == item_id)
        .options(selectinload(PrinterMaintenance.maintenance_type))
    )
    item = result.scalar_one_or_none()
    if not item:
        raise HTTPException(status_code=404, detail="Maintenance item not found")

    # Get printer for name
    result = await db.execute(select(Printer).where(Printer.id == item.printer_id))
    printer = result.scalar_one()

    history = await maintenance_actions.record_performed(db, item, data.notes)
    current_hours = history.hours_at_maintenance

    await db.commit()

    # MQTT relay - publish maintenance reset
    try:
        from backend.app.services.mqtt_relay import mqtt_relay

        await mqtt_relay.on_maintenance_reset(
            printer_id=item.printer_id,
            printer_name=printer.name,
            maintenance_type=item.maintenance_type.name,
        )
    except Exception:
        pass  # Don't fail if MQTT fails

    # Calculate status
    interval = item.custom_interval_hours or item.maintenance_type.default_interval_hours
    interval_type = getattr(item.maintenance_type, "interval_type", "hours") or "hours"
    hours_since = current_hours - item.last_performed_hours
    hours_until = interval - hours_since

    return MaintenanceStatus(
        id=item.id,
        printer_id=item.printer_id,
        printer_name=printer.name,
        printer_model=printer.model,
        maintenance_type_id=item.maintenance_type_id,
        maintenance_type_name=item.maintenance_type.name,
        maintenance_type_icon=item.maintenance_type.icon,
        maintenance_type_wiki_url=getattr(item.maintenance_type, "wiki_url", None),
        enabled=item.enabled,
        notifications_enabled=item.notifications_enabled,
        interval_hours=interval,
        interval_type=interval_type,
        current_hours=current_hours,
        hours_since_maintenance=hours_since,
        hours_until_due=hours_until if interval_type == "hours" else 0,
        days_since_maintenance=0 if interval_type == "days" else None,
        days_until_due=interval if interval_type == "days" else None,
        is_due=False,
        is_warning=False,
        last_performed_at=item.last_performed_at,
        action=item.maintenance_type.action,
        trigger_mode=item.trigger_mode or "manual",
    )


# ============== Maintenance Runs (#3127) ==============


@router.post("/items/{item_id}/run", response_model=MaintenanceRunResponse)
async def run_maintenance_item(
    item_id: int,
    db: AsyncSession = Depends(get_db),
    _: User | None = RequirePermissionIfAuthEnabled(Permission.MAINTENANCE_UPDATE),
):
    """Queue the item's action now; the scheduler starts it once the printer is idle."""
    result = await db.execute(
        select(PrinterMaintenance)
        .where(PrinterMaintenance.id == item_id)
        .options(selectinload(PrinterMaintenance.maintenance_type))
    )
    item = result.scalar_one_or_none()
    if not item:
        raise HTTPException(status_code=404, detail="Maintenance item not found")
    action = item.maintenance_type.action
    if not action:
        raise HTTPException(status_code=400, detail="This maintenance type has no automatic action")
    if maintenance_actions.has_options(action) and not maintenance_actions.selected_calibration_flags(
        item.action_options
    ):
        raise HTTPException(status_code=400, detail="Select at least one calibration option")
    if await maintenance_actions.get_active_run(db, item.id) is not None:
        raise HTTPException(status_code=409, detail="A run is already pending or running for this item")

    try:
        run = await maintenance_actions.create_run(db, item, "manual")
        await db.commit()
    except IntegrityError:
        # Two "Run now" clicks racing past the read above: the partial unique
        # index on active runs lets exactly one through.
        await db.rollback()
        raise HTTPException(status_code=409, detail="A run is already pending or running for this item")
    await db.refresh(run)
    return run


@router.get("/items/{item_id}/runs", response_model=list[MaintenanceRunResponse])
async def list_maintenance_runs(
    item_id: int,
    limit: int = Query(default=20, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
    _: User | None = RequirePermissionIfAuthEnabled(Permission.MAINTENANCE_READ),
):
    """Newest runs of an item first."""
    result = await db.execute(
        select(MaintenanceRun)
        .where(MaintenanceRun.printer_maintenance_id == item_id)
        .order_by(MaintenanceRun.id.desc())
        .limit(limit)
    )
    return list(result.scalars().all())


def _printer_is_running_action(printer_id: int, action: str | None) -> bool:
    """Is the printer, right now, on the job a run of ``action`` dispatched?

    Cancelling a run must only ever stop that job. A row can say "running"
    long after the calibration ended (missed completion, refused command),
    and by then the printer may be hours into somebody's print -- or on the
    other calibration, which is not this run's to stop either.
    """
    state = printer_manager.get_status(printer_id)
    if not state or state.state not in ("RUNNING", "PAUSE", "PREPARE"):
        return False
    return matches_action(action, state.gcode_file or state.current_print, state.subtask_name)


@router.delete("/runs/{run_id}")
async def cancel_maintenance_run(
    run_id: int,
    db: AsyncSession = Depends(get_db),
    _: User | None = RequirePermissionIfAuthEnabled(Permission.MAINTENANCE_UPDATE),
):
    """Cancel a pending run, or stop a running calibration on the printer."""
    result = await db.execute(
        select(MaintenanceRun)
        .where(MaintenanceRun.id == run_id)
        .options(selectinload(MaintenanceRun.printer_maintenance).selectinload(PrinterMaintenance.maintenance_type))
    )
    run = result.scalar_one_or_none()
    if not run:
        raise HTTPException(status_code=404, detail="Maintenance run not found")
    if run.status not in maintenance_actions.RUN_ACTIVE_STATUSES:
        raise HTTPException(status_code=400, detail="Only pending or running runs can be cancelled")

    action = run.printer_maintenance.maintenance_type.action
    if run.status == "running" and _printer_is_running_action(run.printer_id, action):
        # Best effort; the row is closed even if the publish fails, and the
        # printer's own FAILED report then finds nothing left to close.
        printer_manager.stop_print(run.printer_id)
    elif run.status == "running":
        # The row outlived the calibration -- a completion missed across a
        # restart, or a command the firmware never acted on. Whatever the
        # printer is doing now is not ours to stop.
        logger.info(
            "Maintenance run %d cancelled without a stop: printer %d is not running the calibration",
            run.id,
            run.printer_id,
        )

    run.status = "cancelled"
    maintenance_actions.set_waiting(run, None)
    run.completed_at = utcnow_naive()
    maintenance_actions.refresh_schedule(run.printer_maintenance)
    await db.commit()
    return {"status": "cancelled", "id": run.id}


@router.get("/items/{item_id}/history", response_model=list[MaintenanceHistoryResponse])
async def get_maintenance_history(
    item_id: int,
    db: AsyncSession = Depends(get_db),
    _: User | None = RequirePermissionIfAuthEnabled(Permission.MAINTENANCE_READ),
):
    """Get maintenance history for a specific item."""
    result = await db.execute(
        select(MaintenanceHistory)
        .where(MaintenanceHistory.printer_maintenance_id == item_id)
        .order_by(MaintenanceHistory.performed_at.desc())
    )
    return result.scalars().all()


@router.get("/summary")
async def get_maintenance_summary(
    db: AsyncSession = Depends(get_db),
    _: User | None = RequirePermissionIfAuthEnabled(Permission.MAINTENANCE_READ),
):
    """Get a summary of maintenance status across all printers."""
    await ensure_default_types(db)

    result = await db.execute(select(Printer).where(Printer.is_active.is_(True)))
    printers = result.scalars().all()

    total_due = 0
    total_warning = 0
    printers_with_issues = []

    for printer in printers:
        overview = await get_printer_maintenance(printer.id, db)
        total_due += overview.due_count
        total_warning += overview.warning_count
        if overview.due_count > 0 or overview.warning_count > 0:
            printers_with_issues.append(
                {
                    "printer_id": printer.id,
                    "printer_name": printer.name,
                    "due_count": overview.due_count,
                    "warning_count": overview.warning_count,
                }
            )

    return {
        "total_due": total_due,
        "total_warning": total_warning,
        "printers_with_issues": printers_with_issues,
    }


@router.patch("/printers/{printer_id}/hours")
async def set_printer_hours(
    printer_id: int,
    total_hours: float,
    db: AsyncSession = Depends(get_db),
    _: User | None = RequirePermissionIfAuthEnabled(Permission.MAINTENANCE_UPDATE),
):
    """Set the total print hours for a printer (adjusts offset to match).

    The offset is calculated as: offset = total_hours - runtime_hours
    Where runtime_hours comes from the runtime_seconds counter that tracks
    actual machine active time (RUNNING state only — paused time excluded, #1521).
    """
    # Get printer
    result = await db.execute(select(Printer).where(Printer.id == printer_id))
    printer = result.scalar_one_or_none()
    if not printer:
        raise HTTPException(status_code=404, detail="Printer not found")

    # Get current runtime hours
    runtime_hours = (printer.runtime_seconds or 0) / 3600.0

    # Calculate needed offset
    printer.print_hours_offset = max(0, total_hours - runtime_hours)

    await db.commit()

    # Check for maintenance items that need attention and send notification
    try:
        await ensure_default_types(db)
        overview = await _get_printer_maintenance_internal(printer_id, db, commit=True)

        items_needing_attention = [
            {
                "name": item.maintenance_type_name,
                "is_due": item.is_due,
                "is_warning": item.is_warning,
            }
            for item in overview.maintenance_items
            if item.enabled and item.notifications_enabled and (item.is_due or item.is_warning)
        ]

        if items_needing_attention:
            await notification_service.on_maintenance_due(printer_id, printer.name, items_needing_attention, db)
            logger.info(
                f"Sent maintenance notification for printer {printer_id}: "
                f"{len(items_needing_attention)} items need attention"
            )
    except Exception as e:
        logger.warning("Failed to send maintenance notification: %s", e)

    return {
        "printer_id": printer_id,
        "total_hours": total_hours,
        "runtime_hours": runtime_hours,
        "offset_hours": printer.print_hours_offset,
    }
