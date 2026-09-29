"""Product master data for filament (#3165).

Three levels: a product holds what every spool of it shares, its colours and
sizes are declared once, and a variant is a colour × size combination that
actually exists. Spools keep their own copy of all master data, because every
read path (AMS mapping, the print dialog, statistics, Spoolman sync) reads the
spool; edits here write through into that copy. The price is the exception:
it is copied onto a spool when the spool is created and from then on stays
what that spool cost, so a dearer next order never reprices the shelf.

A variant can carry a target stock. The reorder list compares it with the
spools that still count as stock and what the shopping list already waits
for, and goods-in ticks the shopping list off again.
"""

from __future__ import annotations

import logging
from collections import Counter, defaultdict
from dataclasses import dataclass, field

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.models.filament_product import (
    FilamentProduct,
    FilamentProductColor,
    FilamentProductSize,
    FilamentProductSupplier,
    FilamentVariant,
    FilamentVariantCode,
)
from backend.app.models.shopping_list import ShoppingListItem
from backend.app.models.spool import Spool
from backend.app.models.supplier import Supplier
from backend.app.services.supplier_links import apply_supplier_inheritance

logger = logging.getLogger(__name__)

# Which spool columns each level owns. The price is deliberately absent.
PRODUCT_SPOOL_FIELDS = (
    "brand",
    "material",
    "subtype",
    "material_number",
    "slicer_filament",
    "slicer_filament_name",
    "nozzle_temp_min",
    "nozzle_temp_max",
)
PRODUCT_FIELDS = PRODUCT_SPOOL_FIELDS + ("note",)
COLOR_SPOOL_FIELDS = ("color_name", "rgba", "extra_colors", "effect_type")
SIZE_SPOOL_FIELDS = ("label_weight", "core_weight", "core_weight_catalog_id")

MAX_INTAKE_QUANTITY = 100
# Shopping-list lines that are still coming: not yet bought, or bought and
# not yet booked in.
OPEN_ORDER_STATUSES = ("pending", "purchased")
DEFAULT_LOW_STOCK_THRESHOLD = 20.0


class ProductError(ValueError):
    """A product document that cannot be saved as sent (400)."""


class ProductInUse(Exception):
    """Removing a variant that active spools still point at (409)."""

    def __init__(self, spool_count: int):
        super().__init__(f"{spool_count} active spool(s) still belong to a removed colour, size or combination")
        self.spool_count = spool_count


class CodeTaken(Exception):
    """A code that already names a different variant (409)."""

    def __init__(self, variant_id: int, label: str):
        super().__init__(f"This code already belongs to {label}")
        self.variant_id = variant_id
        self.label = label


def _norm(value: str | None) -> str:
    return (value or "").strip().casefold()


def product_key(brand: str | None, material: str | None, subtype: str | None) -> tuple[str, str, str]:
    """Product identity for matching spools: brand + material + subtype."""
    return (_norm(brand), _norm(material), _norm(subtype))


def color_key(color_name: str | None, rgba: str | None) -> str:
    """Colour identity inside a product: the name, or the hex when unnamed."""
    name = _norm(color_name)
    return name if name else f"#{_norm(rgba)}"


def product_label(product: FilamentProduct) -> str:
    return " ".join(part for part in (product.brand, product.material, product.subtype) if part)


def variant_label(product: FilamentProduct, color: FilamentProductColor, size: FilamentProductSize) -> str:
    color_part = color.color_name or (f"#{color.rgba[:6]}" if color.rgba else "?")
    return f"{product_label(product)} · {color_part} · {format_weight(size.label_weight)}"


def format_weight(grams: int) -> str:
    if grams >= 1000 and grams % 100 == 0:
        kilos = grams / 1000
        return f"{kilos:g} kg"
    return f"{grams} g"


def effective_price(variant: FilamentVariant, size: FilamentProductSize) -> float | None:
    """What one spool of this variant costs: its own price, else its size's."""
    if variant.price_override is not None:
        return variant.price_override
    return size.price


def price_to_cost_per_kg(price: float | None, label_weight: int | None) -> float | None:
    """Spools and print costing work in cost per kg; the invoice is per spool."""
    if price is None or not label_weight:
        return None
    return round(price / (label_weight / 1000), 2)


def spool_values(product: FilamentProduct, color: FilamentProductColor, size: FilamentProductSize) -> dict:
    values = {name: getattr(product, name) for name in PRODUCT_SPOOL_FIELDS}
    values.update({name: getattr(color, name) for name in COLOR_SPOOL_FIELDS})
    values.update({name: getattr(size, name) for name in SIZE_SPOOL_FIELDS})
    return values


# ---------------------------------------------------------------- loading


async def load_products(db: AsyncSession) -> list[FilamentProduct]:
    result = await db.execute(select(FilamentProduct).execution_options(populate_existing=True))
    products = list(result.scalars().unique().all())
    products.sort(key=lambda p: (_norm(p.brand), _norm(p.material), _norm(p.subtype)))
    return products


async def load_product(db: AsyncSession, product_id: int) -> FilamentProduct | None:
    result = await db.execute(
        select(FilamentProduct).where(FilamentProduct.id == product_id).execution_options(populate_existing=True)
    )
    return result.scalars().unique().one_or_none()


@dataclass
class VariantStock:
    spools: int = 0
    remaining_g: float = 0.0
    # Spools above the low-stock threshold — what counts against the target.
    # A roll that is nearly used up is on the shelf but no longer stock.
    in_stock: int = 0


async def low_stock_threshold(db: AsyncSession) -> float:
    """The inventory's low-stock threshold in percent, as the spool list uses it."""
    from backend.app.api.routes.settings import get_setting

    value = await get_setting(db, "low_stock_threshold")
    try:
        return float(value) if value not in (None, "") else DEFAULT_LOW_STOCK_THRESHOLD
    except ValueError:
        return DEFAULT_LOW_STOCK_THRESHOLD


def counts_as_stock(label_weight: int | None, weight_used: float | None, threshold_pct: float) -> bool:
    """A spool counts as stock while its remainder is not below the threshold —
    the same rule that marks spools low in the inventory."""
    if not label_weight or label_weight <= 0:
        return False
    remaining = max(0.0, label_weight - (weight_used or 0))
    return remaining / label_weight * 100 >= threshold_pct


async def variant_stock(db: AsyncSession, variant_ids: list[int] | None = None) -> dict[int, VariantStock]:
    """Active spools, remaining grams and spools that still count as stock,
    per variant."""
    threshold = await low_stock_threshold(db)
    query = select(Spool.variant_id, Spool.label_weight, Spool.weight_used, Spool.low_stock_threshold_pct).where(
        Spool.variant_id.is_not(None), Spool.archived_at.is_(None)
    )
    if variant_ids is not None:
        query = query.where(Spool.variant_id.in_(variant_ids))
    stock: dict[int, VariantStock] = defaultdict(VariantStock)
    for variant_id, label_weight, weight_used, own_threshold in (await db.execute(query)).all():
        entry = stock[variant_id]
        entry.spools += 1
        entry.remaining_g += max(0.0, (label_weight or 0) - (weight_used or 0))
        if counts_as_stock(label_weight, weight_used, own_threshold if own_threshold is not None else threshold):
            entry.in_stock += 1
    return dict(stock)


async def variant_on_order(db: AsyncSession, variant_ids: list[int] | None = None) -> dict[int, int]:
    """Spools per variant on the shopping list and not booked in yet."""
    query = (
        select(ShoppingListItem.variant_id, func.coalesce(func.sum(ShoppingListItem.quantity_spools), 0))
        .where(ShoppingListItem.variant_id.is_not(None), ShoppingListItem.status.in_(OPEN_ORDER_STATUSES))
        .group_by(ShoppingListItem.variant_id)
    )
    if variant_ids is not None:
        query = query.where(ShoppingListItem.variant_id.in_(variant_ids))
    return {row[0]: int(row[1]) for row in (await db.execute(query)).all()}


def shortfall(min_stock: int | None, in_stock: int, on_order: int) -> int:
    """How many spools to order so the shelf reaches its target again."""
    if not min_stock:
        return 0
    return max(0, min_stock - in_stock - on_order)


# ---------------------------------------------------------------- saving


async def _write_through(db: AsyncSession, variant_ids: list[int], values: dict) -> int:
    """Copy changed master data onto the active spools of these variants.

    Archived spools keep what they were: they are history. The price never
    comes through here — PRODUCT/COLOR/SIZE_SPOOL_FIELDS do not contain it.
    """
    if not variant_ids or not values:
        return 0
    result = await db.execute(
        update(Spool)
        .where(Spool.variant_id.in_(variant_ids), Spool.archived_at.is_(None))
        .values(**values)
        .execution_options(synchronize_session=False)
    )
    return result.rowcount or 0


def _clean(value):
    if isinstance(value, str):
        value = value.strip()
        return value or None
    return value


async def save_product(db: AsyncSession, product: FilamentProduct | None, data) -> tuple[FilamentProduct, int]:
    """Create or update a product from the editor's full document.

    ``data`` carries the product fields plus ``sizes``, ``colors`` and
    ``variants``; rows with an ``id`` are updated, rows without one created,
    and existing rows missing from the document removed. Variants reference
    colours and sizes by the ``key`` the document gives them, because new rows
    have no id yet. Returns the product and how many spool records the edit
    wrote through to. The caller commits.
    """
    creating = product is None
    # --- validate the document before touching anything
    size_keys: set[str] = set()
    weights: set[int] = set()
    for size in data.sizes:
        if size.key in size_keys:
            raise ProductError(f"Duplicate size key {size.key!r}")
        if size.label_weight in weights:
            raise ProductError(f"The size {format_weight(size.label_weight)} is listed twice")
        size_keys.add(size.key)
        weights.add(size.label_weight)
    color_keys: set[str] = set()
    color_names: set[str] = set()
    for color in data.colors:
        if color.key in color_keys:
            raise ProductError(f"Duplicate colour key {color.key!r}")
        color_keys.add(color.key)
        identity = color_key(color.color_name, color.rgba)
        if identity in color_names:
            raise ProductError(f"The colour {color.color_name or color.rgba!r} is listed twice")
        color_names.add(identity)
    for variant in data.variants:
        if variant.color_key not in color_keys or variant.size_key not in size_keys:
            raise ProductError("A combination refers to a colour or size that is not in the product")
    supplier_rows = list(getattr(data, "suppliers", None) or [])
    supplier_ids = [row.supplier_id for row in supplier_rows]
    if len(set(supplier_ids)) != len(supplier_ids):
        raise ProductError("A supplier is listed twice")
    if supplier_ids:
        known = set((await db.execute(select(Supplier.id).where(Supplier.id.in_(supplier_ids)))).scalars().all())
        if known != set(supplier_ids):
            raise ProductError("Unknown supplier")

    if creating:
        product = FilamentProduct(material=_clean(data.material))
        db.add(product)
        existing_sizes: dict[int, FilamentProductSize] = {}
        existing_colors: dict[int, FilamentProductColor] = {}
        existing_variants: dict[tuple[int, int], FilamentVariant] = {}
    else:
        existing_sizes = {size.id: size for size in product.sizes}
        existing_colors = {color.id: color for color in product.colors}
        existing_variants = {(v.color_id, v.size_id): v for v in product.variants}

    # --- the product row
    changed_product: dict = {}
    for name in PRODUCT_FIELDS:
        new = _clean(getattr(data, name))
        if creating or getattr(product, name) != new:
            setattr(product, name, new)
            if not creating and name in PRODUCT_SPOOL_FIELDS:
                changed_product[name] = new
    await db.flush()

    # --- sizes and colours: update, create, and note what goes away
    size_by_key: dict[str, FilamentProductSize] = {}
    size_changes: dict[int, dict] = {}
    for incoming in data.sizes:
        row = existing_sizes.get(incoming.id) if incoming.id is not None else None
        if incoming.id is not None and row is None:
            raise ProductError(f"Size {incoming.id} does not belong to this product")
        values = {
            "label_weight": incoming.label_weight,
            "core_weight": incoming.core_weight,
            "core_weight_catalog_id": incoming.core_weight_catalog_id,
            "price": incoming.price,
            "price_vat_included": incoming.price_vat_included,
        }
        if row is None:
            row = FilamentProductSize(product_id=product.id, **values)
            db.add(row)
        else:
            changed = {k: v for k, v in values.items() if getattr(row, k) != v}
            for k, v in changed.items():
                setattr(row, k, v)
            spool_side = {k: v for k, v in changed.items() if k in SIZE_SPOOL_FIELDS}
            if spool_side:
                size_changes[row.id] = spool_side
        size_by_key[incoming.key] = row

    color_by_key: dict[str, FilamentProductColor] = {}
    color_changes: dict[int, dict] = {}
    for order, incoming in enumerate(data.colors):
        row = existing_colors.get(incoming.id) if incoming.id is not None else None
        if incoming.id is not None and row is None:
            raise ProductError(f"Colour {incoming.id} does not belong to this product")
        values = {
            "color_name": _clean(incoming.color_name),
            "rgba": _clean(incoming.rgba.upper()) if incoming.rgba else None,
            "extra_colors": _clean(incoming.extra_colors),
            "effect_type": _clean(incoming.effect_type),
        }
        if row is None:
            row = FilamentProductColor(product_id=product.id, sort_order=order, **values)
            db.add(row)
        else:
            row.sort_order = order
            changed = {k: v for k, v in values.items() if getattr(row, k) != v}
            for k, v in changed.items():
                setattr(row, k, v)
            if changed:
                color_changes[row.id] = changed
        color_by_key[incoming.key] = row
    await db.flush()

    # --- suppliers: the document is the whole truth, so they are rebuilt.
    if not creating:
        for row in list(product.suppliers):
            await db.delete(row)
        await db.flush()
    preferred_taken = False
    for incoming in supplier_rows:
        preferred = bool(incoming.preferred) and not preferred_taken
        preferred_taken = preferred_taken or preferred
        db.add(FilamentProductSupplier(product_id=product.id, supplier_id=incoming.supplier_id, preferred=preferred))
    await db.flush()

    kept_size_ids = {row.id for row in size_by_key.values()}
    kept_color_ids = {row.id for row in color_by_key.values()}
    wanted = {(color_by_key[v.color_key].id, size_by_key[v.size_key].id): v for v in data.variants}

    # --- variants that go away: refuse while active spools point at them
    doomed = [variant for pair, variant in existing_variants.items() if pair not in wanted]
    if doomed:
        doomed_ids = [variant.id for variant in doomed]
        active = (
            await db.execute(
                select(func.count(Spool.id)).where(Spool.variant_id.in_(doomed_ids), Spool.archived_at.is_(None))
            )
        ).scalar_one()
        if active:
            raise ProductInUse(active)
        await db.execute(
            update(Spool)
            .where(Spool.variant_id.in_(doomed_ids))
            .values(variant_id=None)
            .execution_options(synchronize_session=False)
        )
        await _forget_orders(db, doomed_ids)
        for variant in doomed:
            await db.delete(variant)
        await db.flush()

    variant_by_pair: dict[tuple[int, int], FilamentVariant] = {}
    for pair, incoming in wanted.items():
        min_stock = getattr(incoming, "min_stock", None) or None
        variant = existing_variants.get(pair)
        if variant is None:
            variant = FilamentVariant(
                product_id=product.id,
                color_id=pair[0],
                size_id=pair[1],
                price_override=incoming.price_override,
                min_stock=min_stock,
            )
            db.add(variant)
        else:
            if variant.price_override != incoming.price_override:
                variant.price_override = incoming.price_override
            if variant.min_stock != min_stock:
                variant.min_stock = min_stock
        variant_by_pair[pair] = variant

    for size_id, row in existing_sizes.items():
        if size_id not in kept_size_ids:
            await db.delete(row)
    for color_id, row in existing_colors.items():
        if color_id not in kept_color_ids:
            await db.delete(row)
    await db.flush()

    # --- write the changes through to the spools
    written = 0
    surviving = {pair: variant for pair, variant in existing_variants.items() if pair in wanted}
    if changed_product:
        written += await _write_through(db, [v.id for v in surviving.values()], changed_product)
    for size_id, values in size_changes.items():
        written += await _write_through(db, [v.id for (c, s), v in surviving.items() if s == size_id], values)
    for color_id, values in color_changes.items():
        written += await _write_through(db, [v.id for (c, s), v in surviving.items() if c == color_id], values)
    if written:
        logger.info("Product %s: master data written through to %d spool record(s)", product.id, written)
    return product, written


async def _forget_orders(db: AsyncSession, variant_ids: list[int]) -> None:
    """Shopping-list lines of combinations that go away stay on the list as
    plain lines — somebody may have ordered them already."""
    if variant_ids:
        await db.execute(
            update(ShoppingListItem)
            .where(ShoppingListItem.variant_id.in_(variant_ids))
            .values(variant_id=None)
            .execution_options(synchronize_session=False)
        )


async def delete_product(db: AsyncSession, product: FilamentProduct) -> int:
    """Delete a product; its spools stay and simply lose the reference."""
    variant_ids = [variant.id for variant in product.variants]
    unlinked = 0
    if variant_ids:
        result = await db.execute(
            update(Spool)
            .where(Spool.variant_id.in_(variant_ids))
            .values(variant_id=None)
            .execution_options(synchronize_session=False)
        )
        unlinked = result.rowcount or 0
        await _forget_orders(db, variant_ids)
    await db.delete(product)
    await db.flush()
    return unlinked


# ---------------------------------------------------------------- codes


async def find_variant(db: AsyncSession, variant_id: int) -> FilamentVariant | None:
    result = await db.execute(select(FilamentVariant).where(FilamentVariant.id == variant_id))
    return result.scalars().unique().one_or_none()


async def find_variant_by_code(db: AsyncSession, code: str) -> FilamentVariant | None:
    code = code.strip()
    if not code:
        return None
    row = (await db.execute(select(FilamentVariantCode).where(FilamentVariantCode.code == code))).scalar_one_or_none()
    if row is None:
        return None
    return await find_variant(db, row.variant_id)


async def assign_code(db: AsyncSession, variant: FilamentVariant, code: str) -> FilamentVariantCode:
    """Teach a variant a code, so the next scan of it is recognised."""
    code = code.strip()
    if not code:
        raise ProductError("The code is empty")
    existing = (
        await db.execute(select(FilamentVariantCode).where(FilamentVariantCode.code == code))
    ).scalar_one_or_none()
    if existing is not None:
        if existing.variant_id == variant.id:
            return existing
        other = await find_variant(db, existing.variant_id)
        product = await load_product(db, other.product_id) if other else None
        label = (
            variant_label(product, other.color, other.size) if other and product else f"variant {existing.variant_id}"
        )
        raise CodeTaken(existing.variant_id, label)
    row = FilamentVariantCode(variant_id=variant.id, code=code)
    db.add(row)
    await db.flush()
    return row


# ---------------------------------------------------------------- intake


@dataclass
class IntakeResult:
    spool_ids: list[int]
    cost_per_kg: float | None
    # Spools of it the shopping list was waiting for, now ticked off.
    orders_settled: int = 0


async def intake(
    db: AsyncSession,
    variant: FilamentVariant,
    *,
    quantity: int,
    price_per_spool: float | None,
    price_vat_included: bool,
    location_id: int | None = None,
    note: str | None = None,
    settle_orders: bool = True,
) -> IntakeResult:
    """Create ``quantity`` full spools of a variant, filled in completely.

    The price is the one confirmed at intake — normally the variant's, but a
    sale or another supplier can override it for this delivery — and is
    copied onto the spools as cost per kg. It is not linked: a later price
    change reaches only spools created after it. With ``settle_orders`` the
    delivery also ticks off what the shopping list was waiting for of this
    variant. The caller commits.
    """
    if not 1 <= quantity <= MAX_INTAKE_QUANTITY:
        raise ProductError(f"Quantity must be between 1 and {MAX_INTAKE_QUANTITY}")
    product = await load_product(db, variant.product_id)
    values = spool_values(product, variant.color, variant.size)
    cost = price_to_cost_per_kg(price_per_spool, variant.size.label_weight)
    spools = []
    for _ in range(quantity):
        spool = Spool(
            **values,
            variant_id=variant.id,
            cost_per_kg=cost,
            cost_vat_included=price_vat_included,
            location_id=location_id,
            note=_clean(note),
            weight_used=0,
            added_full=True,
            data_origin="manual",
        )
        db.add(spool)
        spools.append(spool)
    await db.flush()
    for spool in spools:
        await apply_supplier_inheritance(db, spool)
    settled = await settle_orders_for(db, variant.id, quantity) if settle_orders else 0
    logger.info("Intake: %d spool(s) of variant %d at %s/kg", quantity, variant.id, cost)
    return IntakeResult(spool_ids=[spool.id for spool in spools], cost_per_kg=cost, orders_settled=settled)


async def settle_orders_for(db: AsyncSession, variant_id: int, quantity: int) -> int:
    """Tick a delivery off the shopping list: lines already bought first,
    then the oldest. A line that is fully delivered leaves the list, the way
    booking in from the list removes it; a partial delivery lowers it."""
    rows = list(
        (
            await db.execute(
                select(ShoppingListItem).where(
                    ShoppingListItem.variant_id == variant_id, ShoppingListItem.status.in_(OPEN_ORDER_STATUSES)
                )
            )
        )
        .scalars()
        .all()
    )
    rows.sort(key=lambda row: (row.status != "purchased", row.id))
    left = quantity
    settled = 0
    for row in rows:
        if left <= 0:
            break
        take = min(left, row.quantity_spools or 0)
        row.quantity_spools = (row.quantity_spools or 0) - take
        left -= take
        settled += take
        if row.quantity_spools <= 0:
            await db.delete(row)
    if settled:
        await db.flush()
    return settled


# ---------------------------------------------------------------- reorder


def _color_text(color: FilamentProductColor) -> str | None:
    return color.color_name or (f"#{color.rgba[:6]}" if color.rgba else None)


async def reorder_lines(db: AsyncSession) -> list[dict]:
    """Every combination below its target, with what is missing and where it
    has been bought — the usual supplier first."""
    products = await load_products(db)
    stock = await variant_stock(db)
    ordered = await variant_on_order(db)
    lines: list[dict] = []
    for position, product in enumerate(products):
        sizes = {size.id: size for size in product.sizes}
        colors = {color.id: color for color in product.colors}
        for variant in product.variants:
            size = sizes.get(variant.size_id)
            color = colors.get(variant.color_id)
            if not variant.min_stock or size is None or color is None:
                continue
            here = stock.get(variant.id, VariantStock())
            on_order = ordered.get(variant.id, 0)
            missing = shortfall(variant.min_stock, here.in_stock, on_order)
            if missing <= 0:
                continue
            lines.append(
                {
                    "variant_id": variant.id,
                    "product_id": product.id,
                    "product_label": product_label(product),
                    "material_number": product.material_number,
                    "color_name": color.color_name,
                    "rgba": color.rgba,
                    "extra_colors": color.extra_colors,
                    "effect_type": color.effect_type,
                    "label_weight": size.label_weight,
                    "min_stock": variant.min_stock,
                    "spools": here.spools,
                    "in_stock": here.in_stock,
                    "on_order": on_order,
                    "shortfall": missing,
                    "list_price": effective_price(variant, size),
                    "price_vat_included": size.price_vat_included,
                    "suppliers": [
                        {
                            "supplier_id": row.supplier_id,
                            "supplier_name": row.supplier.name if row.supplier else "",
                            "preferred": row.preferred,
                        }
                        for row in product.suppliers
                    ],
                    "_order": (position, color.sort_order, color.id, size.label_weight),
                }
            )
    lines.sort(key=lambda line: line["_order"])
    for line in lines:
        line.pop("_order")
    return lines


async def add_to_shopping_list(db: AsyncSession, items) -> dict:
    """Put reorder lines on the shopping list.

    Each line knows its variant and supplier, so goods-in can tick it off. A
    line that is on the list already and not bought yet grows instead of
    being listed twice. The caller commits.
    """
    added = merged = 0
    for item in items:
        if not 1 <= item.quantity <= MAX_INTAKE_QUANTITY:
            raise ProductError(f"Quantity must be between 1 and {MAX_INTAKE_QUANTITY}")
        variant = await find_variant(db, item.variant_id)
        if variant is None:
            raise ProductError(f"Unknown combination {item.variant_id}")
        product = await load_product(db, variant.product_id)
        supplier_row = None
        if item.supplier_id is not None:
            supplier_row = next((row for row in product.suppliers if row.supplier_id == item.supplier_id), None)
            if supplier_row is None:
                raise ProductError("The supplier does not carry this product")
        existing = (
            (
                await db.execute(
                    select(ShoppingListItem).where(
                        ShoppingListItem.variant_id == variant.id,
                        ShoppingListItem.supplier_id.is_(None)
                        if item.supplier_id is None
                        else ShoppingListItem.supplier_id == item.supplier_id,
                        ShoppingListItem.status == "pending",
                    )
                )
            )
            .scalars()
            .first()
        )
        if existing is not None:
            existing.quantity_spools = (existing.quantity_spools or 0) + item.quantity
            merged += 1
            continue
        note_parts = [format_weight(variant.size.label_weight)]
        if supplier_row is not None:
            note_parts.append(supplier_row.supplier.name if supplier_row.supplier else None)
        db.add(
            ShoppingListItem(
                material=product.material,
                subtype=product.subtype,
                brand=product.brand,
                color_name=_color_text(variant.color),
                quantity_spools=item.quantity,
                note=" · ".join(part for part in note_parts if part),
                status="pending",
                variant_id=variant.id,
                supplier_id=item.supplier_id,
            )
        )
        added += 1
    await db.flush()
    return {"added": added, "merged": merged}


async def receive_order(db: AsyncSession, item: ShoppingListItem) -> IntakeResult:
    """Book a shopping-list line in as spools of its variant, at the
    combination's price, and take it off the list. The caller commits."""
    variant = await find_variant(db, item.variant_id) if item.variant_id else None
    if variant is None:
        raise ProductError("This line is not linked to a product combination")
    result = await intake(
        db,
        variant,
        quantity=item.quantity_spools,
        price_per_spool=effective_price(variant, variant.size),
        price_vat_included=variant.size.price_vat_included,
        settle_orders=False,
    )
    await db.delete(item)
    await db.flush()
    return result


# ---------------------------------------------------------------- conversion


def _mode(values, default=None):
    counted = Counter(value for value in values if value not in (None, ""))
    if not counted:
        return default
    return counted.most_common(1)[0][0]


@dataclass
class PlannedColor:
    key: str
    color_name: str | None
    rgba: str | None
    extra_colors: str | None
    effect_type: str | None
    existing_id: int | None
    spool_count: int = 0


@dataclass
class PlannedSize:
    label_weight: int
    core_weight: int
    price: float | None
    price_vat_included: bool
    existing_id: int | None
    spool_count: int = 0


@dataclass
class PlannedProduct:
    key: tuple[str, str, str]
    existing_id: int | None
    brand: str | None
    material: str
    subtype: str | None
    material_number: str | None
    slicer_filament: str | None
    slicer_filament_name: str | None
    nozzle_temp_min: int | None
    nozzle_temp_max: int | None
    colors: dict[str, PlannedColor] = field(default_factory=dict)
    sizes: dict[int, PlannedSize] = field(default_factory=dict)
    # (colour key, label weight) -> (existing variant id or None, spool ids)
    variants: dict[tuple[str, int], tuple[int | None, list[int]]] = field(default_factory=dict)
    material_numbers: Counter = field(default_factory=Counter)
    spool_count: int = 0

    @property
    def label(self) -> str:
        return " ".join(part for part in (self.brand, self.material, self.subtype) if part)


@dataclass
class ConversionPlan:
    products: list[PlannedProduct]
    conflicts: list[dict]
    already_assigned: int

    def as_dict(self) -> dict:
        new_products = sum(1 for p in self.products if p.existing_id is None)
        return {
            "spools_to_assign": sum(p.spool_count for p in self.products),
            "already_assigned": self.already_assigned,
            "new_products": new_products,
            "existing_products": len(self.products) - new_products,
            "new_colors": sum(1 for p in self.products for c in p.colors.values() if c.existing_id is None),
            "new_sizes": sum(1 for p in self.products for s in p.sizes.values() if s.existing_id is None),
            "new_variants": sum(1 for p in self.products for v in p.variants.values() if v[0] is None),
            "conflicts": self.conflicts,
            "products": [
                {
                    "label": p.label,
                    "existing_id": p.existing_id,
                    "material_number": p.material_number,
                    "spool_count": p.spool_count,
                    "colors": [
                        {
                            "color_name": c.color_name,
                            "rgba": c.rgba,
                            "is_new": c.existing_id is None,
                            "spool_count": c.spool_count,
                        }
                        for c in p.colors.values()
                    ],
                    "sizes": [
                        {
                            "label_weight": s.label_weight,
                            "core_weight": s.core_weight,
                            "price": s.price,
                            "is_new": s.existing_id is None,
                            "spool_count": s.spool_count,
                        }
                        for s in sorted(p.sizes.values(), key=lambda s: s.label_weight)
                    ],
                    "variant_count": len(p.variants),
                }
                for p in sorted(self.products, key=lambda p: (-p.spool_count, p.label.casefold()))
            ],
        }


async def plan_conversion(db: AsyncSession) -> ConversionPlan:
    """Group the spools that have no variant yet into products and variants.

    Matches onto what already exists first (product by brand + material +
    subtype, colour by name, size by label weight) and plans only what is
    missing, so it can run again later — after an RFID auto-add, say — and
    pick up just the new spools. Nothing on the spools changes except the
    reference; master data that differs between spools of one product stays
    as it is until the product is edited.
    """
    products = await load_products(db)
    by_key: dict[tuple[str, str, str], FilamentProduct] = {}
    for product in products:
        by_key.setdefault(product_key(product.brand, product.material, product.subtype), product)

    spools = list(
        (
            await db.execute(
                select(Spool).where(Spool.archived_at.is_(None), Spool.variant_id.is_(None)).order_by(Spool.id)
            )
        )
        .scalars()
        .all()
    )
    already_assigned = (
        await db.execute(select(func.count(Spool.id)).where(Spool.archived_at.is_(None), Spool.variant_id.is_not(None)))
    ).scalar_one()

    groups: dict[tuple[str, str, str], list[Spool]] = defaultdict(list)
    for spool in spools:
        groups[product_key(spool.brand, spool.material, spool.subtype)].append(spool)

    planned: list[PlannedProduct] = []
    for key, members in groups.items():
        existing = by_key.get(key)
        numbers = Counter(s.material_number.strip() for s in members if s.material_number and s.material_number.strip())
        if existing is not None:
            plan = PlannedProduct(
                key=key,
                existing_id=existing.id,
                brand=existing.brand,
                material=existing.material,
                subtype=existing.subtype,
                material_number=existing.material_number,
                slicer_filament=existing.slicer_filament,
                slicer_filament_name=existing.slicer_filament_name,
                nozzle_temp_min=existing.nozzle_temp_min,
                nozzle_temp_max=existing.nozzle_temp_max,
            )
            for color in existing.colors:
                plan.colors[color_key(color.color_name, color.rgba)] = PlannedColor(
                    key=color_key(color.color_name, color.rgba),
                    color_name=color.color_name,
                    rgba=color.rgba,
                    extra_colors=color.extra_colors,
                    effect_type=color.effect_type,
                    existing_id=color.id,
                )
            for size in existing.sizes:
                plan.sizes[size.label_weight] = PlannedSize(
                    label_weight=size.label_weight,
                    core_weight=size.core_weight,
                    price=size.price,
                    price_vat_included=size.price_vat_included,
                    existing_id=size.id,
                )
            color_ids = {c.id: color_key(c.color_name, c.rgba) for c in existing.colors}
            size_ids = {s.id: s.label_weight for s in existing.sizes}
            for variant in existing.variants:
                pair = (color_ids.get(variant.color_id), size_ids.get(variant.size_id))
                if None not in pair:
                    plan.variants[pair] = (variant.id, [])
        else:
            temps = _mode([(s.nozzle_temp_min, s.nozzle_temp_max) for s in members], (None, None))
            plan = PlannedProduct(
                key=key,
                existing_id=None,
                brand=_mode([s.brand for s in members]),
                material=_mode([s.material for s in members], members[0].material),
                subtype=_mode([s.subtype for s in members]),
                material_number=numbers.most_common(1)[0][0] if numbers else None,
                slicer_filament=_mode([s.slicer_filament for s in members]),
                slicer_filament_name=_mode([s.slicer_filament_name for s in members]),
                nozzle_temp_min=temps[0],
                nozzle_temp_max=temps[1],
            )
        plan.material_numbers = numbers

        by_color: dict[str, list[Spool]] = defaultdict(list)
        by_size: dict[int, list[Spool]] = defaultdict(list)
        for spool in members:
            by_color[color_key(spool.color_name, spool.rgba)].append(spool)
            by_size[spool.label_weight or 0].append(spool)
        for ckey, colored in by_color.items():
            if ckey not in plan.colors:
                plan.colors[ckey] = PlannedColor(
                    key=ckey,
                    color_name=_mode([s.color_name for s in colored]),
                    rgba=_mode([s.rgba for s in colored]),
                    extra_colors=_mode([s.extra_colors for s in colored]),
                    effect_type=_mode([s.effect_type for s in colored]),
                    existing_id=None,
                )
            plan.colors[ckey].spool_count += len(colored)
        for weight, sized in by_size.items():
            if weight not in plan.sizes:
                # The newest priced spool says best what this size costs now.
                priced = [s for s in sized if s.cost_per_kg is not None]
                latest = max(priced, key=lambda s: (s.created_at is not None, s.created_at, s.id)) if priced else None
                plan.sizes[weight] = PlannedSize(
                    label_weight=weight,
                    core_weight=_mode([s.core_weight for s in sized], 250),
                    price=round(latest.cost_per_kg * weight / 1000, 2) if latest else None,
                    price_vat_included=bool(latest.cost_vat_included) if latest else True,
                    existing_id=None,
                )
            plan.sizes[weight].spool_count += len(sized)
        for spool in members:
            pair = (color_key(spool.color_name, spool.rgba), spool.label_weight or 0)
            existing_variant = plan.variants.get(pair, (None, []))
            plan.variants[pair] = (existing_variant[0], existing_variant[1] + [spool.id])
        plan.spool_count = len(members)
        planned.append(plan)

    conflicts: list[dict] = []
    for plan in planned:
        if len(plan.material_numbers) > 1:
            conflicts.append(
                {
                    "kind": "several_numbers",
                    "product": plan.label,
                    "numbers": [
                        {"number": number, "spools": count} for number, count in plan.material_numbers.most_common()
                    ],
                }
            )
    owners: dict[str, list[str]] = defaultdict(list)
    for plan in planned:
        if plan.material_number:
            owners[plan.material_number].append(plan.label)
    for product in products:
        if product.material_number and product.id not in {p.existing_id for p in planned}:
            owners[product.material_number].append(product_label(product))
    for number, labels in owners.items():
        if len(labels) > 1:
            conflicts.append({"kind": "shared_number", "number": number, "products": sorted(labels)})
    # Near-duplicate colour names inside a product ("Black" and "Schwarz" are
    # not caught, but "Black" and "black " are merged already) — flag two
    # colours with the same hex under different names.
    for plan in planned:
        by_hex: dict[str, list[str]] = defaultdict(list)
        for color in plan.colors.values():
            if color.rgba and color.color_name:
                by_hex[color.rgba[:6].upper()].append(color.color_name)
        for hex_value, names in by_hex.items():
            if len(set(names)) > 1:
                conflicts.append(
                    {"kind": "same_color", "product": plan.label, "hex": hex_value, "names": sorted(set(names))}
                )

    return ConversionPlan(products=planned, conflicts=conflicts, already_assigned=already_assigned)


async def apply_conversion(db: AsyncSession) -> dict:
    """Carry out the plan: create what is missing, then set the references."""
    plan = await plan_conversion(db)
    summary = plan.as_dict()
    for planned in plan.products:
        if planned.existing_id is None:
            product = FilamentProduct(
                brand=planned.brand,
                material=planned.material,
                subtype=planned.subtype,
                material_number=planned.material_number,
                slicer_filament=planned.slicer_filament,
                slicer_filament_name=planned.slicer_filament_name,
                nozzle_temp_min=planned.nozzle_temp_min,
                nozzle_temp_max=planned.nozzle_temp_max,
            )
            db.add(product)
            await db.flush()
            product_id = product.id
        else:
            product_id = planned.existing_id

        color_ids: dict[str, int] = {}
        for order, (ckey, color) in enumerate(planned.colors.items()):
            if color.existing_id is not None:
                color_ids[ckey] = color.existing_id
                continue
            row = FilamentProductColor(
                product_id=product_id,
                color_name=color.color_name,
                rgba=color.rgba,
                extra_colors=color.extra_colors,
                effect_type=color.effect_type,
                sort_order=order,
            )
            db.add(row)
            await db.flush()
            color_ids[ckey] = row.id
        size_ids: dict[int, int] = {}
        for weight, size in planned.sizes.items():
            if size.existing_id is not None:
                size_ids[weight] = size.existing_id
                continue
            row = FilamentProductSize(
                product_id=product_id,
                label_weight=weight,
                core_weight=size.core_weight,
                price=size.price,
                price_vat_included=size.price_vat_included,
            )
            db.add(row)
            await db.flush()
            size_ids[weight] = row.id
        for (ckey, weight), (variant_id, spool_ids) in planned.variants.items():
            if variant_id is None:
                row = FilamentVariant(product_id=product_id, color_id=color_ids[ckey], size_id=size_ids[weight])
                db.add(row)
                await db.flush()
                variant_id = row.id
            if spool_ids:
                await db.execute(
                    update(Spool)
                    .where(Spool.id.in_(spool_ids))
                    .values(variant_id=variant_id)
                    .execution_options(synchronize_session=False)
                )
    logger.info(
        "Product conversion: %d spool(s) assigned, %d new product(s), %d new variant(s)",
        summary["spools_to_assign"],
        summary["new_products"],
        summary["new_variants"],
    )
    return summary


# ---------------------------------------------------------------- automatic assignment

# What decides which variant a spool is a roll of.
IDENTITY_FIELDS = ("brand", "material", "subtype", "color_name", "rgba", "label_weight")


def spool_identity(spool: Spool) -> tuple:
    return tuple(getattr(spool, name) for name in IDENTITY_FIELDS)


@dataclass
class _ProductEntry:
    product: FilamentProduct
    colors: dict[str, FilamentProductColor]
    colors_by_hex: dict[str, list[FilamentProductColor]]
    sizes: dict[int, FilamentProductSize]
    variants: dict[tuple[int, int], FilamentVariant]


async def _product_entries(db: AsyncSession) -> dict[tuple[str, str, str], _ProductEntry]:
    entries: dict[tuple[str, str, str], _ProductEntry] = {}
    for product in await load_products(db):
        key = product_key(product.brand, product.material, product.subtype)
        if key in entries:
            continue
        colors: dict[str, FilamentProductColor] = {}
        by_hex: dict[str, list[FilamentProductColor]] = defaultdict(list)
        for color in product.colors:
            colors.setdefault(color_key(color.color_name, color.rgba), color)
            if color.rgba:
                by_hex[color.rgba[:6].upper()].append(color)
        entries[key] = _ProductEntry(
            product=product,
            colors=colors,
            colors_by_hex=by_hex,
            sizes={size.label_weight: size for size in product.sizes},
            variants={(v.color_id, v.size_id): v for v in product.variants},
        )
    return entries


def _match_color(entry: _ProductEntry, color_name: str | None, rgba: str | None) -> FilamentProductColor | None:
    """By name first; failing that by colour value, when exactly one of the
    product's colours has it. An RFID tag knows the hex for certain, while the
    name it resolves to may be spelt differently from the product's."""
    found = entry.colors.get(color_key(color_name, rgba))
    if found is not None:
        return found
    if rgba:
        same = entry.colors_by_hex.get(rgba[:6].upper(), [])
        if len(same) == 1:
            return same[0]
    return None


def _fill_empty(spool: Spool, product: FilamentProduct, variant: FilamentVariant, size: FilamentProductSize) -> None:
    """Give a new spool what it arrived without. Nothing it carries is touched."""
    if not (spool.material_number or "").strip() and product.material_number:
        spool.material_number = product.material_number
    if not spool.slicer_filament and product.slicer_filament:
        spool.slicer_filament = product.slicer_filament
        if not spool.slicer_filament_name:
            spool.slicer_filament_name = product.slicer_filament_name
    if spool.nozzle_temp_min is None and spool.nozzle_temp_max is None:
        spool.nozzle_temp_min = product.nozzle_temp_min
        spool.nozzle_temp_max = product.nozzle_temp_max
    if spool.cost_per_kg is None:
        cost = price_to_cost_per_kg(effective_price(variant, size), size.label_weight)
        if cost is not None:
            spool.cost_per_kg = cost
            spool.cost_vat_included = size.price_vat_included


@dataclass
class AutoAssignResult:
    assigned: list[int] = field(default_factory=list)
    # Spools whose product does not exist yet. A new product is a decision
    # (number, price), so those wait for the take-over dialog.
    without_product: list[int] = field(default_factory=list)
    new_variants: int = 0


async def auto_assign_spools(db: AsyncSession, spools: list[Spool], *, fill_empty: bool = True) -> AutoAssignResult:
    """Put spools under their product variant without anybody clicking.

    Runs wherever a spool comes into being (by hand, in bulk, from a CSV,
    from an RFID tag) and when an edit changes what the spool is. The product
    must exist; a colour or size it does not list yet is added to it together
    with the combination, because a new colour of a known product is routine.
    With ``fill_empty`` the spool also gets what it arrived without from the
    product: material number, preset, temperatures, and the price as cost per
    kg. The caller commits.
    """
    result = AutoAssignResult()
    active = [spool for spool in spools if spool.archived_at is None]
    if not active:
        return result
    entries = await _product_entries(db)
    for spool in active:
        entry = entries.get(product_key(spool.brand, spool.material, spool.subtype))
        if entry is None:
            spool.variant_id = None
            result.without_product.append(spool.id)
            continue
        product = entry.product
        color = _match_color(entry, spool.color_name, spool.rgba)
        if color is None:
            color = FilamentProductColor(
                product_id=product.id,
                color_name=spool.color_name,
                rgba=spool.rgba,
                extra_colors=spool.extra_colors,
                effect_type=spool.effect_type,
                sort_order=len(entry.colors),
            )
            db.add(color)
            await db.flush()
            entry.colors[color_key(color.color_name, color.rgba)] = color
            if color.rgba:
                entry.colors_by_hex[color.rgba[:6].upper()].append(color)
        weight = spool.label_weight or 0
        size = entry.sizes.get(weight)
        if size is None:
            size = FilamentProductSize(
                product_id=product.id,
                label_weight=weight,
                core_weight=spool.core_weight or 0,
                core_weight_catalog_id=spool.core_weight_catalog_id,
                price=round(spool.cost_per_kg * weight / 1000, 2) if spool.cost_per_kg is not None else None,
                price_vat_included=spool.cost_vat_included is not False,
            )
            db.add(size)
            await db.flush()
            entry.sizes[weight] = size
        variant = entry.variants.get((color.id, size.id))
        if variant is None:
            variant = FilamentVariant(product_id=product.id, color_id=color.id, size_id=size.id)
            db.add(variant)
            await db.flush()
            entry.variants[(color.id, size.id)] = variant
            result.new_variants += 1
        spool.variant_id = variant.id
        result.assigned.append(spool.id)
        if fill_empty:
            _fill_empty(spool, product, variant, size)
    if result.assigned or result.without_product:
        logger.info(
            "Auto-assign: %d spool(s) to their variant, %d without a product, %d new variant(s)",
            len(result.assigned),
            len(result.without_product),
            result.new_variants,
        )
    return result


async def untagged_spool_for_tray(
    db: AsyncSession, *, material: str, subtype: str | None, rgba: str | None, label_weight: int | None
) -> Spool | None:
    """The roll booked in at goods-in that an AMS tag read has just found.

    A Bambu spool entered at goods-in waits on the shelf untagged, as a spool
    of its variant. When the AMS reads its tag for the first time, that spool
    should take the tag instead of a second record being created. The tray
    names material, subtype, colour value and label weight; the brand is
    Bambu Lab. Oldest roll first, in the order it was booked in.
    """
    if not rgba or not label_weight:
        return None
    entries = await _product_entries(db)
    wanted_material, wanted_subtype = _norm(material), _norm(subtype)
    variant_ids: list[int] = []
    for (brand, product_material, product_subtype), entry in entries.items():
        if "bambu" not in brand or product_material != wanted_material or product_subtype != wanted_subtype:
            continue
        color = _match_color(entry, None, rgba)
        size = entry.sizes.get(int(label_weight))
        if color is None or size is None:
            continue
        variant = entry.variants.get((color.id, size.id))
        if variant is not None:
            variant_ids.append(variant.id)
    if not variant_ids:
        return None
    result = await db.execute(
        select(Spool)
        .where(
            Spool.variant_id.in_(variant_ids),
            Spool.archived_at.is_(None),
            Spool.tag_uid.is_(None),
            Spool.tray_uuid.is_(None),
        )
        .order_by(Spool.created_at.asc(), Spool.id.asc())
        .limit(1)
    )
    return result.scalars().first()
