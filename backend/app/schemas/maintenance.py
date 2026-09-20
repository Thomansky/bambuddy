"""Maintenance tracking schemas."""

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field, field_validator, model_validator

from backend.app.schemas.print_queue import UTCDatetime
from backend.app.services.maintenance_actions import (
    BED_TEMP_BELOW_KEY,
    CALIBRATION_FLAGS,
    TRIGGER_MODES,
    normalize_bed_temp_below,
    parse_schedule_time,
)

# Calibration flags (bool) plus the bed_temp_below start condition (float,
# degrees C), as stored on the item and reported by the overview (#3127).
ActionOptions = dict[str, bool | float]


# Maintenance Type schemas
class MaintenanceTypeBase(BaseModel):
    name: str = Field(..., min_length=1, max_length=100)
    description: str | None = None
    default_interval_hours: float = Field(default=100.0, ge=1.0)
    # "hours" = print hours, "days" = calendar days
    interval_type: str = Field(default="hours", pattern="^(hours|days)$")
    icon: str | None = None
    wiki_url: str | None = None  # Documentation link for custom types


class MaintenanceTypeCreate(MaintenanceTypeBase):
    pass


class MaintenanceTypeUpdate(BaseModel):
    name: str | None = None
    description: str | None = None
    default_interval_hours: float | None = Field(default=None, ge=1.0)
    interval_type: str | None = Field(default=None, pattern="^(hours|days)$")
    icon: str | None = None
    wiki_url: str | None = None


class MaintenanceTypeResponse(MaintenanceTypeBase):
    id: int
    is_system: bool
    # "calibration" when Bambuddy can perform the task itself (#3127)
    action: str | None = None
    created_at: datetime

    class Config:
        from_attributes = True


# Printer Maintenance schemas
class PrinterMaintenanceBase(BaseModel):
    printer_id: int
    maintenance_type_id: int
    custom_interval_hours: float | None = None
    enabled: bool = True


class PrinterMaintenanceCreate(PrinterMaintenanceBase):
    pass


def _validate_schedule_days(days: list[int] | None) -> list[int] | None:
    if days is None:
        return None
    if any(d < 0 or d > 6 for d in days):
        raise ValueError("schedule_days must contain weekdays 0 (Monday) to 6 (Sunday)")
    if len(set(days)) != len(days):
        raise ValueError("schedule_days must not repeat a weekday")
    return sorted(days)


def _validate_schedule_time(value: str | None) -> str | None:
    if value is None:
        return None
    if parse_schedule_time(value) is None:
        raise ValueError("schedule_time must be HH:MM")
    hour, minute = parse_schedule_time(value)
    return f"{hour:02d}:{minute:02d}"


class PrinterMaintenanceUpdate(BaseModel):
    custom_interval_hours: float | None = None
    custom_interval_type: str | None = Field(default=None, pattern="^(hours|days)$")
    enabled: bool | None = None
    # Automatic action settings (#3127); only meaningful on items whose type
    # carries an action. Cross-field rules (schedule needs days and time, a
    # non-manual trigger needs at least one flag) are checked in the route,
    # where the stored values fill in whatever the PATCH leaves out.
    # action_options is the whole new set: the flags the client sends (the
    # route fills the rest in as off) plus bed_temp_below, where null or a
    # missing key clears the condition.
    action_options: dict[str, Any] | None = None
    trigger_mode: str | None = Field(default=None, pattern=f"^({'|'.join(TRIGGER_MODES)})$")
    schedule_days: list[int] | None = None
    schedule_time: str | None = None

    @field_validator("action_options")
    @classmethod
    def _known_options_only(cls, value: dict[str, Any] | None) -> dict[str, Any] | None:
        if value is None:
            return None
        unknown = sorted(set(value) - set(CALIBRATION_FLAGS) - {BED_TEMP_BELOW_KEY})
        if unknown:
            raise ValueError(f"Unknown calibration option(s): {', '.join(unknown)}")
        checked: dict[str, Any] = {flag: bool(value[flag]) for flag in CALIBRATION_FLAGS if flag in value}
        if BED_TEMP_BELOW_KEY in value:
            checked[BED_TEMP_BELOW_KEY] = normalize_bed_temp_below(value[BED_TEMP_BELOW_KEY])
        return checked

    @field_validator("schedule_days")
    @classmethod
    def _weekdays(cls, value: list[int] | None) -> list[int] | None:
        return _validate_schedule_days(value)

    @field_validator("schedule_time")
    @classmethod
    def _hh_mm(cls, value: str | None) -> str | None:
        return _validate_schedule_time(value)

    @model_validator(mode="after")
    def _schedule_fields_together(self) -> "PrinterMaintenanceUpdate":
        if self.trigger_mode == "schedule" and (self.schedule_days == [] or self.schedule_time == ""):
            raise ValueError("A schedule needs at least one weekday and a time")
        return self


class PrinterMaintenanceResponse(BaseModel):
    id: int
    printer_id: int
    maintenance_type_id: int
    maintenance_type: MaintenanceTypeResponse
    custom_interval_hours: float | None
    enabled: bool
    last_performed_at: datetime | None
    last_performed_hours: float
    action_options: ActionOptions | None = None
    trigger_mode: str = "manual"
    schedule_days: list[int] | None = None
    schedule_time: str | None = None
    # UTCDatetime: naive UTC in the DB, sent with the Z suffix like the queue
    # and scheduled-drying routes so the client parses them as UTC.
    schedule_next_at: UTCDatetime = None
    last_auto_run_at: UTCDatetime = None
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True


# Maintenance run schemas (#3127)
class MaintenanceRunResponse(BaseModel):
    id: int
    printer_maintenance_id: int
    printer_id: int
    status: str  # pending / running / completed / failed / cancelled
    source: str  # manual / due / schedule
    options: dict[str, bool] | None
    start_after: UTCDatetime
    waiting_reason: str | None
    # Figures behind the reason, e.g. {"bed_temp": 34.2, "threshold": 30.0}
    # for bed_too_warm; None for the reasons that have none
    waiting_detail: dict[str, Any] | None = None
    error_message: str | None
    created_at: UTCDatetime
    started_at: UTCDatetime
    completed_at: UTCDatetime

    class Config:
        from_attributes = True


class CurrentRun(BaseModel):
    """The pending or running run of an item, as the card shows it."""

    id: int
    status: str
    source: str
    waiting_reason: str | None
    waiting_detail: dict[str, Any] | None = None
    started_at: UTCDatetime


# Maintenance History schemas
class MaintenanceHistoryBase(BaseModel):
    notes: str | None = None


class MaintenanceHistoryCreate(MaintenanceHistoryBase):
    pass


class MaintenanceHistoryResponse(MaintenanceHistoryBase):
    id: int
    printer_maintenance_id: int
    performed_at: datetime
    hours_at_maintenance: float

    class Config:
        from_attributes = True


# Combined status response for frontend
class MaintenanceStatus(BaseModel):
    """Maintenance status for a printer with calculated values."""

    id: int
    printer_id: int
    printer_name: str
    printer_model: str | None  # For model-specific documentation links
    maintenance_type_id: int
    maintenance_type_name: str
    maintenance_type_icon: str | None
    maintenance_type_wiki_url: str | None  # Custom wiki URL for the type
    enabled: bool
    # Interval configuration
    interval_hours: float  # custom or default (hours for print-based, days for time-based)
    interval_type: str  # "hours" or "days"
    # For print-hour based maintenance
    current_hours: float  # total print hours for printer
    hours_since_maintenance: float  # current - last_performed
    hours_until_due: float  # interval - hours_since (for hours type)
    # For time-based maintenance
    days_since_maintenance: float | None  # days since last performed
    days_until_due: float | None  # for days type
    # Status flags
    is_due: bool  # hours_until_due <= 0 OR days_until_due <= 0
    is_warning: bool  # within 10% of interval
    last_performed_at: datetime | None
    # Automatic action (#3127); action is None for reminder-only types and the
    # rest is then not meaningful
    action: str | None = None
    action_options: ActionOptions | None = None
    action_available_options: list[str] | None = None  # flags this printer model can run
    trigger_mode: str = "manual"
    schedule_days: list[int] | None = None
    schedule_time: str | None = None
    schedule_next_at: UTCDatetime = None
    current_run: CurrentRun | None = None
    last_run: MaintenanceRunResponse | None = None


class PrinterMaintenanceOverview(BaseModel):
    """Overview of all maintenance items for a printer."""

    printer_id: int
    printer_name: str
    printer_model: str | None  # For model-specific documentation links
    total_print_hours: float
    maintenance_items: list[MaintenanceStatus]
    due_count: int
    warning_count: int


class PerformMaintenanceRequest(BaseModel):
    """Request to mark maintenance as performed."""

    notes: str | None = None
