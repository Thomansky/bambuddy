"""Maintenance tracking models."""

from datetime import datetime

from sqlalchemy import JSON, Boolean, DateTime, Float, ForeignKey, Index, String, Text, func, text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from backend.app.core.database import Base


class MaintenanceType(Base):
    """Defines a type of maintenance task with default interval."""

    __tablename__ = "maintenance_types"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(100))
    description: Mapped[str | None] = mapped_column(Text)
    default_interval_hours: Mapped[float] = mapped_column(Float, default=100.0)
    # Interval type: "hours" (print hours) or "days" (calendar days)
    interval_type: Mapped[str] = mapped_column(String(20), default="hours")
    icon: Mapped[str | None] = mapped_column(String(50))  # Icon name for UI
    wiki_url: Mapped[str | None] = mapped_column(String(500))  # Documentation link
    is_system: Mapped[bool] = mapped_column(Boolean, default=False)  # Pre-defined vs custom
    is_deleted: Mapped[bool] = mapped_column(Boolean, default=False)  # Hidden/removed type
    # What Bambuddy can do itself for this type instead of only reminding
    # (#3127). "calibration" is the only value so far; None = reminder only.
    # System types only — custom types cannot carry an action.
    action: Mapped[str | None] = mapped_column(String(32), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())

    # Relationships
    printer_maintenance: Mapped[list["PrinterMaintenance"]] = relationship(
        back_populates="maintenance_type", cascade="all, delete-orphan"
    )


class PrinterMaintenance(Base):
    """Tracks maintenance status for a specific printer."""

    __tablename__ = "printer_maintenance"

    id: Mapped[int] = mapped_column(primary_key=True)
    printer_id: Mapped[int] = mapped_column(ForeignKey("printers.id", ondelete="CASCADE"))
    maintenance_type_id: Mapped[int] = mapped_column(ForeignKey("maintenance_types.id", ondelete="CASCADE"))

    # Custom interval for this printer (overrides default if set)
    custom_interval_hours: Mapped[float | None] = mapped_column(Float, nullable=True)
    # Custom interval type for this printer (overrides default if set)
    custom_interval_type: Mapped[str | None] = mapped_column(String(20), nullable=True)

    # Tracking
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    # Per-item mute (#3127): off drops the item from the due/warning reminder
    # and from the run-result notification, without disabling the item.
    notifications_enabled: Mapped[bool] = mapped_column(Boolean, default=True, server_default="1", nullable=False)
    last_performed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_performed_hours: Mapped[float] = mapped_column(Float, default=0.0)  # Hours at last reset

    # Automatic action settings (#3127); only read when the type has an action.
    # action_options: calibration flag -> bool (see maintenance_actions.CALIBRATION_FLAGS)
    # trigger_mode: "manual" | "when_due" | "schedule"
    # schedule_days: weekday ints, 0 = Monday; schedule_time: "HH:MM" in the
    # local zone (same convention as the local backup schedule)
    # schedule_next_at: next scheduled occurrence, naive UTC, kept current by
    # the routes on save and by the scheduler after each run
    action_options: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    trigger_mode: Mapped[str] = mapped_column(String(16), default="manual")
    schedule_days: Mapped[list | None] = mapped_column(JSON, nullable=True)
    schedule_time: Mapped[str | None] = mapped_column(String(5), nullable=True)
    schedule_next_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_auto_run_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), onupdate=func.now())

    # Relationships
    printer: Mapped["Printer"] = relationship(back_populates="maintenance_items")
    maintenance_type: Mapped["MaintenanceType"] = relationship(back_populates="printer_maintenance")
    history: Mapped[list["MaintenanceHistory"]] = relationship(
        back_populates="printer_maintenance", cascade="all, delete-orphan"
    )
    runs: Mapped[list["MaintenanceRun"]] = relationship(
        back_populates="printer_maintenance", cascade="all, delete-orphan"
    )


class MaintenanceHistory(Base):
    """Log of maintenance actions performed."""

    __tablename__ = "maintenance_history"

    id: Mapped[int] = mapped_column(primary_key=True)
    printer_maintenance_id: Mapped[int] = mapped_column(ForeignKey("printer_maintenance.id", ondelete="CASCADE"))
    performed_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    hours_at_maintenance: Mapped[float] = mapped_column(Float, default=0.0)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Relationships
    printer_maintenance: Mapped["PrinterMaintenance"] = relationship(back_populates="history")


class MaintenanceRun(Base):
    """One execution of an actionable maintenance item (#3127).

    Created pending (by hand, when the item falls due, or from its schedule),
    dispatched by PrintScheduler._check_maintenance_runs() once the printer is
    idle and — with require_plate_clear on — the plate has been released, and
    closed by the printer's own completion event. At most one pending/running
    run exists per item.
    """

    __tablename__ = "maintenance_runs"
    __table_args__ = (
        # The one-active-run-per-item rule, enforced where two concurrent
        # "Run now" requests cannot both slip past the route's read-then-
        # insert. Partial on both engines: finished runs pile up freely.
        Index(
            "uq_maintenance_runs_active",
            "printer_maintenance_id",
            unique=True,
            sqlite_where=text("status IN ('pending', 'running')"),
            postgresql_where=text("status IN ('pending', 'running')"),
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    printer_maintenance_id: Mapped[int] = mapped_column(ForeignKey("printer_maintenance.id", ondelete="CASCADE"))
    printer_id: Mapped[int] = mapped_column(ForeignKey("printers.id", ondelete="CASCADE"))

    # pending / running / completed / failed / cancelled
    status: Mapped[str] = mapped_column(String(20), default="pending")
    # manual / due / schedule
    source: Mapped[str] = mapped_column(String(16), default="manual")
    # Calibration flags frozen at creation, so a settings change does not
    # alter a run that is already waiting.
    options: Mapped[dict | None] = mapped_column(JSON, nullable=True)

    # Earliest start instant, naive UTC (same convention as
    # scheduled_dryings.start_after). None = as soon as the printer is idle.
    start_after: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    # Why the last scheduler pass left the run pending (a token the card
    # translates) and, for reasons that carry a measurement, its figures:
    # {"bed_temp": 34.2, "threshold": 30.0} for bed_too_warm. Written and
    # cleared together.
    waiting_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    waiting_detail: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    printer_maintenance: Mapped["PrinterMaintenance"] = relationship(back_populates="runs")
    printer: Mapped["Printer"] = relationship()


# Import at end to avoid circular imports
from backend.app.models.printer import Printer  # noqa: E402
