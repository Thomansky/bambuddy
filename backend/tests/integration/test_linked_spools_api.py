"""API coverage for linked spools (#2936).

The contract under test: linking shares filament MASTER DATA only. The
per-spool boundary — measured weights and usage baseline, tag ids, location,
archive state, category — must provably survive propagation untouched.
"""

from datetime import datetime, timezone

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.models.spool import Spool
from backend.app.models.spool_link_group import SpoolLinkGroup


@pytest.fixture
async def spool_factory(db_session: AsyncSession):
    async def _create(**kwargs):
        defaults = {
            "material": "PLA",
            "subtype": "Matte",
            "brand": "Bambu Lab",
            "color_name": "Charcoal",
            "rgba": "333333FF",
            "label_weight": 1000,
            "core_weight": 250,
            "weight_used": 0,
            "weight_used_baseline": 0,
            "weight_locked": False,
        }
        defaults.update(kwargs)
        spool = Spool(**defaults)
        db_session.add(spool)
        await db_session.commit()
        await db_session.refresh(spool)
        return spool

    return _create


async def _group_count(db_session: AsyncSession) -> int:
    return len((await db_session.execute(select(SpoolLinkGroup))).scalars().all())


class TestLinking:
    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_link_copies_source_master_data_once(self, async_client: AsyncClient, spool_factory, db_session):
        source = await spool_factory(cost_per_kg=19.99, note="curated", nozzle_temp_max=230)
        other = await spool_factory(cost_per_kg=None, note=None, nozzle_temp_max=None)

        resp = await async_client.post(
            "/api/v1/inventory/spools/link",
            json={"spool_ids": [other.id], "source_spool_id": source.id},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["linked"] == 2
        assert body["updated"] == 1
        assert body["group_id"] > 0

        await db_session.refresh(other)
        await db_session.refresh(source)
        assert other.filament_group_id == source.filament_group_id == body["group_id"]
        # The source spool's values won.
        assert other.cost_per_kg == 19.99
        assert other.note == "curated"
        assert other.nozzle_temp_max == 230

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_link_unknown_spool_is_404(self, async_client: AsyncClient, spool_factory):
        source = await spool_factory()
        resp = await async_client.post(
            "/api/v1/inventory/spools/link",
            json={"spool_ids": [999999], "source_spool_id": source.id},
        )
        assert resp.status_code == 404

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_linking_into_existing_group_merges_and_dissolves_old(
        self, async_client: AsyncClient, spool_factory, db_session
    ):
        a = await spool_factory()
        b = await spool_factory()
        c = await spool_factory()
        d = await spool_factory()
        # Group 1: a+b. Group 2: c+d.
        await async_client.post("/api/v1/inventory/spools/link", json={"spool_ids": [b.id], "source_spool_id": a.id})
        await async_client.post("/api/v1/inventory/spools/link", json={"spool_ids": [d.id], "source_spool_id": c.id})
        # Pull c over into a's group: group 2 falls to one member and dissolves.
        resp = await async_client.post(
            "/api/v1/inventory/spools/link", json={"spool_ids": [c.id], "source_spool_id": a.id}
        )
        assert resp.status_code == 200

        for spool in (a, b, c, d):
            await db_session.refresh(spool)
        assert a.filament_group_id == b.filament_group_id == c.filament_group_id
        # d's group dissolved — it is unlinked, not orphaned.
        assert d.filament_group_id is None
        assert await _group_count(db_session) == 1


class TestPropagationBoundary:
    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_master_data_propagates_but_per_spool_state_never(
        self, async_client: AsyncClient, spool_factory, db_session
    ):
        archived_at = datetime(2026, 1, 1, tzinfo=timezone.utc)
        a = await spool_factory()
        b = await spool_factory(
            weight_used=420.5,
            weight_used_baseline=100.0,
            tag_uid="AABBCCDD",
            tray_uuid="0123456789ABCDEF0123456789ABCDEF",
            storage_location="Drybox 7",
            category="Production",
            low_stock_threshold_pct=42,
            archived_at=archived_at.replace(tzinfo=None),
        )
        await async_client.post("/api/v1/inventory/spools/link", json={"spool_ids": [b.id], "source_spool_id": a.id})

        resp = await async_client.patch(
            f"/api/v1/inventory/spools/{a.id}",
            json={"cost_per_kg": 24.5, "note": "new price", "weight_used": 50},
        )
        assert resp.status_code == 200

        await db_session.refresh(b)
        # Master data crossed the group…
        assert b.cost_per_kg == 24.5
        assert b.note == "new price"
        # …and every per-spool field provably did not.
        assert b.weight_used == 420.5
        assert b.weight_used_baseline == 100.0
        assert b.tag_uid == "AABBCCDD"
        assert b.tray_uuid == "0123456789ABCDEF0123456789ABCDEF"
        assert b.storage_location == "Drybox 7"
        assert b.category == "Production"
        assert b.low_stock_threshold_pct == 42
        assert b.archived_at is not None

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_per_spool_only_edit_does_not_touch_members(
        self, async_client: AsyncClient, spool_factory, db_session
    ):
        a = await spool_factory()
        b = await spool_factory()
        await async_client.post("/api/v1/inventory/spools/link", json={"spool_ids": [b.id], "source_spool_id": a.id})
        before = b.updated_at

        resp = await async_client.patch(f"/api/v1/inventory/spools/{a.id}", json={"weight_used": 123})
        assert resp.status_code == 200

        await db_session.refresh(b)
        assert b.weight_used == 0
        assert b.updated_at == before

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_bulk_update_propagates_only_master_fields(
        self, async_client: AsyncClient, spool_factory, db_session
    ):
        a = await spool_factory()
        b = await spool_factory(weight_used=200)
        await async_client.post("/api/v1/inventory/spools/link", json={"spool_ids": [b.id], "source_spool_id": a.id})

        # Bulk edit selects ONLY a; patch mixes a master field with a
        # per-spool field.
        resp = await async_client.post(
            "/api/v1/inventory/spools/bulk-update",
            json={"ids": [a.id], "update": {"cost_per_kg": 30, "weight_used": 999}},
        )
        assert resp.status_code == 200
        assert resp.json()["propagated"] == 1

        await db_session.refresh(b)
        assert b.cost_per_kg == 30
        # The per-spool half of the patch stayed on the selection.
        assert b.weight_used == 200


class TestUnlinkAndDissolve:
    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_unlink_dissolves_two_member_group(self, async_client: AsyncClient, spool_factory, db_session):
        a = await spool_factory()
        b = await spool_factory()
        await async_client.post("/api/v1/inventory/spools/link", json={"spool_ids": [b.id], "source_spool_id": a.id})

        resp = await async_client.post(f"/api/v1/inventory/spools/{a.id}/unlink")
        assert resp.status_code == 200
        assert resp.json()["filament_group_id"] is None

        await db_session.refresh(b)
        assert b.filament_group_id is None
        assert await _group_count(db_session) == 0

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_unlink_keeps_larger_group_alive(self, async_client: AsyncClient, spool_factory, db_session):
        a = await spool_factory()
        b = await spool_factory()
        c = await spool_factory()
        await async_client.post(
            "/api/v1/inventory/spools/link", json={"spool_ids": [b.id, c.id], "source_spool_id": a.id}
        )

        await async_client.post(f"/api/v1/inventory/spools/{a.id}/unlink")

        await db_session.refresh(b)
        await db_session.refresh(c)
        assert b.filament_group_id is not None
        assert b.filament_group_id == c.filament_group_id
        assert await _group_count(db_session) == 1

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_deleting_a_member_dissolves_a_two_member_group(
        self, async_client: AsyncClient, spool_factory, db_session
    ):
        a = await spool_factory()
        b = await spool_factory()
        await async_client.post("/api/v1/inventory/spools/link", json={"spool_ids": [b.id], "source_spool_id": a.id})

        resp = await async_client.delete(f"/api/v1/inventory/spools/{a.id}")
        assert resp.status_code == 200

        await db_session.refresh(b)
        assert b.filament_group_id is None
        assert await _group_count(db_session) == 0


class TestScanFlowLinking:
    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_create_with_link_to_spool_id_joins_and_inherits(
        self, async_client: AsyncClient, spool_factory, db_session
    ):
        existing = await spool_factory(cost_per_kg=21.5, note="known product")

        resp = await async_client.post(
            "/api/v1/inventory/spools",
            json={"material": "PLA", "link_to_spool_id": existing.id, "tag_uid": "CAFEBABE"},
        )
        assert resp.status_code == 200
        body = resp.json()
        await db_session.refresh(existing)
        assert body["filament_group_id"] == existing.filament_group_id
        # Master data taken over from the linked spool…
        assert body["cost_per_kg"] == 21.5
        assert body["note"] == "known product"
        assert body["subtype"] == "Matte"
        # …while the scan's own identity stays.
        assert body["tag_uid"] == "CAFEBABE"

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_create_with_unknown_link_target_is_404(self, async_client: AsyncClient):
        resp = await async_client.post(
            "/api/v1/inventory/spools",
            json={"material": "PLA", "link_to_spool_id": 999999},
        )
        assert resp.status_code == 404
