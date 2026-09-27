from datetime import datetime

from sqlalchemy import DateTime, Integer, String, func
from sqlalchemy.orm import Mapped, mapped_column

from backend.app.core.database import Base


class ShoppingListItem(Base):
    """A filament SKU queued for purchase."""

    __tablename__ = "filament_shopping_list"

    id: Mapped[int] = mapped_column(primary_key=True)
    material: Mapped[str] = mapped_column(String(50))
    subtype: Mapped[str | None] = mapped_column(String(50))
    brand: Mapped[str | None] = mapped_column(String(100))
    color_name: Mapped[str | None] = mapped_column(String(100))
    quantity_spools: Mapped[int] = mapped_column(Integer, default=1)
    note: Mapped[str | None] = mapped_column(String(500))
    status: Mapped[str] = mapped_column(String(20), default="pending")  # pending | purchased | received
    purchased_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    # Set when the line came from the product master data's reorder list
    # (#3165): the variant it restocks and the supplier it is bought from.
    # Goods-in of that variant ticks the line off.
    variant_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    supplier_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    added_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
