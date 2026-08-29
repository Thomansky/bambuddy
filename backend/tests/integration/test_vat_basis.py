"""Coverage for the VAT basis of the spool cost.

``cost_vat_included`` says which basis ``cost_per_kg`` was entered in; the
stored number is never rewritten, conversion happens at display time via the
``vat_rate_percent`` setting.
"""

import pytest
from httpx import AsyncClient


class TestCostVatIncluded:
    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_defaults_to_gross(self, async_client: AsyncClient):
        resp = await async_client.post(
            "/api/v1/inventory/spools",
            json={"material": "PLA", "brand": "Bambu Lab", "cost_per_kg": 25.0},
        )
        assert resp.status_code == 200
        assert resp.json()["cost_vat_included"] is True

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_net_entry_round_trips_and_flips(self, async_client: AsyncClient):
        created = await async_client.post(
            "/api/v1/inventory/spools",
            json={"material": "PETG", "cost_per_kg": 21.0, "cost_vat_included": False},
        )
        assert created.status_code == 200
        body = created.json()
        assert body["cost_vat_included"] is False
        # The stored number keeps the entered basis — no silent conversion.
        assert body["cost_per_kg"] == 21.0

        flipped = await async_client.patch(
            f"/api/v1/inventory/spools/{body['id']}",
            json={"cost_vat_included": True},
        )
        assert flipped.status_code == 200
        assert flipped.json()["cost_vat_included"] is True
        assert flipped.json()["cost_per_kg"] == 21.0

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_csv_round_trip_preserves_the_basis(self, async_client: AsyncClient):
        created = await async_client.post(
            "/api/v1/inventory/spools",
            json={"material": "ASA", "cost_per_kg": 30.0, "cost_vat_included": False},
        )
        assert created.status_code == 200

        export = await async_client.get("/api/v1/inventory/spools/export")
        assert export.status_code == 200
        header = export.text.splitlines()[0].split(",")
        assert "cost_vat_included" in header

        result = await async_client.post(
            "/api/v1/inventory/spools/import",
            files={"file": ("spools.csv", export.content, "text/csv")},
        )
        assert result.status_code == 200
        assert result.json()["created"] == 1

        listing = (await async_client.get("/api/v1/inventory/spools")).json()
        assert len(listing) == 2
        assert all(row["cost_vat_included"] is False for row in listing)

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_csv_import_rejects_garbage_basis(self, async_client: AsyncClient):
        csv_text = "material,cost_per_kg,cost_vat_included\nPLA,25,maybe\n"
        preview = await async_client.post(
            "/api/v1/inventory/spools/import?dry_run=true",
            files={"file": ("spools.csv", csv_text.encode(), "text/csv")},
        )
        assert preview.status_code == 200
        body = preview.json()
        assert body["error_count"] == 1
        assert "cost_vat_included" in body["rows"][0]["reason"]


class TestVatRateSetting:
    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_default_and_update(self, async_client: AsyncClient):
        settings = (await async_client.get("/api/v1/settings/")).json()
        assert settings["vat_rate_percent"] == 19.0
        assert settings["price_vat_basis"] == "gross"

        resp = await async_client.put(
            "/api/v1/settings/", json={"vat_rate_percent": 7.7, "price_vat_basis": "net"}
        )
        assert resp.status_code == 200
        settings = (await async_client.get("/api/v1/settings/")).json()
        assert settings["vat_rate_percent"] == 7.7
        assert settings["price_vat_basis"] == "net"

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_rejects_unknown_basis(self, async_client: AsyncClient):
        resp = await async_client.put("/api/v1/settings/", json={"price_vat_basis": "maybe"})
        assert resp.status_code == 422
