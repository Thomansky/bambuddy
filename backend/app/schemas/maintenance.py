"""Maintenance tracking schemas."""

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field, TypeAdapter, ValidationError, ValidationInfo, field_validator, model_validator

from backend.app.schemas.print_queue import UTCDatetime
from backend.app.services.maintenance_actions import (
    BED_TEMP_BELOW_KEY,
    CALIBRATION_FLAGS,
    KNOWN_ACTIONS,
    TRIGGER_MODES,
    normalize_bed_temp_below,
    parse_schedule_time,
)

# Calibration flags (bool) plus the bed_temp_below start condition (float,
# degrees C), as stored on the item and reported by the overview (#3127).
ActionOptions = dict[str, bool | float]

# Flags go through pydantic's own bool parsing so "false"/0 still read as
# off and "abc"/null are rejected, as they were with dict[str, bool].
_FLAG_BOOL = TypeAdapter(bool)


def _parse_flag(flag: str, value: Any) -> bool:
    try:
        return _FLAG_BOOL.validate_python(value)
    except ValidationError:
        raise ValueError(f"{flag} must be a boolean") from None


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
    # What Bambuddy performs itself for this type (#3127), so a second
    # calibration with its own interval and schedule can sit next to the
    # seeded one. Fixed at creation: MaintenanceTypeUpdate has no action.
    action: str | None = None
    # Printers the type is put on right away. Each must be able to run the
    # action; the route refuses the request otherwise.
    printer_ids: list[int] | None = None

    @field_validator("action")
    @classmethod
    def _known_action(cls, value: str | None) -> str | None:
        if value is None or value == "":
            return None
        if value not in KNOWN_ACTIONS:
            raise ValueError(f"action must be one of: {', '.join(KNOWN_ACTIONS)}")
        return value


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
    # Coverage on the fleet (#3127). printer_count / printer_ids are the
    # active printers with an ENABLED item of this type; eligible_count is
    # how many active printers the type can apply to at all (model gate for
    # system types and for an action, every printer otherwise). Computed in
    # the route -- a bare from_attributes read leaves them at zero.
    printer_count: int = 0
    eligible_count: int = 0
    printer_ids: list[int] = Field(default_factory=list)
    # The printers behind eligible_count, so the tab can offer one checkbox
    # per printer without repeating the model gate in the client.
    eligible_printer_ids: list[int] = Field(default_factory=list)

    class Config:
        from_attributes = True


class DeletedMaintenanceTypeResponse(BaseModel):
    """A hidden type as the "Deleted types" list shows it (#3127)."""

    id: int
    name: str
    icon: str | None = None
    is_system: bool
    action: str | None = None
    default_interval_hours: float
    interval_type: str = "hours"
    # Naive UTC in the DB; NULL on rows hidden before the column existed.
    deleted_at: UTCDatetime = None
    # Printer items still attached, which come back with the type.
    item_count: int = 0


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
    # Off mutes the item: no due/warning reminder, no run-result message (#3127).
    # Both flags are NOT NULL columns: leaving one out keeps it, null is refused.
    notifications_enabled: bool | None = None
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
    # With a schedule: keep the print queue from starting a job on the
    # printer that would still be running at the scheduled time (#3127).
    reserve_before_schedule: bool | None = None

    @field_validator("action_options")
    @classmethod
    def _known_options_only(cls, value: dict[str, Any] | None) -> dict[str, Any] | None:
        if value is None:
            return None
        unknown = sorted(set(value) - set(CALIBRATION_FLAGS) - {BED_TEMP_BELOW_KEY})
        if unknown:
            raise ValueError(f"Unknown calibration option(s): {', '.join(unknown)}")
        checked: dict[str, Any] = {flag: _parse_flag(flag, value[flag]) for flag in CALIBRATION_FLAGS if flag in value}
        if BED_TEMP_BELOW_KEY in value:
            checked[BED_TEMP_BELOW_KEY] = normalize_bed_temp_below(value[BED_TEMP_BELOW_KEY])
        return checked

    @field_validator("enabled", "notifications_enabled", "reserve_before_schedule")
    @classmethod
    def _flag_not_null(cls, value: bool | None, info: ValidationInfo) -> bool:
        if value is None:
            raise ValueError(f"{info.field_name} must be true or false")
        return value

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
    notifications_enabled: bool = True
    last_performed_at: datetime | None
    last_performed_hours: float
    action_options: ActionOptions | None = None
    trigger_mode: str = "manual"
    schedule_days: list[int] | None = None
    schedule_time: str | None = None
    # UTCDatetime: naive UTC in the DB, sent with the Z suffix like the queue
    # and scheduled-drying routes so the client parses them as UTC.
    schedule_next_at: UTCDatetime = None
    reserve_before_schedule: bool = True
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
    # Figures behind the reason: {"bed_temp": 34.2, "threshold": 30.0} for
    # bed_too_warm, {"item": "Printer Calibration"} for after_other_run;
    # None for the reasons that have none
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
    notifications_enabled: bool = True  # False = muted: no reminder, no run result (#3127)
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
    # The queue keeps clear of the scheduled run (#3127); only read with a schedule
    reserve_before_schedule: bool = True
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
    # The require_plate_clear setting, so the card can say what an automatic
    # trigger actually waits for (#3127): both go through the scheduler's
    # idle check with this gate.
    require_plate_clear: bool = False
    # Actions this printer model can perform (#3127), so the "Add type" form
    # can offer the printers that fit the action the user picked.
    available_actions: list[str] = Field(default_factory=list)


class PerformMaintenanceRequest(BaseModel):
    """Request to mark maintenance as performed."""

    notes: str | None = None
