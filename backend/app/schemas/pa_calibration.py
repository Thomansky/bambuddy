"""Pydantic schemas for flow-dynamics (pressure advance) calibration."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field

from backend.app.schemas.slicer import PresetRef


class PaPresetTriplet(BaseModel):
    """The printer / process / filament presets a run slices with."""

    printer: PresetRef
    process: PresetRef
    filament: PresetRef


class PaCalibrationFilament(BaseModel):
    """What the slot currently holds, as the preflight sees it."""

    filament_id: str = ""
    setting_id: str = ""
    name: str = ""
    material: str = ""
    colour: str = ""


class PaCalibrationPreflight(BaseModel):
    """Everything the modal needs to decide whether Start may be pressed.

    ``blocked_reasons`` is the whole answer: an empty list means supported and
    ready, and every entry is a token the frontend renders through i18n. A
    disabled Start with no reason is the one outcome this endpoint must never
    produce.
    """

    supported: bool
    blocked_reasons: list[str] = Field(default_factory=list)
    printer_model: str | None = None
    nozzle_diameter: str | None = None
    nozzle_id: str | None = None
    extruder_id: int = 0
    filament: PaCalibrationFilament | None = None
    current_k: float | None = None
    current_cali_idx: int | None = None
    current_profile_name: str | None = None
    presets: PaPresetTriplet | None = None
    plate_types: list[str] = Field(default_factory=list)
    default_plate_type: str | None = None
    estimated_seconds: int = 0
    estimated_grams: float = 0.0


class PaCalibrationRunCreate(BaseModel):
    ams_id: int
    slot_id: int
    plate_type: str
    presets: PaPresetTriplet
    # Not defaulted to True, and validated server-side: the run prints on the
    # bed, so a plate with parts on it gets hit. The user ticks this in the
    # modal and the answer is recorded on the row.
    plate_confirmed: bool = False


class PaCalibrationRunResponse(BaseModel):
    id: int
    printer_id: int
    ams_id: int
    slot_id: int
    tray_id: int
    extruder_id: int
    filament_id: str
    filament_name: str
    nozzle_diameter: str
    nozzle_id: str | None = None
    plate_type: str
    plate_confirmed: bool
    method: str
    status: str
    stage: str
    waiting_reason: str | None = None
    waiting_detail: dict | None = None
    progress: float
    k_before: float | None = None
    k_value: float | None = None
    n_coef: str | None = None
    confidence: int | None = None
    error_message: str | None = None
    created_at: datetime
    started_at: datetime | None = None
    completed_at: datetime | None = None
