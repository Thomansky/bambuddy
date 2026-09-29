"""Product master data for filament (#3165): products, colour × size variants,
codes learnt at intake, and spools created from a variant."""

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.core.auth import RequireAnyPermissionIfAuthEnabled, RequirePermissionIfAuthEnabled
from backend.app.core.database import get_db
from backend.app.core.permissions import Permission
from backend.app.core.websocket import ws_manager
from backend.app.models.filament_product import FilamentProduct, FilamentVariantCode
from backend.app.models.location import Location
from backend.app.models.shopping_list import ShoppingListItem
from backend.app.models.user import User
from backend.app.services.filament_products import (
    CodeTaken,
    ProductError,
    ProductInUse,
    VariantStock,
    add_to_shopping_list,
    apply_conversion,
    assign_code,
    delete_product,
    effective_price,
    find_variant,
    find_variant_by_code,
    intake,
    load_product,
    load_products,
    plan_conversion,
    price_to_cost_per_kg,
    product_label,
    receive_order,
    reorder_lines,
    save_product,
    shortfall,
    variant_on_order,
    variant_stock,
)

router = APIRouter(prefix="/inventory/products", tags=["inventory"])


class ProductSizeIn(BaseModel):
    id: int | None = None
    key: str = Field(min_length=1, max_length=64)
    label_weight: int = Field(gt=0, le=100_000)
    core_weight: int = Field(default=250, ge=0, le=100_000)
    core_weight_catalog_id: int | None = None
    price: float | None = Field(default=None, ge=0)
    price_vat_included: bool = True


class ProductColorIn(BaseModel):
    id: int | None = None
    key: str = Field(min_length=1, max_length=64)
    color_name: str | None = Field(default=None, max_length=100)
    rgba: str | None = Field(default=None, pattern=r"^[0-9A-Fa-f]{8}$")
    extra_colors: str | None = Field(default=None, max_length=255)
    effect_type: str | None = Field(default=None, max_length=20)


class ProductVariantIn(BaseModel):
    color_key: str
    size_key: str
    price_override: float | None = Field(default=None, ge=0)
    # Spools of it that should be on the shelf; empty = no target.
    min_stock: int | None = Field(default=None, ge=0, le=1000)


class ProductPresetIn(BaseModel):
    # Matches ``printers.model`` ("H2S", "H2D").
    printer_model: str = Field(min_length=1, max_length=50)
    slicer_filament: str | None = Field(default=None, max_length=128)
    slicer_filament_name: str | None = Field(default=None, max_length=255)


class ProductSupplierIn(BaseModel):
    supplier_id: int
    # The usual supplier; the reorder list starts from it.
    preferred: bool = False


class ProductIn(BaseModel):
    brand: str | None = Field(default=None, max_length=100)
    material: str = Field(min_length=1, max_length=50)
    subtype: str | None = Field(default=None, max_length=50)
    material_number: str | None = Field(default=None, max_length=64)
    slicer_filament: str | None = Field(default=None, max_length=50)
    slicer_filament_name: str | None = Field(default=None, max_length=100)
    nozzle_temp_min: int | None = Field(default=None, ge=0, le=500)
    nozzle_temp_max: int | None = Field(default=None, ge=0, le=500)
    note: str | None = Field(default=None, max_length=500)
    # The preset per printer model; left out, they stay as they are.
    presets: list[ProductPresetIn] | None = None
    sizes: list[ProductSizeIn] = []
    colors: list[ProductColorIn] = []
    variants: list[ProductVariantIn] = []
    suppliers: list[ProductSupplierIn] = []


class CodeIn(BaseModel):
    code: str = Field(min_length=1, max_length=128)


class IntakeIn(BaseModel):
    quantity: int = Field(ge=1, le=100)
    price_per_spool: float | None = Field(default=None, ge=0)
    price_vat_included: bool = True
    location_id: int | None = None
    note: str | None = Field(default=None, max_length=500)


class ReorderItemIn(BaseModel):
    variant_id: int
    quantity: int = Field(ge=1, le=100)
    supplier_id: int | None = None


class ReorderIn(BaseModel):
    items: list[ReorderItemIn] = Field(min_length=1, max_length=500)


def _product_out(product: FilamentProduct, stock: dict[int, VariantStock], on_order: dict[int, int]) -> dict:
    sizes = {size.id: size for size in product.sizes}
    variants = []
    for variant in product.variants:
        size = sizes.get(variant.size_id)
        price = effective_price(variant, size) if size else None
        here = stock.get(variant.id, VariantStock())
        ordered = on_order.get(variant.id, 0)
        variants.append(
            {
                "id": variant.id,
                "color_id": variant.color_id,
                "size_id": variant.size_id,
                "price_override": variant.price_override,
                "effective_price": price,
                "cost_per_kg": price_to_cost_per_kg(price, size.label_weight if size else None),
                "codes": [{"id": code.id, "code": code.code} for code in variant.codes],
                "spool_count": here.spools,
                "remaining_g": round(here.remaining_g),
                "min_stock": variant.min_stock,
                "in_stock": here.in_stock,
                "on_order": ordered,
                "shortfall": shortfall(variant.min_stock, here.in_stock, ordered),
            }
        )
    return {
        "id": product.id,
        "label": product_label(product),
        "brand": product.brand,
        "material": product.material,
        "subtype": product.subtype,
        "material_number": product.material_number,
        "slicer_filament": product.slicer_filament,
        "slicer_filament_name": product.slicer_filament_name,
        "presets": [
            {
                "printer_model": row.printer_model,
                "slicer_filament": row.slicer_filament,
                "slicer_filament_name": row.slicer_filament_name,
            }
            for row in product.presets
        ],
        "nozzle_temp_min": product.nozzle_temp_min,
        "nozzle_temp_max": product.nozzle_temp_max,
        "note": product.note,
        "sizes": [
            {
                "id": size.id,
                "label_weight": size.label_weight,
                "core_weight": size.core_weight,
                "core_weight_catalog_id": size.core_weight_catalog_id,
                "price": size.price,
                "price_vat_included": size.price_vat_included,
            }
            for size in product.sizes
        ],
        "colors": [
            {
                "id": color.id,
                "color_name": color.color_name,
                "rgba": color.rgba,
                "extra_colors": color.extra_colors,
                "effect_type": color.effect_type,
            }
            for color in product.colors
        ],
        "variants": variants,
        "suppliers": [
            {
                "supplier_id": row.supplier_id,
                "supplier_name": row.supplier.name if row.supplier else "",
                "preferred": row.preferred,
            }
            for row in product.suppliers
        ],
        "spool_count": sum(v["spool_count"] for v in variants),
        "remaining_g": sum(v["remaining_g"] for v in variants),
    }


async def _fresh_product_out(db: AsyncSession, product_id: int) -> dict:
    db.expire_all()
    product = await load_product(db, product_id)
    variant_ids = [variant.id for variant in product.variants]
    stock = await variant_stock(db, variant_ids)
    return _product_out(product, stock, await variant_on_order(db, variant_ids))


@router.get("")
async def list_products(
    db: AsyncSession = Depends(get_db),
    _: User | None = RequirePermissionIfAuthEnabled(Permission.INVENTORY_READ),
):
    products = await load_products(db)
    stock = await variant_stock(db)
    on_order = await variant_on_order(db)
    return [_product_out(product, stock, on_order) for product in products]


@router.get("/reorder")
async def list_reorder(
    db: AsyncSession = Depends(get_db),
    _: User | None = RequirePermissionIfAuthEnabled(Permission.INVENTORY_READ),
):
    """Every combination below its target stock, with what is missing once
    the spools already on the shopping list are counted, and where it can be
    bought."""
    return await reorder_lines(db)


@router.post("/reorder")
async def order_reorder_lines(
    data: ReorderIn,
    db: AsyncSession = Depends(get_db),
    _: User | None = RequireAnyPermissionIfAuthEnabled(
        Permission.INVENTORY_FORECAST_WRITE, Permission.INVENTORY_UPDATE
    ),
):
    """Put reorder lines on the shopping list, each tied to its combination
    and supplier so goods-in can tick it off again."""
    try:
        result = await add_to_shopping_list(db, data.items)
    except ProductError as exc:
        await db.rollback()
        raise HTTPException(400, str(exc)) from exc
    await db.commit()
    return result


@router.post("/orders/{item_id}/receive")
async def receive_shopping_list_line(
    item_id: int,
    db: AsyncSession = Depends(get_db),
    _: User | None = RequirePermissionIfAuthEnabled(Permission.INVENTORY_UPDATE),
):
    """Book a shopping-list line in through the product: full spools of its
    combination with all master data, priced from its supplier."""
    item = (await db.execute(select(ShoppingListItem).where(ShoppingListItem.id == item_id))).scalar_one_or_none()
    if item is None:
        raise HTTPException(404, "Item not found")
    try:
        result = await receive_order(db, item)
    except ProductError as exc:
        await db.rollback()
        raise HTTPException(400, str(exc)) from exc
    await db.commit()
    await ws_manager.broadcast({"type": "inventory_changed"})
    return {"spool_ids": result.spool_ids, "cost_per_kg": result.cost_per_kg, "orders_settled": result.orders_settled}


@router.get("/conversion")
async def preview_conversion(
    db: AsyncSession = Depends(get_db),
    _: User | None = RequirePermissionIfAuthEnabled(Permission.INVENTORY_READ),
):
    """What taking over the existing spools would create — changes nothing."""
    return (await plan_conversion(db)).as_dict()


@router.post("/conversion")
async def run_conversion(
    db: AsyncSession = Depends(get_db),
    _: User | None = RequirePermissionIfAuthEnabled(Permission.INVENTORY_UPDATE),
):
    summary = await apply_conversion(db)
    await db.commit()
    await ws_manager.broadcast({"type": "inventory_changed"})
    return summary


@router.get("/lookup")
async def lookup_code(
    code: str = Query(min_length=1, max_length=128),
    db: AsyncSession = Depends(get_db),
    _: User | None = RequirePermissionIfAuthEnabled(Permission.INVENTORY_READ),
):
    """The variant a scanned code names — 404 for a code nobody taught yet."""
    variant = await find_variant_by_code(db, code)
    if variant is None:
        raise HTTPException(404, "Unknown code")
    return {"variant_id": variant.id, "product": await _fresh_product_out(db, variant.product_id)}


@router.post("")
async def create_product(
    data: ProductIn,
    db: AsyncSession = Depends(get_db),
    _: User | None = RequirePermissionIfAuthEnabled(Permission.INVENTORY_UPDATE),
):
    try:
        product, _written = await save_product(db, None, data)
    except ProductError as exc:
        raise HTTPException(400, str(exc)) from exc
    await db.commit()
    return await _fresh_product_out(db, product.id)


@router.get("/{product_id}")
async def get_product(
    product_id: int,
    db: AsyncSession = Depends(get_db),
    _: User | None = RequirePermissionIfAuthEnabled(Permission.INVENTORY_READ),
):
    product = await load_product(db, product_id)
    if product is None:
        raise HTTPException(404, "Product not found")
    return await _fresh_product_out(db, product_id)


@router.put("/{product_id}")
async def update_product(
    product_id: int,
    data: ProductIn,
    db: AsyncSession = Depends(get_db),
    _: User | None = RequirePermissionIfAuthEnabled(Permission.INVENTORY_UPDATE),
):
    """Save the editor's whole document; master-data changes reach the
    product's active spools, the price never does."""
    product = await load_product(db, product_id)
    if product is None:
        raise HTTPException(404, "Product not found")
    try:
        product, written = await save_product(db, product, data)
    except ProductError as exc:
        await db.rollback()
        raise HTTPException(400, str(exc)) from exc
    except ProductInUse as exc:
        await db.rollback()
        raise HTTPException(409, {"message": str(exc), "spool_count": exc.spool_count}) from exc
    await db.commit()
    if written:
        await ws_manager.broadcast({"type": "inventory_changed"})
    return {"product": await _fresh_product_out(db, product_id), "spools_updated": written}


@router.delete("/{product_id}")
async def remove_product(
    product_id: int,
    db: AsyncSession = Depends(get_db),
    _: User | None = RequirePermissionIfAuthEnabled(Permission.INVENTORY_DELETE),
):
    """Delete a product; its spools stay, only the reference goes."""
    product = await load_product(db, product_id)
    if product is None:
        raise HTTPException(404, "Product not found")
    unlinked = await delete_product(db, product)
    await db.commit()
    if unlinked:
        await ws_manager.broadcast({"type": "inventory_changed"})
    return {"spools_unlinked": unlinked}


@router.post("/variants/{variant_id}/codes")
async def add_code(
    variant_id: int,
    data: CodeIn,
    db: AsyncSession = Depends(get_db),
    _: User | None = RequirePermissionIfAuthEnabled(Permission.INVENTORY_UPDATE),
):
    variant = await find_variant(db, variant_id)
    if variant is None:
        raise HTTPException(404, "Variant not found")
    try:
        row = await assign_code(db, variant, data.code)
    except ProductError as exc:
        raise HTTPException(400, str(exc)) from exc
    except CodeTaken as exc:
        raise HTTPException(409, {"message": str(exc), "variant_id": exc.variant_id, "label": exc.label}) from exc
    await db.commit()
    return {"id": row.id, "code": row.code, "variant_id": variant_id}


@router.delete("/codes/{code_id}")
async def remove_code(
    code_id: int,
    db: AsyncSession = Depends(get_db),
    _: User | None = RequirePermissionIfAuthEnabled(Permission.INVENTORY_UPDATE),
):
    row = (await db.execute(select(FilamentVariantCode).where(FilamentVariantCode.id == code_id))).scalar_one_or_none()
    if row is None:
        raise HTTPException(404, "Code not found")
    await db.delete(row)
    await db.commit()
    return {"status": "deleted"}


@router.post("/variants/{variant_id}/intake")
async def intake_variant(
    variant_id: int,
    data: IntakeIn,
    db: AsyncSession = Depends(get_db),
    _: User | None = RequirePermissionIfAuthEnabled(Permission.INVENTORY_UPDATE),
):
    """Goods in: ``quantity`` full spools of the variant, filled in from the
    master data, with the confirmed price copied on as cost per kg."""
    variant = await find_variant(db, variant_id)
    if variant is None:
        raise HTTPException(404, "Variant not found")
    if data.location_id is not None:
        location = (await db.execute(select(Location.id).where(Location.id == data.location_id))).scalar_one_or_none()
        if location is None:
            raise HTTPException(400, "Location not found")
    try:
        result = await intake(
            db,
            variant,
            quantity=data.quantity,
            price_per_spool=data.price_per_spool,
            price_vat_included=data.price_vat_included,
            location_id=data.location_id,
            note=data.note,
        )
    except ProductError as exc:
        raise HTTPException(400, str(exc)) from exc
    await db.commit()
    await ws_manager.broadcast({"type": "inventory_changed"})
    return {"spool_ids": result.spool_ids, "cost_per_kg": result.cost_per_kg, "orders_settled": result.orders_settled}
