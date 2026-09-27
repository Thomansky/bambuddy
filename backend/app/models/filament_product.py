from datetime import datetime

from sqlalchemy import Boolean, DateTime, Float, ForeignKey, Integer, String, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from backend.app.core.database import Base
from backend.app.models.supplier import Supplier


class FilamentProduct(Base):
    """A filament product as it is bought (#3165): one line, all its colours
    and sizes.

    The product is the home of what every spool of it shares — brand,
    material, subtype, the internal material number, preset and temperatures.
    Colours and sizes are declared once on the product; a variant is a
    colour × size combination that actually exists. Spools keep their own
    copy of all of it (every read path stays untouched) and point at their
    variant; an edit here writes through to them, except the price, which a
    spool keeps as what it actually cost.
    """

    __tablename__ = "filament_products"

    id: Mapped[int] = mapped_column(primary_key=True)
    brand: Mapped[str | None] = mapped_column(String(100))
    material: Mapped[str] = mapped_column(String(50))
    subtype: Mapped[str | None] = mapped_column(String(50))
    material_number: Mapped[str | None] = mapped_column(String(64))
    slicer_filament: Mapped[str | None] = mapped_column(String(50))
    slicer_filament_name: Mapped[str | None] = mapped_column(String(100))
    nozzle_temp_min: Mapped[int | None] = mapped_column(Integer)
    nozzle_temp_max: Mapped[int | None] = mapped_column(Integer)
    note: Mapped[str | None] = mapped_column(String(500))
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), onupdate=func.now())

    sizes: Mapped[list["FilamentProductSize"]] = relationship(
        back_populates="product",
        cascade="all",
        lazy="selectin",
        order_by="FilamentProductSize.label_weight",
    )
    colors: Mapped[list["FilamentProductColor"]] = relationship(
        back_populates="product",
        cascade="all",
        lazy="selectin",
        order_by="[FilamentProductColor.sort_order, FilamentProductColor.id]",
    )
    variants: Mapped[list["FilamentVariant"]] = relationship(back_populates="product", cascade="all", lazy="selectin")
    # Where the product is bought — on the product, not on every spool.
    suppliers: Mapped[list["FilamentProductSupplier"]] = relationship(
        back_populates="product",
        cascade="all",
        lazy="selectin",
        order_by="[FilamentProductSupplier.preferred.desc(), FilamentProductSupplier.id]",
    )


class FilamentProductSize(Base):
    """A size the product is sold in, with what the empty spool weighs and
    what one spool of it costs — the price as it is on the invoice."""

    __tablename__ = "filament_product_sizes"

    id: Mapped[int] = mapped_column(primary_key=True)
    product_id: Mapped[int] = mapped_column(ForeignKey("filament_products.id", ondelete="CASCADE"), index=True)
    label_weight: Mapped[int] = mapped_column(Integer)
    core_weight: Mapped[int] = mapped_column(Integer, default=250)
    core_weight_catalog_id: Mapped[int | None] = mapped_column(Integer)
    # Price of ONE spool of this size. Spools store cost per kg; the intake
    # converts, so nobody divides by hand.
    price: Mapped[float | None] = mapped_column(Float)
    price_vat_included: Mapped[bool] = mapped_column(Boolean, default=True, server_default="1")

    product: Mapped[FilamentProduct] = relationship(back_populates="sizes")


class FilamentProductColor(Base):
    """A colour the product is sold in."""

    __tablename__ = "filament_product_colors"

    id: Mapped[int] = mapped_column(primary_key=True)
    product_id: Mapped[int] = mapped_column(ForeignKey("filament_products.id", ondelete="CASCADE"), index=True)
    color_name: Mapped[str | None] = mapped_column(String(100))
    rgba: Mapped[str | None] = mapped_column(String(8))
    extra_colors: Mapped[str | None] = mapped_column(String(255))
    effect_type: Mapped[str | None] = mapped_column(String(20))
    sort_order: Mapped[int] = mapped_column(Integer, default=0)

    product: Mapped[FilamentProduct] = relationship(back_populates="colors")


class FilamentVariant(Base):
    """A colour × size combination that exists — the thing that is scanned,
    stocked and turned into spools."""

    __tablename__ = "filament_variants"
    __table_args__ = (UniqueConstraint("color_id", "size_id", name="uq_filament_variants_color_size"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    product_id: Mapped[int] = mapped_column(ForeignKey("filament_products.id", ondelete="CASCADE"), index=True)
    color_id: Mapped[int] = mapped_column(ForeignKey("filament_product_colors.id", ondelete="CASCADE"), index=True)
    size_id: Mapped[int] = mapped_column(ForeignKey("filament_product_sizes.id", ondelete="CASCADE"), index=True)
    # Only for the few combinations that cost more than their size (silk, glow).
    price_override: Mapped[float | None] = mapped_column(Float)

    product: Mapped[FilamentProduct] = relationship(back_populates="variants")
    color: Mapped[FilamentProductColor] = relationship(lazy="selectin")
    size: Mapped[FilamentProductSize] = relationship(lazy="selectin")
    codes: Mapped[list["FilamentVariantCode"]] = relationship(back_populates="variant", cascade="all", lazy="selectin")


class FilamentVariantCode(Base):
    """A code a variant is recognised by at intake — an EAN on the box, a QR
    code, anything a scanner types. Learnt on the first scan rather than
    typed in advance."""

    __tablename__ = "filament_variant_codes"

    id: Mapped[int] = mapped_column(primary_key=True)
    variant_id: Mapped[int] = mapped_column(ForeignKey("filament_variants.id", ondelete="CASCADE"), index=True)
    code: Mapped[str] = mapped_column(String(128), unique=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())

    variant: Mapped[FilamentVariant] = relationship(back_populates="codes")


class FilamentProductSupplier(Base):
    """Where a product can be bought: the supplier's own article number and,
    per size, what one spool costs there. One supplier can be marked as the
    usual one — goods-in starts from it."""

    __tablename__ = "filament_product_suppliers"
    __table_args__ = (UniqueConstraint("product_id", "supplier_id", name="uq_filament_product_suppliers_pair"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    product_id: Mapped[int] = mapped_column(ForeignKey("filament_products.id", ondelete="CASCADE"), index=True)
    supplier_id: Mapped[int] = mapped_column(ForeignKey("suppliers.id"), index=True)
    article_number: Mapped[str | None] = mapped_column(String(100))
    preferred: Mapped[bool] = mapped_column(Boolean, default=False, server_default="0")

    product: Mapped[FilamentProduct] = relationship(back_populates="suppliers")
    supplier: Mapped[Supplier] = relationship(lazy="selectin")
    prices: Mapped[list["FilamentProductSupplierPrice"]] = relationship(
        back_populates="product_supplier", cascade="all", lazy="selectin"
    )


class FilamentProductSupplierPrice(Base):
    """What one spool of a size costs at one supplier."""

    __tablename__ = "filament_product_supplier_prices"
    __table_args__ = (
        UniqueConstraint("product_supplier_id", "size_id", name="uq_filament_product_supplier_prices_size"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    product_supplier_id: Mapped[int] = mapped_column(
        ForeignKey("filament_product_suppliers.id", ondelete="CASCADE"), index=True
    )
    size_id: Mapped[int] = mapped_column(ForeignKey("filament_product_sizes.id", ondelete="CASCADE"), index=True)
    price: Mapped[float] = mapped_column(Float)

    product_supplier: Mapped[FilamentProductSupplier] = relationship(back_populates="prices")
