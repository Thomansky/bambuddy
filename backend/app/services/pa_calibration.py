"""Measure a filament's pressure advance the way Bambu Studio does on an H2.

On an X1/P1/A1 a flow-dynamics calibration is one MQTT command. On the H2
series that command is refused outright (``result: "fail", reason:
"Unsupport"``, measured on an H2S), and Bambu Studio instead **prints** for it:
it slices a 30 mm line against the user's real printer/process/filament
presets and dispatches it with ``extrude_cali_flag: 1``. The measurement itself
is vendor start-G-code from the machine preset --

    M1002 judge_flag extrude_cali_flag
    M622 J0
        M983.3 F{filament_max_volumetric_speed/2.4} A{nozzle_diameter}
    M623

-- parameterised by the filament preset. Nothing Bambuddy writes performs the
measurement; slicing is how the right vendor block for the user's exact machine
and filament gets into the file. The number it produces is then read back with
``extrusion_cali_get_result`` and, only after the user confirms, written with
``extrusion_cali_set``.

Everything here is shaped against a capture of Studio driving an H2S through
two complete runs (``mqtt-tap/tap-0938BJ611001133-20260920-103100.jsonl``).
"""

from __future__ import annotations

import json
import logging
import tempfile
import uuid
from datetime import timedelta
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.core.config import settings as app_settings
from backend.app.models.pa_calibration import ACTIVE_PA_STATUSES, PaCalibrationRun
from backend.app.models.printer import Printer
from backend.app.services.bambu_ftp import (
    FtpFailureReport,
    UploadCancelled,
    delete_file_async,
    describe_upload_failure,
    get_ftp_retry_settings,
    upload_file_async,
)
from backend.app.services.printer_manager import printer_manager
from backend.app.services.slice_output_check import (
    extrude_cali_gate_missing,
    extrude_cali_nozzle_diameters,
    missing_extrude_cali_message,
    missing_start_gcode_message,
    start_gcode_is_missing,
    unresolved_filament_message,
    unresolved_filament_slots,
)
from backend.app.services.slicer_api import (
    SlicerApiError,
    SlicerApiService,
    get_stall_timeout_seconds,
)
from backend.app.utils.filename import derive_remote_filename
from backend.app.utils.local_time import utcnow_naive
from backend.app.utils.pa_calibration import (
    build_extrusion_cali_set_filament,
    round_k_value,
    write_nozzle_id,
)
from backend.app.utils.printer_models import supports_sliced_pa_calibration

logger = logging.getLogger(__name__)

# The model that gets sliced. A single bar; the print body is cosmetic.
PA_MODEL_PATH = Path(__file__).resolve().parent.parent / "data" / "pa_calibration" / "pa_line.stl"
PA_MODEL_FILENAME = "pa_line.stl"

# What lands on the printer's SD root. ``project_file`` references files as
# ``ftp://{filename}`` with no directory, so the upload has to go to the root.
PA_REMOTE_FILENAME = derive_remote_filename("bambuddy_pa_cali.gcode.3mf")

# Force the one-layer shape whatever process preset the user picked, so the
# job stays a few minutes rather than inheriting a 0.08 mm fine profile's idea
# of how to print a bar.
PA_PROCESS_OVERRIDES: dict[str, str] = {
    "layer_height": "0.25",
    "initial_layer_print_height": "0.25",
    "wall_loops": "1",
    "top_shell_layers": "0",
    "bottom_shell_layers": "1",
    "sparse_infill_density": "0",
    "skirt_loops": "0",
    "brim_type": "no_brim",
    "enable_prime_tower": "0",
    "timelapse_type": "0",
}

# From the captured job's slice_info.config (prediction 344 s, weight 0.07 g),
# rounded up so the modal never promises less than it takes.
PA_ESTIMATED_SECONDS = 420
PA_ESTIMATED_GRAMS = 0.1

# Plate types the modal offers, in the slicer's own vocabulary -- the same
# strings SliceRequest.bed_type takes, because that is where this value goes.
PA_PLATE_TYPES = ("cool_plate", "eng_plate", "hot_plate", "textured_plate")

# The printer must leave idle within this long after project_file, or the
# publish never reached it. There is no ack for project_file, so the state
# transition is the only confirmation -- the same rule _watchdog_print_start
# applies to a queued job.
START_WATCHDOG_SECONDS = 90

# A started run older than this is dead: the printer went offline mid-print
# and never came back, or a task died somewhere this module does not model.
# Closing it keeps the next attempt from being blocked forever by the
# one-run-per-printer index. The restart case does not wait for this clock --
# see reconcile_interrupted_runs.
STALE_ACTIVE_HOURS = 2
# A result nobody confirmed is not written. Sweeping it keeps the row out of
# the way without ever touching the printer.
STALE_AWAITING_HOURS = 24

# Printer states in which a print is running. Mirrors the MQTT layer's own set;
# importing it would drag that module into every caller of this one, and the
# list has not changed since the firmware introduced it.
_ACTIVE_PRINT_STATES = frozenset({"PREPARE", "SLICING", "RUNNING", "PAUSE"})

# Statuses during which the run owns the printer. Deliberately NOT the whole
# active set: a `queued` run has not committed to anything yet (reserving there
# would deadlock against the very queue it waits for), and by
# `awaiting_confirmation` the print has finished -- holding a farm printer for
# up to 24 hours waiting on a click would be worse than anything it protects.
RESERVING_STATUSES: frozenset[str] = frozenset({"slicing", "uploading", "printing", "reading_result"})

# Statuses that only ever move forward because a background task is driving
# them. No coroutine survives a process restart, and nothing re-spawns one, so
# a row found in one of these at startup has nothing behind it -- and every one
# of them is a RESERVING_STATUS, holding the printer off the print queue while
# it sits there.
TASK_DRIVEN_STATUSES: frozenset[str] = frozenset({"slicing", "uploading", "reading_result"})

_INTERRUPTED_MESSAGES = {
    "slicing": "Bambuddy restarted while the calibration job was being sliced. Nothing reached the printer.",
    "uploading": "Bambuddy restarted while the calibration job was being uploaded.",
    "reading_result": (
        "Bambuddy restarted before the measurement could be read back. Nothing was written to the printer."
    ),
}


class PaCalibrationError(Exception):
    """A run cannot continue. The message goes in front of the user."""


def run_to_response(row: PaCalibrationRun) -> dict:
    """The API shape of a run.

    Hand-maintained on purpose, like ``archive_to_response`` and
    ``_provider_to_dict``: a new column has to be added here too, or it
    silently never reaches the client.
    """
    return {
        "id": row.id,
        "printer_id": row.printer_id,
        "ams_id": row.ams_id,
        "slot_id": row.slot_id,
        "tray_id": row.tray_id,
        "extruder_id": row.extruder_id,
        "filament_id": row.filament_id,
        "filament_name": row.filament_name,
        "nozzle_diameter": row.nozzle_diameter,
        "nozzle_id": row.nozzle_id,
        "plate_type": row.plate_type,
        "plate_confirmed": row.plate_confirmed,
        "method": row.method,
        "status": row.status,
        "stage": row.stage,
        "waiting_reason": row.waiting_reason,
        "waiting_detail": row.waiting_detail,
        "progress": row.progress or 0.0,
        "k_before": row.k_before,
        "k_value": row.k_value,
        "n_coef": row.n_coef,
        "confidence": row.confidence,
        "error_message": row.error_message,
        "created_at": row.created_at,
        "started_at": row.started_at,
        "completed_at": row.completed_at,
    }


async def resolve_slicer_url(db: AsyncSession) -> str:
    """The sidecar this install slices with.

    Read exactly the way the library slice route reads it -- ``preferred_slicer``
    picks which of the two URL settings applies, and an empty setting falls back
    to the env default. Anything else would let a calibration slice go to a
    different sidecar than every other slice on the same install.
    """
    from backend.app.api.routes.settings import get_setting

    preferred = (await get_setting(db, "preferred_slicer")) or "bambu_studio"
    if preferred == "orcaslicer":
        configured = await get_setting(db, "orcaslicer_api_url")
        return (configured or app_settings.slicer_api_url).strip()
    if preferred == "bambu_studio":
        configured = await get_setting(db, "bambu_studio_api_url")
        return (configured or app_settings.bambu_studio_api_url).strip()
    return ""


def global_tray_id(ams_id: int, slot_id: int) -> int:
    """The global tray id for a slot, in the form ams_mapping wants.

    Mirrors ``_slot_preset_key`` and the frontend's ``getGlobalTrayId``: an
    AMS-HT unit (128-135) holds one slot and shares its id, the external spool
    is 254, everything else is ``ams_id * 4 + slot_id``.
    """
    if ams_id >= 254:
        return 254
    if 128 <= ams_id <= 135:
        return ams_id
    return ams_id * 4 + slot_id


def slot_is_external(ams_id: int) -> bool:
    return ams_id >= 254


def find_slot(state, ams_id: int, slot_id: int) -> dict | None:
    """The live tray dict for one slot, or None when it is not there.

    The external spool lives under ``vt_tray`` (which the MQTT layer normalises
    to a list, and which ``vir_slot`` may have replaced on H2 firmware); AMS
    slots live under ``ams``.
    """
    raw = getattr(state, "raw_data", None) or {}
    if slot_is_external(ams_id):
        # A dual-nozzle machine reports two external trays (254 = Ext-L,
        # 255 = Ext-R) and the UI addresses them as ams_id 255 with slot 0/1,
        # so the wanted entry has to be picked by id rather than taken first.
        wanted = 254 + slot_id if slot_id in (0, 1) else None
        trays = [tray for tray in raw.get("vt_tray") or [] if isinstance(tray, dict)]
        if wanted is not None and len(trays) > 1:
            for tray in trays:
                try:
                    if int(tray.get("id", -1)) == wanted:
                        return tray
                except (TypeError, ValueError):
                    continue
        return trays[0] if trays else None
    for unit in raw.get("ams") or []:
        if not isinstance(unit, dict):
            continue
        try:
            if int(unit.get("id", -1)) != ams_id:
                continue
        except (TypeError, ValueError):
            continue
        for tray in unit.get("tray") or []:
            if not isinstance(tray, dict):
                continue
            try:
                if int(tray.get("id", -1)) == slot_id:
                    return tray
            except (TypeError, ValueError):
                continue
    return None


def slot_filament_id(tray: dict | None) -> str:
    return str((tray or {}).get("tray_info_idx") or "").strip()


def slot_filament_name(tray: dict | None) -> str:
    """A human label for the slot's filament, best effort.

    ``tray_sub_brands`` is what the AMS card shows ("PLA Matte"); the bare
    ``tray_type`` is the fallback. Display only, plus the *last* fallback for
    the profile name when a run creates one -- an overwrite always reuses the
    existing profile's name, because that is the one the user chose.
    """
    tray = tray or {}
    for key in ("tray_sub_brands", "tray_id_name", "tray_type"):
        value = str(tray.get(key) or "").strip()
        if value:
            return value
    return ""


def nozzle_id_for_extruder(state, extruder_id: int) -> str | None:
    """The fitted nozzle's id in calibration-table form, e.g. ``HS01-0.4``.

    Built from the same push_status fields the K-profile list is matched
    against -- ``nozzle_type`` ("HS01") plus ``nozzle_diameter``. Never from
    ``get_accessories``, which reports stale nozzle data on H2D.
    """
    nozzles = getattr(state, "nozzles", None) or []
    if extruder_id >= len(nozzles):
        extruder_id = 0
    if extruder_id >= len(nozzles):
        return None
    nozzle = nozzles[extruder_id]
    kind = str(getattr(nozzle, "nozzle_type", "") or "").strip().upper()
    diameter = str(getattr(nozzle, "nozzle_diameter", "") or "").strip()
    if not kind or not diameter:
        return None
    return f"{kind}-{diameter}"


def blocking_reasons(
    *,
    printer: Printer,
    state,
    ams_id: int,
    slot_id: int,
    extruder_id: int,
    slicer_url: str,
    has_active_run: bool,
    printer_reserved: bool,
) -> list[str]:
    """Why Start cannot be pressed, as i18n tokens. Empty means it can.

    Every reason the UI could ever show is produced here rather than inferred
    in the frontend, so a disabled button always has a sentence next to it.
    """
    reasons: list[str] = []
    if not supports_sliced_pa_calibration(printer.model):
        # Nothing below is worth evaluating -- the feature does not exist for
        # this machine, and listing four more blockers would suggest it might.
        return ["model_not_supported"]
    if state is None or not getattr(state, "connected", False):
        return ["printer_offline"]
    if getattr(state, "state", None) in _ACTIVE_PRINT_STATES:
        reasons.append("printer_busy")
    if printer_reserved:
        reasons.append("printer_reserved")
    if has_active_run:
        reasons.append("run_already_active")
    if not slicer_url:
        reasons.append("slicer_not_configured")

    tray = find_slot(state, ams_id, slot_id)
    if tray is None or not slot_filament_id(tray):
        reasons.append("slot_empty")
    if not nozzle_id_for_extruder(state, extruder_id):
        reasons.append("nozzle_unknown")
    return reasons


def set_stage(row: PaCalibrationRun, status: str, *, progress: float | None = None) -> None:
    row.status = status
    row.stage = status
    row.waiting_reason = None
    row.waiting_detail = None
    if progress is not None:
        row.progress = progress


def fail_run(row: PaCalibrationRun, message: str) -> None:
    """End a run as failed, keeping the stage it got to."""
    row.status = "failed"
    row.error_message = message
    row.waiting_reason = None
    row.completed_at = utcnow_naive()
    logger.warning("PA calibration run %s failed at %s: %s", row.id, row.stage, message)


async def cleanup_remote_file(printer: Printer, row: PaCalibrationRun) -> None:
    """Delete the uploaded job from the printer. Best effort, always.

    Called on every terminal state. A file left behind becomes a ghost print on
    the next power-on for some firmware, and there is nothing the user could do
    about a failure here, so it is logged rather than surfaced.
    """
    if not row.remote_filename or not printer.ip_address:
        return
    try:
        await delete_file_async(
            printer.ip_address,
            printer.access_code,
            f"/{row.remote_filename}",
            printer_model=printer.model,
            respect_handshake_cooloff=False,
        )
    except Exception as exc:  # noqa: BLE001 - cleanup must never mask the outcome
        logger.info("PA calibration run %s: could not remove %s: %s", row.id, row.remote_filename, exc)


def _patch_bed_type(process_json: str, bed_type: str) -> str:
    """Write ``curr_bed_type`` onto the process preset, as the slice route does."""
    from backend.app.api.routes.library import _patch_process_bed_type

    return _patch_process_bed_type(process_json, bed_type)


def preset_nozzle_diameter(printer_json: str) -> str | None:
    """The nozzle diameter a resolved printer preset slices for, or None.

    Bambu presets carry it as a one-element list (``["0.4"]``); a flattened
    string or a number is accepted too. None means the preset does not say,
    and the caller must not turn "cannot tell" into a refusal.
    """
    try:
        data = json.loads(printer_json or "")
    except (TypeError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    value = data.get("nozzle_diameter")
    if isinstance(value, list):
        value = value[0] if value else None
    if value is None or isinstance(value, bool):
        return None
    text = str(value).strip()
    if not text:
        return None
    try:
        # "0.40" and "0.4" are the same nozzle; compare as numbers, render the
        # way the rest of the feature spells a diameter.
        return f"{float(text):g}"
    except ValueError:
        return None


def nozzle_diameter_mismatch(expected: str, found: str | None) -> bool:
    """Whether a diameter from a preset or a sliced file contradicts the run's.

    ``None`` is not a contradiction -- an older sidecar's preset may not carry
    the field at all, and refusing on a missing value would fail runs that are
    perfectly correct.
    """
    if not found:
        return False
    try:
        return float(found) != float(expected)
    except (TypeError, ValueError):
        return False


async def slice_calibration_job(db: AsyncSession, row: PaCalibrationRun, *, on_progress=None) -> bytes:
    """Slice the calibration job, or raise ``PaCalibrationError``.

    Returns the 3MF bytes only when all three guards pass. Nothing is written
    to disk, nothing is uploaded and no MQTT command is published here -- that
    separation is the point: every way a slice can go wrong ends in this
    function, before the printer has heard of the run.
    """
    from backend.app.schemas.slicer import PresetRef
    from backend.app.services.preset_resolver import resolve_preset_ref
    from backend.app.services.process_overrides import apply_process_overrides

    api_url = await resolve_slicer_url(db)
    if not api_url:
        raise PaCalibrationError(
            "No slicer sidecar is configured. This calibration is a sliced print, so it needs one."
        )

    presets = row.presets or {}
    try:
        printer_ref = PresetRef(**presets["printer"])
        process_ref = PresetRef(**presets["process"])
        filament_ref = PresetRef(**presets["filament"])
    except (KeyError, TypeError, ValueError) as exc:
        raise PaCalibrationError(f"The preset selection for this run is unusable: {exc}") from exc

    printer_json = await resolve_preset_ref(db, None, printer_ref, "printer")
    process_json = await resolve_preset_ref(db, None, process_ref, "process")
    filament_json = await resolve_preset_ref(db, None, filament_ref, "filament")

    # Precondition 7, on the half the live nozzle check cannot see. The run
    # records the diameter the printer reports as fitted; the measurement is
    # run by "M983.3 F... A{nozzle_diameter}" expanded from the PRINTER PRESET.
    # Both sides of the existing check come from push_status, so a user who
    # edits the preset field to another machine's nozzle gets a file that
    # measures 0.6 through a 0.4 nozzle and a result filed under 0.4. Checked
    # before the slice, because a slice that cannot be used is worth nobody's
    # minute.
    preset_diameter = preset_nozzle_diameter(printer_json)
    if nozzle_diameter_mismatch(row.nozzle_diameter, preset_diameter):
        raise PaCalibrationError(
            f"The printer preset '{printer_ref.id}' slices for a {preset_diameter} mm nozzle, "
            f"but a {row.nozzle_diameter} mm nozzle is fitted. The measurement would be run for "
            "the wrong nozzle and stored against this one."
        )

    # The plate the user confirmed is on the bed has to reach the SLICER, not
    # the dispatch: ``curr_bed_type`` inside the G-code decides the texture
    # trim (G29.1 Z-0.01), while the project_file's own bed_type field is
    # "auto" for every Bambuddy print.
    process_json = _patch_bed_type(process_json, row.plate_type)
    process_json = apply_process_overrides(process_json, PA_PROCESS_OVERRIDES)

    model_bytes = PA_MODEL_PATH.read_bytes()
    service = SlicerApiService(api_url, timeout_seconds=await get_stall_timeout_seconds(db))
    try:
        result = await service.slice_with_profiles(
            model_bytes=model_bytes,
            model_filename=PA_MODEL_FILENAME,
            printer_profile_json=printer_json,
            process_profile_json=process_json,
            filament_profile_jsons=[filament_json],
            plate=1,
            export_3mf=True,
            arrange=False,
            orient=False,
            request_id=str(uuid.uuid4()),
            on_progress=on_progress,
        )
    except SlicerApiError as exc:
        raise PaCalibrationError(str(exc)) from exc
    finally:
        await service.close()

    content = result.content
    preset_name = str(printer_ref.id)
    if start_gcode_is_missing(content, export_3mf=True):
        raise PaCalibrationError(missing_start_gcode_message(preset_name))

    # For an ordinary print an unresolved filament preset is a warning and the
    # file is kept. Here it is fatal: a slice that fell back to PLA at 200 C
    # measures the wrong filament at the wrong temperature, and the number
    # would then be written to the printer as if it were true.
    unresolved = unresolved_filament_slots(content, export_3mf=True)
    if unresolved:
        raise PaCalibrationError(unresolved_filament_message(unresolved, [str(filament_ref.id)]))

    if extrude_cali_gate_missing(content, export_3mf=True):
        raise PaCalibrationError(missing_extrude_cali_message(preset_name))

    # The same check again, this time against what the slicer actually wrote:
    # the A argument of the expanded M983.3 is the diameter the printer will
    # measure for. Belt and braces with the preset check above, and the only
    # one of the two that still holds if a sidecar resolves the preset
    # differently from how it reports it.
    for sliced in sorted(extrude_cali_nozzle_diameters(content, export_3mf=True)):
        if nozzle_diameter_mismatch(row.nozzle_diameter, sliced):
            raise PaCalibrationError(
                f"The sliced file measures a {sliced} mm nozzle, but a {row.nozzle_diameter} mm "
                f"nozzle is fitted. It was discarded rather than printed."
            )

    return content


async def upload_calibration_job(printer: Printer, row: PaCalibrationRun, content: bytes) -> None:
    """Put the sliced job on the printer's SD root, or raise.

    Deletes first: the firmware answers 553 to an overwrite, which is the same
    reason the print dispatch deletes before uploading.

    ``remote_filename`` is recorded before the transfer rather than after it.
    A name only written on success would make every cleanup path skip a
    transfer that got half-way, leaving a truncated 3MF in the printer's root.
    """
    _, _, _, ftp_timeout = await get_ftp_retry_settings()
    remote = f"/{PA_REMOTE_FILENAME}"
    row.remote_filename = PA_REMOTE_FILENAME
    try:
        await delete_file_async(
            printer.ip_address,
            printer.access_code,
            remote,
            socket_timeout=ftp_timeout,
            printer_model=printer.model,
            respect_handshake_cooloff=False,
        )
    except Exception as exc:  # noqa: BLE001 - "not there" is the normal case
        logger.debug("PA calibration run %s: pre-delete of %s: %s", row.id, remote, exc)

    failure = FtpFailureReport()
    with tempfile.TemporaryDirectory(prefix="bambuddy-pa-") as tmpdir:
        local = Path(tmpdir) / PA_REMOTE_FILENAME
        local.write_bytes(content)
        try:
            uploaded = await upload_file_async(
                printer.ip_address,
                printer.access_code,
                local,
                remote,
                socket_timeout=ftp_timeout,
                printer_model=printer.model,
                respect_handshake_cooloff=False,
                failure=failure,
            )
        except UploadCancelled as exc:
            raise PaCalibrationError(
                "Upload was too slow to finish and was cancelled. Check the printer's Wi-Fi signal."
            ) from exc
        except Exception as exc:  # noqa: BLE001 - any transport error ends the run
            raise PaCalibrationError(f"Upload failed: {exc}") from exc

    if not uploaded:
        raise PaCalibrationError(describe_upload_failure(failure.failure))


def recheck_preconditions(printer: Printer, row: PaCalibrationRun) -> str | None:
    """Re-verify everything immediately before dispatch. None means go.

    Run twice on purpose -- once when the run is picked up, once here -- because
    minutes pass in between while the file is sliced and uploaded, and in that
    window a user can start a print, pull the spool or swap the nozzle.
    """
    if not supports_sliced_pa_calibration(printer.model):
        return "This printer model cannot measure pressure advance by printing."
    state = printer_manager.get_status(row.printer_id)
    if state is None or not state.connected:
        return "The printer went offline."
    if state.state in _ACTIVE_PRINT_STATES:
        return "The printer started another job."

    tray = find_slot(state, row.ams_id, row.slot_id)
    current = slot_filament_id(tray)
    if not current:
        return "The slot is empty."
    if current != row.filament_id:
        return f"The filament in the slot changed ({row.filament_id} -> {current})."

    fitted = nozzle_id_for_extruder(state, row.extruder_id)
    if not fitted:
        return "The printer did not report which nozzle is fitted."
    diameter = fitted.rsplit("-", 1)[-1]
    if diameter != row.nozzle_diameter:
        # The G-code carries A{nozzle_diameter} from the sliced preset, so a
        # nozzle swap between slicing and dispatch would calibrate one diameter
        # with another's parameters.
        return f"The nozzle changed ({row.nozzle_diameter} -> {diameter})."
    return None


def start_calibration_print(row: PaCalibrationRun) -> bool:
    """Dispatch the sliced job with flow-dynamics calibration forced on.

    ``flow_cali="on"`` is the only way to put ``extrude_cali_flag: 1`` on the
    wire: the field is not independently settable, and "on" also sets
    ``flow_cali: true`` -- which is exactly the pair Studio sent.

    ``bed_levelling="off"`` yields ``auto_bed_leveling: 0``, selecting the
    ``g29_before_print_flag`` J0 branch: a plain ``G28`` with no mesh, the same
    branch Studio's job took.
    """
    return printer_manager.start_print(
        row.printer_id,
        row.remote_filename or PA_REMOTE_FILENAME,
        plate_id=1,
        ams_mapping=[row.tray_id],
        bed_levelling="off",
        flow_cali="on",
        vibration_cali=False,
        layer_inspect=True,
        timelapse=False,
        use_ams=not slot_is_external(row.ams_id),
        nozzle_offset_cali="off",
    )


def pick_result_entry(response: dict, *, filament_id: str, extruder_id: int) -> dict:
    """The one ``filaments[]`` entry this run measured, or raise.

    Matched on ``filament_id`` **and** ``extruder_id``. More than one match is
    as fatal as none: writing a K to a nozzle it was not measured on is silent,
    and a coin flip is not a way to choose. Both captured runs returned exactly
    one entry.
    """
    result = str(response.get("result", "")).lower()
    if result != "success":
        reason = str(response.get("reason") or "").strip()
        raise PaCalibrationError(reason or "The printer reported no calibration result.")

    entries = response.get("filaments") or []
    matches = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        if str(entry.get("filament_id") or "") != filament_id:
            continue
        try:
            entry_extruder = int(entry.get("extruder_id") or 0)
        except (TypeError, ValueError):
            continue
        if entry_extruder == extruder_id:
            matches.append(entry)

    if not matches:
        raise PaCalibrationError(f"The printer returned no calibration result for {filament_id or 'this filament'}.")
    if len(matches) > 1:
        raise PaCalibrationError(
            f"The printer returned {len(matches)} calibration results for {filament_id}; "
            "refusing to guess which one was measured."
        )
    entry = matches[0]
    if not str(entry.get("n_coef") or "").strip():
        # Never invent one. n_coef is part of what the profile means, and the
        # printer is the only source for it.
        raise PaCalibrationError("The calibration result carries no n_coef.")
    return entry


def build_write_payload(row: PaCalibrationRun, *, profile_name: str) -> dict:
    """The ``filaments[]`` entry for ``extrusion_cali_set``, from a finished run."""
    entry = row.result_raw or {}
    return build_extrusion_cali_set_filament(
        ams_id=row.ams_id,
        slot_id=row.slot_id,
        extruder_id=row.extruder_id,
        filament_id=row.filament_id,
        k_value=round_k_value(entry.get("k_value", row.k_value)),
        n_coef=str(entry.get("n_coef") or row.n_coef or ""),
        name=profile_name,
        nozzle_diameter=row.nozzle_diameter,
        nozzle_id=write_nozzle_id(str(entry.get("nozzle_id") or row.nozzle_id or "")),
    )


async def printer_reserved_elsewhere(db: AsyncSession, printer_id: int) -> bool:
    """Whether the print queue or a drying cycle has already claimed this printer.

    The other direction of the reservation, asked through the scheduler
    because it owns all four facts -- a queue row printing or claimed, an
    upload still in flight, a post-dispatch hold, drying -- and none of them
    is visible in ``gcode_state``. Imported here rather than at module scope:
    the scheduler imports this module.
    """
    from backend.app.services.print_scheduler import scheduler

    return printer_id in await scheduler.printers_reserved_elsewhere(db)


async def active_run_printer_ids(db: AsyncSession) -> set[int]:
    """Printers currently held by a calibration run.

    Read by the print dispatch and by scheduled drying so their cards can say
    "waiting for a flow-dynamics calibration" instead of a bare "printer busy".
    """
    rows = await db.execute(
        select(PaCalibrationRun.printer_id).where(PaCalibrationRun.status.in_(sorted(RESERVING_STATUSES)))
    )
    return {pid for (pid,) in rows.all() if pid is not None}


async def sweep_stale_runs(db: AsyncSession, *, spawn=None) -> int:
    """Close runs that cannot finish, so the next attempt is not blocked.

    Two clocks, two outcomes. A run that has started and is older than two
    hours is dead -- a task that died mid-slice, a printer that went offline
    mid-print -- and is failed. A run waiting for confirmation is not dead,
    only unanswered; after a day it is cancelled, and cancelled means *nothing
    was written*.

    A ``queued`` run has neither clock. It is not stale, it is queued: it holds
    no printer, it has a reason on it saying what it is waiting for, and a
    queued *print* behind a six-hour job does not expire after two hours
    either. Failing it would contradict the deferral-never-drop discipline the
    rest of this module is built on, and the message would be a lie.

    The uploaded job is removed from the printer for anything swept while it
    still held one -- ``spawn`` defers that to after the caller's commit, so a
    scheduler tick never waits on FTP; calling without it deletes inline.
    """
    now = utcnow_naive()
    swept = 0
    orphaned: list[PaCalibrationRun] = []
    result = await db.execute(select(PaCalibrationRun).where(PaCalibrationRun.status.in_(ACTIVE_PA_STATUSES)))
    for row in result.scalars():
        reference = row.started_at or row.created_at
        if reference is None or row.status == "queued":
            continue
        if row.status == "awaiting_confirmation":
            if now - reference > timedelta(hours=STALE_AWAITING_HOURS):
                row.status = "cancelled"
                row.completed_at = now
                row.error_message = "No answer within 24 hours - nothing was written to the printer."
                swept += 1
            continue
        if now - reference > timedelta(hours=STALE_ACTIVE_HOURS):
            if row.remote_filename:
                orphaned.append(row)
            fail_run(row, "The run stopped making progress and was closed after two hours.")
            swept += 1

    if orphaned:
        printer_rows = await db.execute(select(Printer).where(Printer.id.in_({r.printer_id for r in orphaned})))
        printers = {p.id: p for p in printer_rows.scalars()}
        for row in orphaned:
            printer = printers.get(row.printer_id)
            if printer is None:
                continue
            if spawn is not None:
                spawn(cleanup_remote_file(printer, row), name=f"pa-calibration-sweep-cleanup-{row.id}")
            else:
                await cleanup_remote_file(printer, row)
    return swept


# ---------------------------------------------------------------------------
# The run state machine
#
# Driven by the scheduler's tick, which owns the gating decisions, plus two
# background tasks for the parts that block for minutes (slice + upload +
# dispatch, then the result read-back). The tick itself never blocks:
# everything it does is a DB read, a live-state read and at most one spawn.
# ---------------------------------------------------------------------------


async def notify(event: str, row: PaCalibrationRun, printer_name: str, detail: str = "") -> None:
    """Tell the user where a run got to. Never lets a notification fail a run."""
    from backend.app.core.database import async_session
    from backend.app.services.notification_service import notification_service

    try:
        async with async_session() as db:
            await notification_service.on_pa_calibration(
                printer_id=row.printer_id,
                printer_name=printer_name,
                event=event,
                filament=row.filament_name or row.filament_id,
                k_value=row.k_value,
                detail=detail,
                db=db,
            )
    except Exception as exc:  # noqa: BLE001 - a notification is never worth a run
        logger.debug("PA calibration run %s: notification %s failed: %s", row.id, event, exc)


async def finish_terminal(row: PaCalibrationRun, printer: Printer, *, event: str, detail: str = "") -> None:
    """Common tail for every terminal state: clean the printer, tell the user."""
    await cleanup_remote_file(printer, row)
    await notify(event, row, printer.name, detail)


async def run_status_now(db: AsyncSession, run_id: int) -> str | None:
    """The run's status as the database has it, not as this session remembers it.

    A background task loads its row once and then works for minutes. The cancel
    route writes from the request's own session, and the sessionmaker is
    ``expire_on_commit=False``, so this session's copy still says what it said
    when the task started -- and its next ``commit`` would blindly write that
    stale status back over the cancel, reviving a run the user stopped.

    ``no_autoflush`` is load-bearing: the row is dirty while this runs (the
    slice's progress callback writes to it), and an autoflush on the way out
    would push exactly the value this read exists to question.
    """
    with db.no_autoflush:
        result = await db.execute(select(PaCalibrationRun.status).where(PaCalibrationRun.id == run_id))
    return result.scalar_one_or_none()


async def stopped_meanwhile(db: AsyncSession, run_id: int, printer: Printer, expected: str) -> bool:
    """Whether this run stopped being ours while the last step ran.

    Cancelled from the route, or closed by the sweep. Rolls back whatever this
    session was about to write -- committing it would blindly revive the run
    and carry on to ``start_print`` -- and removes whatever was uploaded
    before it stopped.
    """
    if await run_status_now(db, run_id) == expected:
        return False
    await db.rollback()
    logger.info("PA calibration run %s: no longer %s, stopping before the printer is touched", run_id, expected)
    row = await db.get(PaCalibrationRun, run_id)
    if row is not None:
        await cleanup_remote_file(printer, row)
    return True


async def close_stranded_run(run_id: int, message: str) -> None:
    """Fail a run whose driving task died, in a session of its own.

    The session the task was using may be the reason it died, so this takes a
    fresh one. Nothing here may raise: it is already the error path.
    """
    from backend.app.core.database import async_session

    try:
        async with async_session() as db:
            row = await db.get(PaCalibrationRun, run_id)
            if row is None or row.status not in ACTIVE_PA_STATUSES:
                return
            printer = await db.get(Printer, row.printer_id)
            fail_run(row, message)
            await db.commit()
            if printer is not None:
                await finish_terminal(row, printer, event="failed", detail=message)
    except Exception:  # noqa: BLE001 - the error path may not have an error path
        logger.exception("PA calibration run %s: could not close a stranded run", run_id)


async def reconcile_interrupted_runs() -> int:
    """Close runs whose driving task did not survive a restart.

    ``slicing``, ``uploading`` and ``reading_result`` only ever move forward
    because a background task is pushing them, and no coroutine survives a
    process restart -- the same fact the queue clears its dispatch claims on at
    startup (#2615). Nothing re-spawns these, so without this a run interrupted
    mid-slice sits there until the two-hour sweep, and for those two hours
    every queued job for its printer is held with "running a flow-dynamics
    calibration" while nothing is running and nothing is being sliced.

    Failing rather than resuming: a run that was interrupted before dispatch
    was authorised by a person who ticked "the build plate is empty" some time
    ago, and silently starting a print on their plate after a container
    restart is not a recovery, it is a surprise.
    """
    from backend.app.core.database import async_session

    closed = 0
    try:
        async with async_session() as db:
            result = await db.execute(
                select(PaCalibrationRun).where(PaCalibrationRun.status.in_(sorted(TASK_DRIVEN_STATUSES)))
            )
            rows = list(result.scalars().all())
            if not rows:
                return 0
            printer_rows = await db.execute(select(Printer).where(Printer.id.in_({r.printer_id for r in rows})))
            printers = {p.id: p for p in printer_rows.scalars()}
            interrupted = []
            for row in rows:
                message = _INTERRUPTED_MESSAGES[row.status]
                fail_run(row, message)
                interrupted.append((row, message))
                closed += 1
            await db.commit()
            for row, message in interrupted:
                printer = printers.get(row.printer_id)
                if printer is not None:
                    await finish_terminal(row, printer, event="failed", detail=message)
    except Exception:  # noqa: BLE001 - startup must not fail on this
        logger.exception("Could not reconcile interrupted flow-dynamics calibration runs")
    if closed:
        logger.info("Closed %d flow-dynamics calibration run(s) interrupted by a restart", closed)
    return closed


async def run_slice_and_dispatch(run_id: int) -> None:
    """Slice, guard, upload, re-check, dispatch. One run, up to printing.

    Wrapped so that nothing can escape. Anything this path raises that is not a
    ``PaCalibrationError`` -- an ``HTTPException`` out of ``resolve_preset_ref``
    for a preset ref it cannot serve, an ``OSError`` reading the model, a
    transport error outside a guarded block -- would otherwise leave the row in
    ``slicing`` with no task behind it, holding the printer off the print queue
    until the two-hour sweep.
    """
    try:
        await _slice_and_dispatch(run_id)
    except Exception:
        logger.exception("PA calibration run %s: preparing the calibration print failed", run_id)
        await close_stranded_run(run_id, "Preparing the calibration print failed unexpectedly.")


async def _slice_and_dispatch(run_id: int) -> None:
    """The body of ``run_slice_and_dispatch``.

    Owns its own session: it runs for minutes and must not hold the scheduler's
    pooled connection while an FTP transfer is in flight, which is the same
    reason the queue's own dispatch commits before uploading.
    """
    from backend.app.core.database import async_session

    async with async_session() as db:
        row = await db.get(PaCalibrationRun, run_id)
        if row is None or row.status != "slicing":
            return
        printer = await db.get(Printer, row.printer_id)
        if printer is None:
            fail_run(row, "The printer was deleted.")
            await db.commit()
            return

        def on_progress(snapshot: dict) -> None:
            try:
                percent = float(snapshot.get("percent") or snapshot.get("progress") or 0)
            except (TypeError, ValueError):
                return
            row.progress = max(0.0, min(100.0, percent))

        try:
            content = await slice_calibration_job(db, row, on_progress=on_progress)
        except PaCalibrationError as exc:
            fail_run(row, str(exc))
            await db.commit()
            # Nothing was uploaded and nothing published, so there is no remote
            # file to remove -- which is the whole point of failing here.
            await notify("failed", row, printer.name, str(exc))
            return

        # Cancelled while the slice ran? Nothing has been uploaded and nothing
        # published, so stopping is the whole of it.
        if await stopped_meanwhile(db, run_id, printer, "slicing"):
            return

        set_stage(row, "uploading", progress=0.0)
        # Committed before the transfer starts, so both this task and the
        # cancel route know which file to remove if the upload is interrupted
        # or only gets half-way.
        row.remote_filename = PA_REMOTE_FILENAME
        await db.commit()

        try:
            await upload_calibration_job(printer, row, content)
        except PaCalibrationError as exc:
            fail_run(row, str(exc))
            await db.commit()
            await finish_terminal(row, printer, event="failed", detail=str(exc))
            return

        # Cancelled during the upload? The file is on the printer by now, or
        # partly so, and it goes with the run.
        if await stopped_meanwhile(db, run_id, printer, "uploading"):
            return

        # Both re-checks first, then one last look at the run itself, then
        # the branch that acts -- with nothing awaited in between. Asking
        # about the run before these would let their conclusion be written
        # over a cancel that landed while they ran, which is the whole shape
        # of the bug this guards.
        blocked = recheck_preconditions(printer, row)
        reserved = False if blocked else await printer_reserved_elsewhere(db, row.printer_id)
        if await stopped_meanwhile(db, run_id, printer, "uploading"):
            return

        if blocked:
            fail_run(row, blocked)
            await db.commit()
            await finish_terminal(row, printer, event="failed", detail=blocked)
            return

        if reserved:
            # The queue claimed the printer while this was being sliced and
            # uploaded. Deferral, not failure: the user asked for a
            # calibration, not for one attempt at one. The uploaded file stays
            # where it is -- the next attempt deletes before uploading, and a
            # cancel removes it by the name already on the row.
            set_stage(row, "queued", progress=0.0)
            row.waiting_reason = "printer_reserved"
            row.started_at = None
            await db.commit()
            logger.info(
                "PA calibration run %s: the print queue took printer %s during the upload; back to queued",
                row.id,
                row.printer_id,
            )
            return

        if not start_calibration_print(row):
            fail_run(row, "The printer refused the job (it is busy or not connected).")
            await db.commit()
            await finish_terminal(row, printer, event="failed", detail=row.error_message or "")
            return

        state = printer_manager.get_status(row.printer_id)
        row.dispatched_subtask_id = getattr(state, "dispatched_subtask", None) if state else None
        set_stage(row, "printing", progress=0.0)
        # The printer has not been seen printing yet. Until it is, a FINISH is
        # the previous job's, not ours; see ``print_finished``.
        row.print_started = False
        row.started_at = utcnow_naive()
        await db.commit()
        logger.info(
            "PA calibration run %s: dispatched %s to printer %s",
            row.id,
            row.remote_filename,
            row.printer_id,
        )


async def run_read_result(run_id: int, *, settle_seconds: float = 3.0) -> None:
    """Read the measurement back and park the run for confirmation.

    Nothing is written to the printer here. That is the whole point of the
    separate ``awaiting_confirmation`` state: the K value the printer measured
    does not become the K value the printer *uses* until a person says so.

    Wrapped for the same reason the dispatch task is: ``reading_result`` is a
    reserving status, so a task that dies inside it holds the printer off the
    print queue with nothing behind it.
    """
    try:
        await _read_result(run_id, settle_seconds=settle_seconds)
    except Exception:
        logger.exception("PA calibration run %s: reading the measurement failed", run_id)
        await close_stranded_run(run_id, "Reading the measurement back from the printer failed unexpectedly.")


async def _read_result(run_id: int, *, settle_seconds: float = 3.0) -> None:
    """The body of ``run_read_result``."""
    import asyncio

    from backend.app.core.database import async_session

    # Studio asks about three seconds after FINISH; the value is not ready the
    # instant the job ends.
    if settle_seconds > 0:
        await asyncio.sleep(settle_seconds)

    async with async_session() as db:
        row = await db.get(PaCalibrationRun, run_id)
        if row is None or row.status != "reading_result":
            return
        printer = await db.get(Printer, row.printer_id)
        if printer is None:
            fail_run(row, "The printer was deleted.")
            await db.commit()
            return

        client = printer_manager.get_client(row.printer_id)
        response = None
        if client is not None:
            response = await client.get_extrusion_cali_result(nozzle_diameter=row.nozzle_diameter)

        if not response:
            fail_run(row, "The printer returned no calibration result.")
            await db.commit()
            await finish_terminal(row, printer, event="failed", detail=row.error_message or "")
            return

        try:
            entry = pick_result_entry(response, filament_id=row.filament_id, extruder_id=row.extruder_id)
            k_value = float(entry["k_value"])
        except PaCalibrationError as exc:
            fail_run(row, str(exc))
            await db.commit()
            await finish_terminal(row, printer, event="failed", detail=str(exc))
            return
        except (KeyError, TypeError, ValueError):
            fail_run(row, "The calibration result carries no usable K value.")
            await db.commit()
            await finish_terminal(row, printer, event="failed", detail=row.error_message or "")
            return

        row.k_value = k_value
        row.n_coef = str(entry.get("n_coef") or "")
        try:
            raw_confidence = entry.get("confidence")
            row.confidence = None if raw_confidence is None else int(raw_confidence)
        except (TypeError, ValueError):
            row.confidence = None
        row.result_raw = entry
        set_stage(row, "awaiting_confirmation", progress=100.0)
        await db.commit()

        # The file has served its purpose and the run now waits on a person,
        # which can be a whole day. Clean up here rather than at the end.
        await cleanup_remote_file(printer, row)
        await notify("awaiting_confirmation", row, printer.name)
        logger.info(
            "PA calibration run %s: measured k=%s n_coef=%s confidence=%s",
            row.id,
            row.k_value,
            row.n_coef,
            row.confidence,
        )


def print_finished(state, row: PaCalibrationRun) -> bool:
    """Whether the calibration print we dispatched has ended successfully.

    Two things have to be true, and the first is what gives the second any
    meaning. The printer must have been *seen* printing since this run
    dispatched (``print_started``): ``gcode_state`` can sit at FINISH for the
    better part of a minute after the printer accepted ``project_file``
    (#1078, which is why the queue's own start watchdog accepts a subtask
    advance as well as a state change), and the remote filename is a constant
    -- so the FINISH the *previous* calibration on this printer left behind
    reports our own subtask name and would otherwise read as this run
    finishing, seconds after dispatch and before the printer has moved.

    Then the subtask name, whenever the printer reports one: a FINISH
    belonging to some other job -- a touchscreen reprint, a job Studio sent --
    must not be read as our measurement finishing. Only what the printer
    reports counts. ``state.dispatched_subtask`` is the value Bambuddy wrote
    itself at dispatch, so comparing against it can only ever agree.
    """
    if getattr(state, "state", None) != "FINISH":
        return False
    if not row.print_started:
        return False
    expected = row.dispatched_subtask_id
    observed = getattr(state, "subtask_name", None)
    if not expected or not observed:
        return True
    return str(observed) == str(expected)


async def tick(
    db: AsyncSession,
    *,
    require_plate_clear: bool = False,
    printer_busy_ids: set[int] | None = None,
) -> set[int]:
    """Advance every live calibration run. Returns the printers they hold.

    Called from the scheduler's queue pass, next to the drying check, so it
    inherits the same cadence and the same ``require_plate_clear`` setting.
    Deferral rather than failure is the discipline throughout: a run that
    cannot start right now keeps its row and its reason, exactly as a queued
    print does.
    """
    from backend.app.core.tasks import spawn_background_task

    busy = printer_busy_ids or set()
    # Every task this pass decides to start, spawned only after the commit
    # below. A task that started first would open its own session, read the
    # row as the database still has it -- `queued`, not `slicing` -- and
    # return, leaving a status nothing is driving.
    deferred: list[tuple[object, str | None]] = []

    def defer(coro, name: str | None = None) -> None:
        deferred.append((coro, name))

    async def commit_and_spawn() -> None:
        try:
            await db.commit()
        except Exception:
            # The decisions these tasks were to act on were not written, so
            # they must not run. Closing them also keeps a failed pass from
            # filling the log with "coroutine was never awaited".
            for pending, _name in deferred:
                pending.close()
            raise
        for pending, pending_name in deferred:
            spawn_background_task(pending, name=pending_name)

    await sweep_stale_runs(db, spawn=defer)

    result = await db.execute(
        select(PaCalibrationRun)
        .where(PaCalibrationRun.status.in_(ACTIVE_PA_STATUSES))
        .order_by(PaCalibrationRun.id.asc())
    )
    rows = list(result.scalars().all())
    if not rows:
        await commit_and_spawn()
        return set()

    printer_ids = {row.printer_id for row in rows}
    printer_rows = await db.execute(select(Printer).where(Printer.id.in_(printer_ids)))
    printers = {p.id: p for p in printer_rows.scalars()}

    now = utcnow_naive()
    for row in rows:
        printer = printers.get(row.printer_id)
        if printer is None:
            continue
        if row.status == "queued":
            tick_queued(
                row,
                printer,
                require_plate_clear=require_plate_clear,
                busy=busy,
                spawn=defer,
            )
        elif row.status == "printing":
            tick_printing(row, printer, now=now, spawn=defer)

    await commit_and_spawn()
    return {row.printer_id for row in rows if row.status in RESERVING_STATUSES}


def tick_queued(row, printer, *, require_plate_clear: bool, busy: set[int], spawn) -> None:
    """Gate a queued run, and start it when everything lines up.

    A failing model / slot / nozzle check fails the run -- those cannot resolve
    themselves, and a run that silently waits forever on one is worse than a
    run that says why it stopped. Everything else parks it with a reason.
    """
    if not supports_sliced_pa_calibration(printer.model):
        fail_run(row, "This printer model cannot measure pressure advance by printing.")
        return

    state = printer_manager.get_status(row.printer_id)
    if state is None or not state.connected:
        row.waiting_reason = "printer_offline"
        return
    if state.state in _ACTIVE_PRINT_STATES:
        row.waiting_reason = "printer_busy"
        return
    if row.printer_id in busy:
        row.waiting_reason = "printer_reserved"
        return
    if require_plate_clear and printer_manager.is_awaiting_plate_clear(row.printer_id):
        row.waiting_reason = "plate_not_cleared"
        return
    if not row.plate_confirmed:
        # The route refuses a run without the tick, so a row without one can
        # only come from an older schema or a direct DB write. Failing is the
        # only honest answer: this is the check that keeps a toolhead off a
        # plate nobody said was empty, and it cannot resolve itself.
        fail_run(row, "The build plate was never confirmed empty for this run.")
        return

    tray = find_slot(state, row.ams_id, row.slot_id)
    current = slot_filament_id(tray)
    if not current:
        fail_run(row, "The slot is empty.")
        return
    if current != row.filament_id:
        fail_run(row, f"The filament in the slot changed ({row.filament_id} -> {current}).")
        return

    fitted = nozzle_id_for_extruder(state, row.extruder_id)
    if not fitted:
        fail_run(row, "The printer did not report which nozzle is fitted.")
        return
    fitted_diameter = fitted.rsplit("-", 1)[-1]
    if fitted_diameter != row.nozzle_diameter:
        fail_run(row, f"The nozzle changed ({row.nozzle_diameter} -> {fitted_diameter}).")
        return

    set_stage(row, "slicing", progress=0.0)
    row.started_at = utcnow_naive()
    spawn(run_slice_and_dispatch(row.id), name=f"pa-calibration-dispatch-{row.id}")


def tick_printing(row, printer, *, now, spawn) -> None:
    """Watch a dispatched calibration print to its end.

    ``start_print`` returning True only means the command reached paho, and
    ``project_file`` has no ack, so the printer leaving idle within 90 seconds
    is the real confirmation that it got the job.
    """
    state = printer_manager.get_status(row.printer_id)
    if state is None:
        # Offline mid-print. Keep the row open -- the printer may come back and
        # the measurement still be there. The two-hour sweep closes it if not.
        row.waiting_reason = "printer_offline"
        return
    row.waiting_reason = None

    if state.state in _ACTIVE_PRINT_STATES:
        # The one observation that separates this run's FINISH from the one the
        # previous run left sitting on this printer. Latched, never cleared.
        row.print_started = True

    if print_finished(state, row):
        set_stage(row, "reading_result", progress=100.0)
        spawn(run_read_result(row.id), name=f"pa-calibration-result-{row.id}")
        return

    if state.state == "FAILED" or getattr(state, "print_error", 0):
        # No read-back. A K value from an aborted print is not a measurement.
        fail_run(row, "The calibration print did not finish.")
        spawn(
            finish_terminal(row, printer, event="failed", detail=row.error_message or ""),
            name=f"pa-calibration-cleanup-{row.id}",
        )
        return

    try:
        row.progress = max(0.0, min(100.0, float(getattr(state, "progress", 0) or 0)))
    except (TypeError, ValueError):
        pass

    started = row.started_at or row.created_at
    if (
        state.state not in _ACTIVE_PRINT_STATES
        and started is not None
        and (now - started).total_seconds() > START_WATCHDOG_SECONDS
    ):
        # Which of the two this is depends on the latch: a printer that was
        # never seen printing did not take the job, one that was has stopped
        # without reaching a FINISH we recognise.
        if row.print_started:
            fail_run(row, "The calibration print stopped before it finished.")
        else:
            fail_run(row, "The printer never started the calibration print.")
        spawn(
            finish_terminal(row, printer, event="failed", detail=row.error_message or ""),
            name=f"pa-calibration-cleanup-{row.id}",
        )
