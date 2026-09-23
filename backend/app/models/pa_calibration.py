"""Flow-dynamics (pressure advance) calibration runs.

One row per measurement attempt on one AMS slot. The run is a database row
rather than in-memory state because it spans roughly seven minutes of printing
and then waits for a person: it has to survive a page reload, a backend
restart, and the user walking away.
"""

from datetime import datetime

from sqlalchemy import JSON, Boolean, DateTime, Float, ForeignKey, Index, Integer, String, Text, func, text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from backend.app.core.database import Base

# Statuses in which a run owns its printer. Dispatch of anything else —
# queued prints, scheduled drying — must treat the printer as taken, and a
# second run on the same printer is refused while one of these is set.
ACTIVE_PA_STATUSES: tuple[str, ...] = (
    "queued",
    "slicing",
    "uploading",
    "printing",
    "reading_result",
    "awaiting_confirmation",
    "saving",
)

# The index predicate, spelled once. A SQL literal rather than a column
# expression because ``status`` is still a mapped_column descriptor while the
# class body is being evaluated.
ACTIVE_STATUS_SQL = "status IN (" + ", ".join(f"'{name}'" for name in ACTIVE_PA_STATUSES) + ")"


class PaCalibrationRun(Base):
    """A pressure-advance measurement on one slot of one printer."""

    __tablename__ = "pa_calibration_runs"

    id: Mapped[int] = mapped_column(primary_key=True)
    printer_id: Mapped[int] = mapped_column(ForeignKey("printers.id", ondelete="CASCADE"), index=True)

    # The slot being calibrated. ``tray_id`` is the GLOBAL id (ams_id * 4 +
    # slot_id, or the unit id for an AMS-HT, 254 for the external spool) —
    # ams_mapping and extrusion_cali_sel both want that form, while the
    # extrusion_cali_set payload wants ams_id/slot_id separately.
    ams_id: Mapped[int] = mapped_column(Integer)
    slot_id: Mapped[int] = mapped_column(Integer)
    tray_id: Mapped[int] = mapped_column(Integer)
    extruder_id: Mapped[int] = mapped_column(Integer, default=0)

    filament_id: Mapped[str] = mapped_column(String(64), default="")
    setting_id: Mapped[str] = mapped_column(String(64), default="")
    filament_name: Mapped[str] = mapped_column(String(255), default="")

    nozzle_diameter: Mapped[str] = mapped_column(String(16), default="0.4")
    # The nozzle the printer reported when the run was created, in its fitted
    # form ("HS01-0.4"). The write translates it; see utils.pa_calibration.
    nozzle_id: Mapped[str | None] = mapped_column(String(32), nullable=True)

    plate_type: Mapped[str] = mapped_column(String(32), default="textured_plate")
    # The user's answer to "the build plate is empty and the printed line may be
    # removed afterwards". Recorded rather than only validated at creation: the
    # run can sit in the queue for hours before the toolhead moves, and a
    # dispatch that cannot point at an explicit yes must not happen at all.
    plate_confirmed: Mapped[bool] = mapped_column(Boolean, default=False)
    # {printer: {source, id}, process: {...}, filament: {...}} — the preset
    # triplet the user confirmed, stored so a run that waits in the queue for
    # hours slices with what was shown to them rather than with whatever the
    # defaults resolve to by then.
    presets: Mapped[dict | None] = mapped_column(JSON, nullable=True)

    # How the measurement is performed. "sliced_print" is the H2-series path
    # (a real print whose start G-code runs M983.3). The column exists from day
    # one so the X1/P1/A1 path ("printer_command", one MQTT command, no slice)
    # can be added without a migration.
    method: Mapped[str] = mapped_column(String(32), default="sliced_print")

    status: Mapped[str] = mapped_column(String(32), default="queued", index=True)
    # Where a run got to. Equal to ``status`` while it is progressing; on a
    # terminal status it keeps the stage that was reached, so a failed run can
    # still say it failed at "slicing" rather than only that it failed.
    stage: Mapped[str] = mapped_column(String(32), default="queued")
    waiting_reason: Mapped[str | None] = mapped_column(String(64), nullable=True)
    waiting_detail: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    # 0-100 within the current stage (slice progress, then upload progress).
    progress: Mapped[float] = mapped_column(Float, default=0.0)

    remote_filename: Mapped[str | None] = mapped_column(String(255), nullable=True)
    dispatched_subtask_id: Mapped[str | None] = mapped_column(String(64), nullable=True)

    # The K stored for this (filament_id, nozzle_id) before the run, read at
    # creation so the confirmation card can show old -> new. NULL means there
    # was no profile and confirming will create one.
    k_before: Mapped[float | None] = mapped_column(Float, nullable=True)
    k_value: Mapped[float | None] = mapped_column(Float, nullable=True)
    n_coef: Mapped[str | None] = mapped_column(String(32), nullable=True)
    confidence: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # The whole extrusion_cali_get_result entry, kept verbatim. Both captured
    # runs report confidence 0 and nobody yet knows what a non-zero one means;
    # storing the raw entry is how that gets answered from real runs later.
    result_raw: Mapped[dict | None] = mapped_column(JSON, nullable=True)

    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_by_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), nullable=True)

    printer: Mapped["Printer"] = relationship()
    created_by: Mapped["User | None"] = relationship()

    # One live run per printer, enforced by the database rather than only by
    # the route's read-then-insert: the run moves a printer, and two of them
    # racing would upload over each other's file and dispatch twice.
    __table_args__ = (
        Index(
            "uq_pa_calibration_runs_active",
            "printer_id",
            unique=True,
            sqlite_where=text(ACTIVE_STATUS_SQL),
            postgresql_where=text(ACTIVE_STATUS_SQL),
        ),
    )


from backend.app.models.printer import Printer  # noqa: E402
from backend.app.models.user import User  # noqa: E402
