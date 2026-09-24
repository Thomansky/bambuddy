from datetime import datetime

from sqlalchemy import Boolean, DateTime, Integer, String, func
from sqlalchemy.orm import Mapped, mapped_column

from backend.app.core.database import Base


class NumberSeries(Base):
    """A named running counter whose next value is handed out on create.

    One row per thing that carries a number (``project``, ``queue_job``,
    ``library_folder``), so a fork or an install can add its own without a
    schema change. Every row is seeded disabled: an existing install keeps
    behaving exactly as before until someone turns a series on.
    """

    __tablename__ = "number_series"

    id: Mapped[int] = mapped_column(primary_key=True)
    key: Mapped[str] = mapped_column(String(32), unique=True, index=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=False, server_default="0")
    prefix: Mapped[str] = mapped_column(String(16), default="", server_default="")
    suffix: Mapped[str] = mapped_column(String(16), default="", server_default="")
    # The value the next allocation hands out — the "start number" the user sets.
    next_value: Mapped[int] = mapped_column(Integer, default=1, server_default="1")
    # Zero-pad the number to this width. 0 = no padding; padding never truncates,
    # so a counter that outgrows it simply gets longer.
    padding: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    updated_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), onupdate=func.now())
