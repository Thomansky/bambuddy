"""API routes for the supplier master list (#2988).

Suppliers are *where filament is bought* — distinct from ``Spool.brand``
(who made it). Maintained next to the spool catalog in the filament
settings; spools reference suppliers through the ``spool_suppliers``
association (see routes/inventory.py for the per-spool assignment endpoint).
"""

import logging

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.core.auth import RequirePermissionIfAuthEnabled
from backend.app.core.database import get_db
from backend.app.core.permissions import Permission
from backend.app.models.supplier import SpoolSupplier, Supplier
from backend.app.models.user import User
from backend.app.schemas.supplier import SupplierCreate, SupplierResponse, SupplierUpdate

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/suppliers", tags=["suppliers"])


async def _spool_counts(db: AsyncSession) -> dict[int, int]:
    """Spools referencing each supplier — shown in the list and guarding deletes."""
    result = await db.execute(
        select(SpoolSupplier.supplier_id, func.count(SpoolSupplier.id)).group_by(SpoolSupplier.supplier_id)
    )
    return dict(result.all())


@router.get("", response_model=list[SupplierResponse])
async def list_suppliers(
    db: AsyncSession = Depends(get_db),
    _: User | None = RequirePermissionIfAuthEnabled(Permission.SUPPLIERS_READ),
):
    """List all suppliers with their spool-usage counts."""
    result = await db.execute(select(Supplier).order_by(Supplier.name))
    suppliers = result.scalars().all()
    counts = await _spool_counts(db)
    responses = []
    for supplier in suppliers:
        response = SupplierResponse.model_validate(supplier)
        response.spool_count = counts.get(supplier.id, 0)
        responses.append(response)
    return responses


@router.post("", response_model=SupplierResponse)
async def create_supplier(
    data: SupplierCreate,
    db: AsyncSession = Depends(get_db),
    _: User | None = RequirePermissionIfAuthEnabled(Permission.SUPPLIERS_CREATE),
):
    """Create a supplier."""
    supplier = Supplier(**data.model_dump())
    db.add(supplier)
    await db.commit()
    await db.refresh(supplier)
    return SupplierResponse.model_validate(supplier)


@router.put("/{supplier_id}", response_model=SupplierResponse)
async def update_supplier(
    supplier_id: int,
    data: SupplierUpdate,
    db: AsyncSession = Depends(get_db),
    _: User | None = RequirePermissionIfAuthEnabled(Permission.SUPPLIERS_UPDATE),
):
    """Update a supplier."""
    result = await db.execute(select(Supplier).where(Supplier.id == supplier_id))
    supplier = result.scalar_one_or_none()
    if not supplier:
        raise HTTPException(404, "Supplier not found")

    for field, value in data.model_dump(exclude_unset=True).items():
        setattr(supplier, field, value)
    await db.commit()
    await db.refresh(supplier)

    response = SupplierResponse.model_validate(supplier)
    response.spool_count = (await _spool_counts(db)).get(supplier.id, 0)
    return response


@router.delete("/{supplier_id}")
async def delete_supplier(
    supplier_id: int,
    db: AsyncSession = Depends(get_db),
    _: User | None = RequirePermissionIfAuthEnabled(Permission.SUPPLIERS_DELETE),
):
    """Delete a supplier.

    Refused with 409 while spools still reference it — assignments must
    never silently orphan; reassign or remove them first.
    """
    result = await db.execute(select(Supplier).where(Supplier.id == supplier_id))
    supplier = result.scalar_one_or_none()
    if not supplier:
        raise HTTPException(404, "Supplier not found")

    count = (await _spool_counts(db)).get(supplier_id, 0)
    if count > 0:
        raise HTTPException(
            status_code=409,
            detail=f"Supplier is assigned to {count} spool(s); remove the assignments first",
        )

    await db.delete(supplier)
    await db.commit()
    return {"status": "deleted", "id": supplier_id}
