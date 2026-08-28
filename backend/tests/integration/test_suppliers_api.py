"""API coverage for the supplier master list and spool assignments (#2988)."""

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.models.spool import Spool
from backend.app.models.spool_usage_history import SpoolUsageHistory
from backend.app.models.supplier import SpoolSupplier, Supplier


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


@pytest.fixture
async def supplier_factory(db_session: AsyncSession):
    _counter = [0]

    async def _create(**kwargs):
        _counter[0] += 1
        defaults = {"name": f"Supplier {_counter[0]}"}
        defaults.update(kwargs)
        supplier = Supplier(**defaults)
        db_session.add(supplier)
        await db_session.commit()
        await db_session.refresh(supplier)
        return supplier

    return _create


class TestSupplierCrud:
    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_create_and_list(self, async_client: AsyncClient):
        resp = await async_client.post(
            "/api/v1/suppliers",
            json={"name": "Filament24", "website": "https://filament24.example", "customer_number": "C-1042"},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["name"] == "Filament24"
        assert body["spool_count"] == 0

        listing = await async_client.get("/api/v1/suppliers")
        assert listing.status_code == 200
        assert [s["name"] for s in listing.json()] == ["Filament24"]

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_update(self, async_client: AsyncClient, supplier_factory):
        supplier = await supplier_factory(name="Old Name")
        resp = await async_client.put(f"/api/v1/suppliers/{supplier.id}", json={"name": "New Name"})
        assert resp.status_code == 200
        assert resp.json()["name"] == "New Name"

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_delete_unreferenced(self, async_client: AsyncClient, supplier_factory):
        supplier = await supplier_factory()
        resp = await async_client.delete(f"/api/v1/suppliers/{supplier.id}")
        assert resp.status_code == 200
        assert (await async_client.get("/api/v1/suppliers")).json() == []

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_delete_referenced_is_refused(
        self, async_client: AsyncClient, supplier_factory, spool_factory, db_session: AsyncSession
    ):
        supplier = await supplier_factory()
        spool = await spool_factory()
        db_session.add(SpoolSupplier(spool_id=spool.id, supplier_id=supplier.id))
        await db_session.commit()

        resp = await async_client.delete(f"/api/v1/suppliers/{supplier.id}")
        assert resp.status_code == 409
        assert "1 spool" in resp.json()["detail"]

        # The listing surfaces the usage count that blocked the delete.
        listing = await async_client.get("/api/v1/suppliers")
        assert listing.json()[0]["spool_count"] == 1


class TestSpoolSupplierAssignments:
    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_replace_all_and_embed_in_spool_response(
        self, async_client: AsyncClient, supplier_factory, spool_factory
    ):
        a = await supplier_factory(name="Supplier A")
        b = await supplier_factory(name="Supplier B")
        spool = await spool_factory()

        resp = await async_client.put(
            f"/api/v1/inventory/spools/{spool.id}/suppliers",
            json=[
                {
                    "supplier_id": a.id,
                    "supplier_article_number": "A-100",
                    "cost_per_kg": 19.99,
                    "is_purchase_source": True,
                },
                {"supplier_id": b.id, "cost_per_kg": 22.5},
            ],
        )
        assert resp.status_code == 200
        body = resp.json()
        assert {row["supplier_name"] for row in body} == {"Supplier A", "Supplier B"}
        assert [row["is_purchase_source"] for row in sorted(body, key=lambda r: r["supplier_id"])] == [True, False]

        # Embedded in the inventory listing.
        listing = await async_client.get("/api/v1/inventory/spools")
        spool_row = next(s for s in listing.json() if s["id"] == spool.id)
        assert {row["supplier_name"] for row in spool_row["suppliers"]} == {"Supplier A", "Supplier B"}

        # Replace-all: shrinking the list removes the other assignment.
        resp = await async_client.put(
            f"/api/v1/inventory/spools/{spool.id}/suppliers",
            json=[{"supplier_id": b.id, "is_purchase_source": True}],
        )
        assert resp.status_code == 200
        assert [row["supplier_name"] for row in resp.json()] == ["Supplier B"]

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_two_purchase_sources_are_refused(self, async_client: AsyncClient, supplier_factory, spool_factory):
        a = await supplier_factory()
        b = await supplier_factory()
        spool = await spool_factory()

        resp = await async_client.put(
            f"/api/v1/inventory/spools/{spool.id}/suppliers",
            json=[
                {"supplier_id": a.id, "is_purchase_source": True},
                {"supplier_id": b.id, "is_purchase_source": True},
            ],
        )
        assert resp.status_code == 400

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_unknown_supplier_is_refused(self, async_client: AsyncClient, spool_factory):
        spool = await spool_factory()
        resp = await async_client.put(
            f"/api/v1/inventory/spools/{spool.id}/suppliers",
            json=[{"supplier_id": 999999}],
        )
        assert resp.status_code == 404

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_duplicate_supplier_is_refused(self, async_client: AsyncClient, supplier_factory, spool_factory):
        a = await supplier_factory()
        spool = await spool_factory()
        resp = await async_client.put(
            f"/api/v1/inventory/spools/{spool.id}/suppliers",
            json=[{"supplier_id": a.id}, {"supplier_id": a.id}],
        )
        assert resp.status_code == 400


class TestSupplierInheritance:
    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_new_spool_of_same_product_inherits_sources(
        self, async_client: AsyncClient, supplier_factory, spool_factory, db_session: AsyncSession
    ):
        supplier = await supplier_factory(name="Supplier A")
        donor = await spool_factory()
        db_session.add(
            SpoolSupplier(
                spool_id=donor.id,
                supplier_id=supplier.id,
                supplier_article_number="A-100",
                cost_per_kg=19.99,
                is_purchase_source=True,
            )
        )
        await db_session.commit()

        resp = await async_client.post(
            "/api/v1/inventory/spools",
            json={"material": "PLA", "subtype": "Matte", "brand": "Bambu Lab", "color_name": "Charcoal"},
        )
        assert resp.status_code == 200
        suppliers = resp.json()["suppliers"]
        assert [row["supplier_name"] for row in suppliers] == ["Supplier A"]
        assert suppliers[0]["supplier_article_number"] == "A-100"
        assert suppliers[0]["cost_per_kg"] == 19.99
        # Where THIS spool was bought is unknown — never inherited.
        assert suppliers[0]["is_purchase_source"] is False

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_different_product_inherits_nothing(
        self, async_client: AsyncClient, supplier_factory, spool_factory, db_session: AsyncSession
    ):
        supplier = await supplier_factory()
        donor = await spool_factory()
        db_session.add(SpoolSupplier(spool_id=donor.id, supplier_id=supplier.id))
        await db_session.commit()

        resp = await async_client.post(
            "/api/v1/inventory/spools",
            json={"material": "PETG", "subtype": "Matte", "brand": "Bambu Lab", "color_name": "Charcoal"},
        )
        assert resp.status_code == 200
        assert resp.json()["suppliers"] == []


class TestSupplierStats:
    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_groups_by_purchase_source(
        self, async_client: AsyncClient, supplier_factory, spool_factory, db_session: AsyncSession
    ):
        a = await supplier_factory(name="Supplier A")
        b = await supplier_factory(name="Supplier B")
        bought_at_a = await spool_factory(label_weight=1000, weight_used=200)
        alt_only = await spool_factory(color_name="Red")
        db_session.add_all(
            [
                SpoolSupplier(spool_id=bought_at_a.id, supplier_id=a.id, is_purchase_source=True),
                # Alternative source only — must NOT count toward supplier B.
                SpoolSupplier(spool_id=alt_only.id, supplier_id=b.id, is_purchase_source=False),
                SpoolUsageHistory(
                    spool_id=bought_at_a.id, weight_used=150, percent_used=15, status="completed", cost=3.0
                ),
            ]
        )
        await db_session.commit()

        resp = await async_client.get("/api/v1/inventory/stats/suppliers")
        assert resp.status_code == 200
        rows = resp.json()
        assert len(rows) == 1
        assert rows[0]["supplier_name"] == "Supplier A"
        assert rows[0]["spool_count"] == 1
        assert rows[0]["remaining_g"] == pytest.approx(800)
        assert rows[0]["consumed_g"] == pytest.approx(150)
        assert rows[0]["cost"] == pytest.approx(3.0)


class TestSupplierCsvExport:
    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_export_carries_supplier_names(
        self, async_client: AsyncClient, supplier_factory, spool_factory, db_session: AsyncSession
    ):
        a = await supplier_factory(name="Supplier A")
        b = await supplier_factory(name="Supplier B")
        spool = await spool_factory()
        db_session.add_all(
            [
                SpoolSupplier(spool_id=spool.id, supplier_id=a.id, is_purchase_source=True),
                SpoolSupplier(spool_id=spool.id, supplier_id=b.id),
            ]
        )
        await db_session.commit()

        export = await async_client.get("/api/v1/inventory/spools/export")
        assert export.status_code == 200
        header, row = export.text.splitlines()[:2]
        assert "suppliers" in header.split(",")
        assert "Supplier A*; Supplier B" in row
