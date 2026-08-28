"""Linked spools: shared filament master data across records (#2936).

Spools of the same physical product (e.g. ~90 identical refills) can be
linked into a group. Master-data edits to one member propagate to the whole
group; everything that describes the individual spool — measured/remaining
weight and usage history, RFID/NFC tag ids, location and AMS slot, archive
state, category, low-stock override — never crosses the group. That boundary
is the contract of the feature and is pinned by tests.
"""

import logging

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.models.spool import Spool
from backend.app.models.spool_link_group import SpoolLinkGroup

logger = logging.getLogger(__name__)

# The exact set of columns that is shared across a link group. Everything on
# the spool that is NOT in this set is per-spool state and must never be
# written by propagation. material_number (#2870) / supplier links (#2988)
# would join this set if those features land alongside.
SPOOL_MASTER_DATA_FIELDS: frozenset[str] = frozenset(
    {
        "material",
        "subtype",
        "brand",
        "color_name",
        "rgba",
        "extra_colors",
        "effect_type",
        "label_weight",
        "core_weight",
        "core_weight_catalog_id",
        "nozzle_temp_min",
        "nozzle_temp_max",
        "cost_per_kg",
        "note",
        "slicer_filament",
        "slicer_filament_name",
    }
)


def touches_master_data(field_names) -> bool:
    """True when an update payload contains at least one shared field."""
    return any(name in SPOOL_MASTER_DATA_FIELDS for name in field_names)


async def get_group_members(db: AsyncSession, group_id: int) -> list[Spool]:
    result = await db.execute(select(Spool).where(Spool.filament_group_id == group_id))
    return list(result.scalars().all())


async def propagate_master_data(db: AsyncSession, source: Spool) -> int:
    """Copy the source spool's master data onto its group members.

    Returns the number of OTHER spools written. Archived members are updated
    too — an archived record of the same product is still the same product.
    The caller commits.
    """
    if source.filament_group_id is None:
        return 0
    members = await get_group_members(db, source.filament_group_id)
    updated = 0
    for member in members:
        if member.id == source.id:
            continue
        for field in SPOOL_MASTER_DATA_FIELDS:
            setattr(member, field, getattr(source, field))
        updated += 1
    return updated


async def _dissolve_if_orphan(db: AsyncSession, group_id: int) -> None:
    """Delete a group that has fewer than two members left — no orphans."""
    count = (await db.execute(select(func.count(Spool.id)).where(Spool.filament_group_id == group_id))).scalar_one()
    if count >= 2:
        return
    if count == 1:
        last = (await db.execute(select(Spool).where(Spool.filament_group_id == group_id))).scalars().all()
        for spool in last:
            spool.filament_group_id = None
    group = (await db.execute(select(SpoolLinkGroup).where(SpoolLinkGroup.id == group_id))).scalar_one_or_none()
    if group is not None:
        await db.delete(group)


async def link_spools(db: AsyncSession, spool_ids: list[int], source_spool_id: int) -> tuple[int, int]:
    """Link the given spools into one group; the source spool's values win.

    Reuses the source's existing group when it has one, else creates a new
    group. A listed spool that belonged to a different group leaves it (the
    old group dissolves when fewer than two members remain). Returns
    ``(group_id, propagated_count)``. The caller commits.
    """
    ids = set(spool_ids) | {source_spool_id}
    result = await db.execute(select(Spool).where(Spool.id.in_(ids)))
    spools = {spool.id: spool for spool in result.scalars().all()}
    missing = ids - set(spools)
    if missing:
        raise ValueError(f"Spool(s) not found: {sorted(missing)}")

    source = spools[source_spool_id]
    if source.filament_group_id is not None:
        group_id = source.filament_group_id
    else:
        group = SpoolLinkGroup()
        db.add(group)
        await db.flush()
        group_id = group.id

    left_groups: set[int] = set()
    for spool in spools.values():
        if spool.filament_group_id is not None and spool.filament_group_id != group_id:
            left_groups.add(spool.filament_group_id)
        spool.filament_group_id = group_id
    await db.flush()
    for old_group_id in left_groups:
        await _dissolve_if_orphan(db, old_group_id)

    propagated = await propagate_master_data(db, source)
    logger.info(
        "Linked %d spool(s) into group %d (source spool %d, %d records updated)",
        len(ids),
        group_id,
        source_spool_id,
        propagated,
    )
    return group_id, propagated


async def auto_link_enabled(db: AsyncSession) -> bool:
    """The opt-in ``auto_link_scanned_spools`` setting (default off)."""
    from backend.app.models.settings import Settings

    row = (
        await db.execute(select(Settings.value).where(Settings.key == "auto_link_scanned_spools"))
    ).scalar_one_or_none()
    return (row or "false").lower() == "true"


async def find_auto_link_donor(
    db: AsyncSession,
    *,
    material: str | None,
    subtype: str | None,
    brand: str | None,
    color_name: str | None,
    exclude_spool_id: int,
) -> Spool | None:
    """The existing spool a scanned refill of the same product should link to.

    Product identity is the (material, subtype, brand, color_name) string
    tuple. Prefers a spool that is already in a link group (so the scan
    joins the established group), else the most recently updated match —
    linking to it founds the group. Archived spools count: the product is
    the same even when its last spool ran out.
    """
    if not material:
        return None

    def _same(column, value):
        return column.is_(None) if value is None else column == value

    result = await db.execute(
        select(Spool)
        .where(
            Spool.id != exclude_spool_id,
            Spool.material == material,
            _same(Spool.subtype, subtype),
            _same(Spool.brand, brand),
            _same(Spool.color_name, color_name),
        )
        .order_by(Spool.filament_group_id.is_not(None).desc(), Spool.updated_at.desc())
        .limit(1)
    )
    return result.scalars().first()


async def unlink_spool(db: AsyncSession, spool: Spool) -> None:
    """Remove one spool from its group; the group dissolves below two members.

    The spool keeps its current master data — unlinking separates the record,
    it does not reset it. The caller commits.
    """
    group_id = spool.filament_group_id
    if group_id is None:
        return
    spool.filament_group_id = None
    await db.flush()
    await _dissolve_if_orphan(db, group_id)
