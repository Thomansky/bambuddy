from datetime import datetime

from sqlalchemy import DateTime, func
from sqlalchemy.orm import Mapped, mapped_column

from backend.app.core.database import Base


class SpoolLinkGroup(Base):
    """A group of spools sharing filament master data (#2936).

    Deliberately payload-free: master data stays denormalised on each spool
    row so every read path (list, export, statistics, Spoolman mapping)
    is untouched. The group exists only to say "these records are the same
    filament" — an edit to one member's master data propagates to the rest
    (see services/spool_links.py), while per-spool state (weights, tag ids,
    location, history, archive state) never crosses the group. A group with
    fewer than two members is dissolved, so no orphans accumulate.
    """

    __tablename__ = "spool_link_groups"

    id: Mapped[int] = mapped_column(primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
