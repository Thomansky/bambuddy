"""API routes for flow-dynamics (pressure advance) calibration.

The measurement is a print, so starting one is gated like starting a print
(``printers:control``) and writing its result is gated like editing a
K-profile (``kprofiles:update``) — the two things the run actually does, each
behind the permission that already governs it.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.core.auth import RequirePermissionIfAuthEnabled, get_current_user_optional
from backend.app.core.database import get_db
from backend.app.core.permissions import Permission
from backend.app.models.pa_calibration import ACTIVE_PA_STATUSES, PaCalibrationRun
from backend.app.models.printer import Printer
from backend.app.models.user import User
from backend.app.schemas.pa_calibration import (
    PaCalibrationPreflight,
    PaCalibrationRunCreate,
)
from backend.app.services import pa_calibration as pa
from backend.app.services.printer_manager import printer_manager
from backend.app.services.slot_nozzle import resolve_slot_nozzle
from backend.app.utils.local_time import utcnow_naive
from backend.app.utils.pa_calibration import write_nozzle_id
from backend.app.utils.printer_models import normalize_printer_model, supports_sliced_pa_calibration

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/printers/{printer_id}/pa-calibration", tags=["pa-calibration"])


async def _get_printer(db: AsyncSession, printer_id: int) -> Printer:
    printer = (await db.execute(select(Printer).where(Printer.id == printer_id))).scalar_one_or_none()
    if printer is None:
        raise HTTPException(404, "Printer not found")
    return printer


async def _active_run(db: AsyncSession, printer_id: int) -> PaCalibrationRun | None:
    result = await db.execute(
        select(PaCalibrationRun)
        .where(PaCalibrationRun.printer_id == printer_id)
        .where(PaCalibrationRun.status.in_(ACTIVE_PA_STATUSES))
        .order_by(PaCalibrationRun.id.desc())
    )
    return result.scalars().first()


def _rendered_k(k_value) -> str | None:
    """A profile's stored K in the six-decimal form the write used, or None.

    ``KProfile.k_value`` is whatever string the printer put in its table, so an
    empty or non-numeric one has to compare unequal rather than raise: the row
    is already at ``saving`` by the time the read-back runs, and a ValueError
    out of the route would leave it there with a bare 500 and no reason on it,
    to be reported two hours later as a timeout.
    """
    try:
        return f"{float(k_value):.6f}"
    except (TypeError, ValueError):
        return None


def _stored_profile(profiles, *, filament_id: str, nozzle_id: str | None):
    """The K-profile this run would overwrite, or None if it would create one.

    Matched the way the printer matches on a write: same filament, same
    position-normalised nozzle id. The fitted nozzle reports ``HS01-0.4`` while
    the table files it as ``HS00-0.4``, so both sides go through the same
    translation before they are compared.
    """
    if not filament_id:
        return None
    try:
        wanted = write_nozzle_id(nozzle_id or "")
    except ValueError:
        wanted = None
    for profile in profiles or []:
        if getattr(profile, "filament_id", "") != filament_id:
            continue
        stored = getattr(profile, "nozzle_id", "") or ""
        if wanted is None or not stored:
            # An X1C reports no nozzle_id at all. Falling back to a
            # filament-only match is better than showing "will create" for a
            # profile that plainly exists.
            return profile
        try:
            if write_nozzle_id(stored) == wanted:
                return profile
        except ValueError:
            continue
    return None


async def _suggest_presets(db: AsyncSession, printer: Printer, nozzle_diameter: str, material: str) -> dict | None:
    """A starting triplet of standard presets for this machine and filament.

    A suggestion, not a decision: the modal shows all three and the user can
    change any of them, which is the only way a third-party filament can be
    calibrated at its own temperatures. Returns None when the sidecar's bundle
    has nothing for this printer — the modal then says so rather than starting
    a run that would slice against the wrong machine.

    Matching is by name against the sidecar's own bundled list, plus the
    slicer's ``compatible_printers`` for process and filament. That field is
    the only truthful source for several Bambu models, whose process presets
    are named after a different machine (#2982).
    """
    from backend.app.api.routes.slicer_presets import _fetch_bundled_presets

    bundled = await _fetch_bundled_presets(db)
    model = normalize_printer_model(printer.model) or (printer.model or "")
    if not model:
        return None

    def names_printer(name: str) -> bool:
        lowered = name.lower()
        return model.lower() in lowered and nozzle_diameter in lowered

    printer_preset = next((p for p in bundled.get("printer", []) if names_printer(p.name)), None)
    if printer_preset is None:
        return None

    def compatible(preset) -> bool:
        allowed = getattr(preset, "compatible_printers", None)
        if not allowed:
            # Older sidecars do not report the field. Fall back to the name,
            # degraded exactly as the slice modal is on those sidecars.
            return model.lower() in preset.name.lower()
        return printer_preset.name in allowed

    processes = [p for p in bundled.get("process", []) if compatible(p)]
    process = next((p for p in processes if "standard" in p.name.lower()), None) or (
        processes[0] if processes else None
    )

    filaments = [f for f in bundled.get("filament", []) if compatible(f)]
    material_lower = (material or "").strip().lower()
    filament = None
    if material_lower:
        filament = next(
            (f for f in filaments if (f.filament_type or "").strip().lower() == material_lower),
            None,
        ) or next((f for f in filaments if material_lower in f.name.lower()), None)
    filament = filament or (filaments[0] if filaments else None)

    if process is None or filament is None:
        return None
    return {
        "printer": {"source": "standard", "id": printer_preset.id},
        "process": {"source": "standard", "id": process.id},
        "filament": {"source": "standard", "id": filament.id},
    }


@router.get("/preflight", response_model=PaCalibrationPreflight)
async def preflight(
    printer_id: int,
    ams_id: int = Query(..., description="AMS unit id, or 254/255 for the external spool"),
    slot_id: int = Query(0, description="Slot within the AMS unit (0-3); 0 for a single-slot unit"),
    db: AsyncSession = Depends(get_db),
    _: User | None = RequirePermissionIfAuthEnabled(Permission.KPROFILES_READ),
):
    """Everything the modal needs, including every reason Start is disabled."""
    printer = await _get_printer(db, printer_id)
    state = printer_manager.get_status(printer_id)

    if not supports_sliced_pa_calibration(printer.model):
        # Answered without touching the printer or the sidecar: on an
        # unsupported model there is nothing else worth reporting, and the
        # action is meant to be visibly disabled rather than absent.
        return PaCalibrationPreflight(
            supported=False,
            blocked_reasons=["model_not_supported"],
            printer_model=printer.model,
        )

    slot_nozzle = resolve_slot_nozzle(state, ams_id, slot_id, printer.model)
    extruder_id = slot_nozzle.extruder_or_default
    nozzle_diameter = slot_nozzle.diameter
    nozzle_id = pa.nozzle_id_for_extruder(state, extruder_id) if state else None

    tray = pa.find_slot(state, ams_id, slot_id) if state else None
    filament_id = pa.slot_filament_id(tray)

    slicer_url = await pa.resolve_slicer_url(db)
    if slicer_url:
        # Asked here rather than assumed: a sidecar that is configured but down
        # is the most common way this feature is unavailable, and the modal has
        # to be able to say which of the two it is.
        from backend.app.services.slicer_api import SlicerApiService

        try:
            async with SlicerApiService(slicer_url) as service:
                await service.health()
        except Exception as exc:  # noqa: BLE001 — any failure means unusable
            logger.info("PA calibration preflight: sidecar at %s unreachable: %s", slicer_url, exc)
            slicer_url = ""

    active = await _active_run(db, printer_id)
    # The queue's side of the reservation. Not `pa.active_run_printer_ids`,
    # which only ever reports this feature's own runs — that is what
    # `run_already_active` says, and asking it here produced a blocker that
    # could not occur while the one the spec asks for (the queue, an upload in
    # flight, a drying cycle) was never reported at all.
    reserved = await pa.printer_reserved_elsewhere(db, printer_id)
    reasons = pa.blocking_reasons(
        printer=printer,
        state=state,
        ams_id=ams_id,
        slot_id=slot_id,
        extruder_id=extruder_id,
        slicer_url=slicer_url,
        has_active_run=active is not None,
        printer_reserved=reserved,
    )

    presets = None
    if filament_id:
        presets = await _suggest_presets(db, printer, nozzle_diameter, str((tray or {}).get("tray_type") or ""))
        if presets is None and "slicer_not_configured" not in reasons:
            reasons.append("printer_preset_unavailable")

    current_k = None
    current_cali_idx = None
    current_profile_name = None
    client = printer_manager.get_client(printer_id)
    if client is not None and getattr(client.state, "connected", False) and filament_id:
        # A fresh read, not the cached table: the confirmation card shows
        # old -> new, and an old value that is merely the last thing Bambuddy
        # happened to see is not a fact about the printer.
        profiles = await client.get_kprofiles(nozzle_diameter=nozzle_diameter)
        existing = _stored_profile(profiles, filament_id=filament_id, nozzle_id=nozzle_id)
        if existing is not None:
            try:
                current_k = float(existing.k_value)
            except (TypeError, ValueError):
                current_k = None
            current_cali_idx = getattr(existing, "slot_id", None)
            current_profile_name = getattr(existing, "name", None)

    return PaCalibrationPreflight(
        supported=True,
        blocked_reasons=reasons,
        printer_model=printer.model,
        nozzle_diameter=nozzle_diameter,
        nozzle_id=nozzle_id,
        extruder_id=extruder_id,
        filament={
            "filament_id": filament_id,
            "setting_id": str((tray or {}).get("tray_id_name") or ""),
            "name": pa.slot_filament_name(tray),
            "material": str((tray or {}).get("tray_type") or ""),
            "colour": str((tray or {}).get("tray_color") or ""),
        },
        current_k=current_k,
        current_cali_idx=current_cali_idx,
        current_profile_name=current_profile_name,
        presets=presets,
        plate_types=list(pa.PA_PLATE_TYPES),
        default_plate_type=pa.PA_PLATE_TYPES[-1],
        estimated_seconds=pa.PA_ESTIMATED_SECONDS,
        estimated_grams=pa.PA_ESTIMATED_GRAMS,
    )


@router.post("/runs", status_code=202)
async def create_run(
    printer_id: int,
    body: PaCalibrationRunCreate,
    db: AsyncSession = Depends(get_db),
    user: User | None = Depends(get_current_user_optional),
    _: User | None = RequirePermissionIfAuthEnabled(Permission.PRINTERS_CONTROL),
):
    """Queue a calibration run. The scheduler dispatches it when the printer is free."""
    printer = await _get_printer(db, printer_id)
    if not supports_sliced_pa_calibration(printer.model):
        raise HTTPException(400, "This printer model cannot measure pressure advance by printing.")
    if not body.plate_confirmed:
        # The job prints on the bed. Without the explicit tick this would be a
        # toolhead moving across whatever the last print left there.
        raise HTTPException(400, "The build plate has to be confirmed empty before a calibration print can start.")
    if await _active_run(db, printer_id) is not None:
        raise HTTPException(409, "A flow-dynamics calibration is already running on this printer.")
    if body.plate_type not in pa.PA_PLATE_TYPES:
        raise HTTPException(400, f"Unknown plate type: {body.plate_type}")

    state = printer_manager.get_status(printer_id)
    if state is None or not state.connected:
        raise HTTPException(400, "Printer not connected")

    tray = pa.find_slot(state, body.ams_id, body.slot_id)
    filament_id = pa.slot_filament_id(tray)
    if not filament_id:
        raise HTTPException(400, "That slot holds no identified filament.")

    slot_nozzle = resolve_slot_nozzle(state, body.ams_id, body.slot_id, printer.model)
    extruder_id = slot_nozzle.extruder_or_default
    nozzle_id = pa.nozzle_id_for_extruder(state, extruder_id)
    if not nozzle_id:
        raise HTTPException(400, "The printer has not reported which nozzle is fitted.")

    k_before = None
    client = printer_manager.get_client(printer_id)
    if client is not None:
        profiles = await client.get_kprofiles(nozzle_diameter=slot_nozzle.diameter)
        existing = _stored_profile(profiles, filament_id=filament_id, nozzle_id=nozzle_id)
        if existing is not None:
            try:
                k_before = float(existing.k_value)
            except (TypeError, ValueError):
                k_before = None

    row = PaCalibrationRun(
        printer_id=printer_id,
        ams_id=body.ams_id,
        slot_id=body.slot_id,
        tray_id=pa.global_tray_id(body.ams_id, body.slot_id),
        extruder_id=extruder_id,
        filament_id=filament_id,
        setting_id="",
        filament_name=pa.slot_filament_name(tray),
        nozzle_diameter=slot_nozzle.diameter,
        nozzle_id=nozzle_id,
        plate_type=body.plate_type,
        plate_confirmed=True,
        presets=body.presets.model_dump(),
        method="sliced_print",
        status="queued",
        stage="queued",
        k_before=k_before,
        created_by_id=user.id if user else None,
    )
    db.add(row)
    try:
        await db.commit()
    except IntegrityError:
        # The partial unique index caught a race the read above could not.
        await db.rollback()
        raise HTTPException(409, "A flow-dynamics calibration is already running on this printer.") from None
    await db.refresh(row)
    return pa.run_to_response(row)


@router.get("/runs")
async def list_runs(
    printer_id: int,
    active: bool = Query(False, description="Only runs that are still live"),
    limit: int = Query(20, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
    _: User | None = RequirePermissionIfAuthEnabled(Permission.KPROFILES_READ),
):
    await _get_printer(db, printer_id)
    query = select(PaCalibrationRun).where(PaCalibrationRun.printer_id == printer_id)
    if active:
        query = query.where(PaCalibrationRun.status.in_(ACTIVE_PA_STATUSES))
    query = query.order_by(PaCalibrationRun.id.desc()).limit(limit)
    rows = (await db.execute(query)).scalars().all()
    return {"runs": [pa.run_to_response(row) for row in rows]}


async def _get_run(db: AsyncSession, printer_id: int, run_id: int) -> PaCalibrationRun:
    row = await db.get(PaCalibrationRun, run_id)
    if row is None or row.printer_id != printer_id:
        raise HTTPException(404, "Calibration run not found")
    return row


@router.get("/runs/{run_id}")
async def get_run(
    printer_id: int,
    run_id: int,
    db: AsyncSession = Depends(get_db),
    _: User | None = RequirePermissionIfAuthEnabled(Permission.KPROFILES_READ),
):
    return pa.run_to_response(await _get_run(db, printer_id, run_id))


@router.post("/runs/{run_id}/confirm")
async def confirm_run(
    printer_id: int,
    run_id: int,
    db: AsyncSession = Depends(get_db),
    _: User | None = RequirePermissionIfAuthEnabled(Permission.KPROFILES_UPDATE),
):
    """Write the measured K value to the printer, then read it back.

    This is the only place in the feature that changes persistent printer-side
    state, and it happens only because a person pressed this button.
    """
    row = await _get_run(db, printer_id, run_id)
    printer = await _get_printer(db, printer_id)
    if row.status != "awaiting_confirmation":
        raise HTTPException(409, f"This run is {row.status}, not waiting for confirmation.")

    client = printer_manager.get_client(printer_id)
    if client is None or not client.state.connected:
        raise HTTPException(400, "Printer not connected")

    # The filament has to still be the one that was measured. A spool swapped
    # during the seven minutes would otherwise get another filament's K value.
    state = printer_manager.get_status(printer_id)
    current = pa.slot_filament_id(pa.find_slot(state, row.ams_id, row.slot_id)) if state else ""
    if current != row.filament_id:
        pa.fail_run(row, f"The filament in the slot changed ({row.filament_id} -> {current or 'empty'}).")
        await db.commit()
        raise HTTPException(409, row.error_message or "The filament in the slot changed.")

    profiles = await client.get_kprofiles(nozzle_diameter=row.nozzle_diameter)
    existing = _stored_profile(profiles, filament_id=row.filament_id, nozzle_id=row.nozzle_id)
    profile_name = (getattr(existing, "name", "") or "").strip() or row.filament_name or row.filament_id

    try:
        payload = pa.build_write_payload(row, profile_name=profile_name)
    except ValueError as exc:
        pa.fail_run(row, str(exc))
        await db.commit()
        raise HTTPException(400, str(exc)) from None

    pa.set_stage(row, "saving")
    await db.commit()

    if existing is None:
        # Unverified branch: the capture only covers overwriting an existing
        # profile. set_kprofile mints a setting_id for a new one, and the slot
        # then has to be bound to the new cali_idx. Logged at INFO so a real
        # run of this path is identifiable in a support bundle.
        logger.info(
            "PA calibration run %s: no existing profile for %s on %s — creating one (less-tested path)",
            row.id,
            row.filament_id,
            payload["nozzle_id"],
        )
        seq = client.set_kprofile(
            filament_id=row.filament_id,
            name=profile_name,
            k_value=payload["k_value"],
            nozzle_diameter=row.nozzle_diameter,
            nozzle_id=payload["nozzle_id"],
            extruder_id=row.extruder_id,
            n_coef=payload["n_coef"],
        )
    else:
        seq = client.set_measured_kprofile(row.nozzle_diameter, payload)

    if not seq:
        pa.fail_run(row, "The write could not be sent to the printer.")
        await db.commit()
        raise HTTPException(500, row.error_message or "The write could not be sent.")

    ok, detail = await client.await_cali_ack(seq)
    if not ok:
        pa.fail_run(row, detail or "The printer refused the write.")
        await db.commit()
        await pa.notify("failed", row, printer.name, row.error_message or "")
        raise HTTPException(502, row.error_message or "The printer refused the write.")

    # Read back. await_cali_ack answers True on a timeout by design, so the
    # table itself is the only proof the value was stored.
    stored = await client.get_kprofiles(nozzle_diameter=row.nozzle_diameter)
    written = _stored_profile(stored, filament_id=row.filament_id, nozzle_id=row.nozzle_id)
    expected = payload["k_value"]
    if written is None or _rendered_k(written.k_value) != expected:
        pa.fail_run(row, "The printer did not store the value.")
        await db.commit()
        await pa.notify("failed", row, printer.name, row.error_message or "")
        raise HTTPException(502, row.error_message or "The printer did not store the value.")

    if existing is None:
        cali_idx = getattr(written, "slot_id", None)
        if cali_idx is not None:
            # Bind the slot to the profile that was just created, so the next
            # print actually uses it. Only ever the slot that was calibrated.
            client.extrusion_cali_sel(
                ams_id=row.ams_id,
                tray_id=row.tray_id,
                cali_idx=int(cali_idx),
                filament_id=row.filament_id,
                nozzle_diameter=row.nozzle_diameter,
            )

    pa.set_stage(row, "done", progress=100.0)
    row.completed_at = utcnow_naive()
    await db.commit()
    await pa.notify("done", row, printer.name)
    return pa.run_to_response(row)


@router.post("/runs/{run_id}/discard")
async def discard_run(
    printer_id: int,
    run_id: int,
    db: AsyncSession = Depends(get_db),
    _: User | None = RequirePermissionIfAuthEnabled(Permission.KPROFILES_UPDATE),
):
    """Throw the measurement away. Publishes nothing.

    Behind the *write* permission even though it writes nothing to the
    printer: it is the other half of the same decision confirm makes, and it
    destroys a seven-minute measurement irrecoverably. A viewer-level account
    should not be able to end somebody else's run.
    """
    row = await _get_run(db, printer_id, run_id)
    if row.status != "awaiting_confirmation":
        raise HTTPException(409, f"This run is {row.status}, not waiting for confirmation.")
    row.status = "cancelled"
    row.completed_at = utcnow_naive()
    row.error_message = None
    await db.commit()
    return pa.run_to_response(row)


@router.post("/runs/{run_id}/cancel")
async def cancel_run(
    printer_id: int,
    run_id: int,
    stop_print: bool = Query(
        False,
        description="Also stop the calibration print. Required while the run is printing.",
    ),
    db: AsyncSession = Depends(get_db),
    _: User | None = RequirePermissionIfAuthEnabled(Permission.PRINTERS_CONTROL),
):
    """Cancel a live run.

    Before dispatch this is free. While the job is printing the print has to be
    stopped too — a half-done calibration left running measures nothing and
    keeps the printer — so the caller has to say so explicitly rather than
    have Bambuddy stop a print on an ambiguous click.
    """
    row = await _get_run(db, printer_id, run_id)
    printer = await _get_printer(db, printer_id)
    if row.status not in ACTIVE_PA_STATUSES:
        raise HTTPException(409, f"This run is already {row.status}.")

    if row.status == "printing":
        if not stop_print:
            raise HTTPException(409, "This run is printing. Confirm that the print should be stopped.")
        client = printer_manager.get_client(printer_id)
        if client is not None:
            client.stop_print()
            try:
                from backend.app.main import mark_printer_stopped_by_user

                mark_printer_stopped_by_user(printer_id)
            except Exception as exc:  # noqa: BLE001 — classification only
                logger.warning("PA calibration run %s: could not mark user-stop: %s", row.id, exc)

    row.status = "cancelled"
    row.completed_at = utcnow_naive()
    row.waiting_reason = None
    await db.commit()
    await pa.cleanup_remote_file(printer, row)
    return pa.run_to_response(row)
