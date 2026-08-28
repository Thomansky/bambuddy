"""Auto-link for scanned/auto-added spools (#2936, opt-in setting).

With ``auto_link_scanned_spools`` enabled, a spool created by the RFID
auto-add that matches an existing product joins that spool's link group and
arrives with the full master data — cost per kg included — instead of blank
fields. Off (the default) preserves today's behaviour exactly.
"""

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.models.settings import Settings
from backend.app.models.spool import Spool
from backend.app.services.spool_tag_matcher import create_spool_from_tray

TRAY_DATA = {
    "tray_type": "PLA",
    "tray_sub_brands": "PLA Matte",
    "tray_color": "333333FF",
    "tag_uid": "0011223344556677",
    "tray_uuid": "0123456789ABCDEF0123456789ABCDEF",
    "remain": 100,
}


@pytest.fixture
async def known_product_spool(db_session: AsyncSession):
    """An existing, manually curated spool of the product the scan matches.

    create_spool_from_tray derives material="PLA", subtype="Matte",
    brand="Bambu Lab" from TRAY_DATA; color_name stays None because the test
    database has no colour catalog entry for the hex.
    """
    spool = Spool(
        material="PLA",
        subtype="Matte",
        brand="Bambu Lab",
        color_name=None,
        rgba="333333FF",
        label_weight=1000,
        core_weight=250,
        weight_used=650,
        weight_used_baseline=0,
        weight_locked=False,
        cost_per_kg=17.9,
        note="curated master data",
    )
    db_session.add(spool)
    await db_session.commit()
    await db_session.refresh(spool)
    return spool


async def _set_auto_link(db_session: AsyncSession, enabled: bool) -> None:
    db_session.add(Settings(key="auto_link_scanned_spools", value="True" if enabled else "False"))
    await db_session.commit()


@pytest.mark.asyncio
@pytest.mark.integration
async def test_disabled_by_default_creates_standalone_spool(db_session: AsyncSession, known_product_spool):
    spool = await create_spool_from_tray(db_session, dict(TRAY_DATA))
    await db_session.commit()

    assert spool.filament_group_id is None
    assert spool.cost_per_kg is None


@pytest.mark.asyncio
@pytest.mark.integration
async def test_enabled_links_and_inherits_master_data(db_session: AsyncSession, known_product_spool):
    await _set_auto_link(db_session, True)

    spool = await create_spool_from_tray(db_session, dict(TRAY_DATA))
    await db_session.commit()
    await db_session.refresh(known_product_spool)

    # Joined the known product's (freshly founded) group…
    assert spool.filament_group_id is not None
    assert spool.filament_group_id == known_product_spool.filament_group_id
    # …and took over the curated master data, price included.
    assert spool.cost_per_kg == 17.9
    assert spool.note == "curated master data"
    # Per-spool state stays its own: fresh scan is full, donor stays used.
    assert spool.weight_used == 0
    assert known_product_spool.weight_used == 650
    assert spool.tag_uid == "0011223344556677"
    assert known_product_spool.tag_uid is None


@pytest.mark.asyncio
@pytest.mark.integration
async def test_enabled_without_matching_product_stays_standalone(db_session: AsyncSession):
    await _set_auto_link(db_session, True)

    spool = await create_spool_from_tray(db_session, dict(TRAY_DATA))
    await db_session.commit()

    assert spool.filament_group_id is None
