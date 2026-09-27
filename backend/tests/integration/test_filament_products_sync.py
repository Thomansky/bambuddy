"""Product master data, second round (#3165): spools find their product by
themselves, a roll booked in at goods-in takes its RFID tag when the AMS first
reads it, and suppliers live on the product rather than on every spool.
"""

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.models.filament_product import FilamentProductColor
from backend.app.models.spool import Spool
from backend.app.services.spool_tag_matcher import create_spool_from_tray, find_matching_untagged_spool

pytestmark = pytest.mark.integration

API = "/api/v1/inventory/products"
SPOOLS = "/api/v1/inventory/spools"


async def _product(client: AsyncClient, **overrides) -> dict:
    document = {
        "brand": "Bambu Lab",
        "material": "PLA",
        "subtype": "Matte",
        "material_number": "52",
        "nozzle_temp_min": 190,
        "nozzle_temp_max": 230,
        "sizes": [
            {"key": "g500", "label_weight": 500, "core_weight": 180, "price": 12.0},
            {"key": "kg1", "label_weight": 1000, "core_weight": 250, "price": 20.0},
        ],
        "colors": [
            {"key": "charcoal", "color_name": "Charcoal", "rgba": "000000FF"},
            {"key": "white", "color_name": "Ivory White", "rgba": "FFFFFFFF"},
        ],
        "variants": [
            {"color_key": "charcoal", "size_key": "kg1"},
            {"color_key": "charcoal", "size_key": "g500"},
            {"color_key": "white", "size_key": "kg1"},
        ],
    }
    document.update(overrides)
    response = await client.post(API, json=document)
    assert response.status_code == 200, response.text
    return response.json()


def _variant_id(product: dict, color_name: str, label_weight: int) -> int:
    color = next(c for c in product["colors"] if c["color_name"] == color_name)
    size = next(s for s in product["sizes"] if s["label_weight"] == label_weight)
    return next(v["id"] for v in product["variants"] if v["color_id"] == color["id"] and v["size_id"] == size["id"])


async def _new_spool(client: AsyncClient, **values) -> dict:
    payload = {
        "material": "PLA",
        "subtype": "Matte",
        "brand": "Bambu Lab",
        "color_name": "Charcoal",
        "rgba": "000000FF",
        "label_weight": 1000,
        "core_weight": 250,
    }
    payload.update(values)
    response = await client.post(SPOOLS, json=payload)
    assert response.status_code == 200, response.text
    return response.json()


async def _fresh(db: AsyncSession, spool_id: int) -> Spool:
    db.expire_all()
    return (await db.execute(select(Spool).where(Spool.id == spool_id))).scalar_one()


def _tray(**values) -> dict:
    tray = {
        "tray_type": "PLA",
        "tray_sub_brands": "PLA Matte",
        "tray_color": "000000FF",
        "tray_id_name": "",
        "tray_weight": "1000",
        "tray_info_idx": "GFA01",
        "tag_uid": "A1B2C3D4E5F60708",
        "tray_uuid": "0123456789ABCDEF0123456789ABCDEF",
        "nozzle_temp_min": "190",
        "nozzle_temp_max": "230",
        "remain": 100,
    }
    tray.update(values)
    return tray


class TestNewSpoolsFindTheirProduct:
    @pytest.mark.asyncio
    async def test_a_new_spool_is_assigned_and_filled_in(self, async_client: AsyncClient):
        product = await _product(async_client)

        spool = await _new_spool(async_client)

        assert spool["variant_id"] == _variant_id(product, "Charcoal", 1000)
        assert spool["material_number"] == "52"
        assert spool["cost_per_kg"] == 20.0
        assert (spool["nozzle_temp_min"], spool["nozzle_temp_max"]) == (190, 230)

    @pytest.mark.asyncio
    async def test_what_the_spool_brings_along_is_kept(self, async_client: AsyncClient):
        await _product(async_client)

        spool = await _new_spool(async_client, cost_per_kg=15.0, material_number="X1")

        assert spool["cost_per_kg"] == 15.0
        assert spool["material_number"] == "X1"

    @pytest.mark.asyncio
    async def test_a_new_colour_of_a_known_product_gets_its_number(self, async_client: AsyncClient):
        """The material number is the product's, whatever the colour — the
        inheritance keyed on colour could not give a new colour its number."""
        product = await _product(async_client)

        spool = await _new_spool(async_client, color_name="Scarlet Red", rgba="C8102EFF")

        assert spool["material_number"] == "52"
        after = (await async_client.get(f"{API}/{product['id']}")).json()
        assert "Scarlet Red" in [c["color_name"] for c in after["colors"]]
        assert spool["variant_id"] == _variant_id(after, "Scarlet Red", 1000)

    @pytest.mark.asyncio
    async def test_a_spool_of_an_unknown_product_waits_for_the_take_over(self, async_client: AsyncClient):
        await _product(async_client)

        spool = await _new_spool(async_client, brand="Polymaker", subtype="Pro")

        assert spool["variant_id"] is None
        assert len((await async_client.get(API)).json()) == 1

    @pytest.mark.asyncio
    async def test_a_batch_adds_a_new_colour_once(self, async_client: AsyncClient, db_session):
        await _product(async_client)

        response = await async_client.post(
            f"{SPOOLS}/bulk",
            json={
                "quantity": 3,
                "spool": {
                    "material": "PLA",
                    "subtype": "Matte",
                    "brand": "Bambu Lab",
                    "color_name": "Grass Green",
                    "rgba": "00AE42FF",
                    "label_weight": 1000,
                    "core_weight": 250,
                },
            },
        )
        assert response.status_code == 200, response.text

        variants = {spool["variant_id"] for spool in response.json()}
        assert len(variants) == 1 and None not in variants
        greens = (
            (
                await db_session.execute(
                    select(FilamentProductColor).where(FilamentProductColor.color_name == "Grass Green")
                )
            )
            .scalars()
            .all()
        )
        assert len(greens) == 1


class TestEditsMoveTheSpool:
    @pytest.mark.asyncio
    async def test_correcting_the_colour_moves_the_spool(self, async_client: AsyncClient):
        product = await _product(async_client)
        spool = await _new_spool(async_client)

        response = await async_client.patch(
            f"{SPOOLS}/{spool['id']}", json={"color_name": "Ivory White", "rgba": "FFFFFFFF"}
        )

        assert response.status_code == 200, response.text
        assert response.json()["variant_id"] == _variant_id(product, "Ivory White", 1000)

    @pytest.mark.asyncio
    async def test_a_spool_that_is_no_longer_the_product_leaves_it(self, async_client: AsyncClient):
        await _product(async_client)
        spool = await _new_spool(async_client)

        response = await async_client.patch(f"{SPOOLS}/{spool['id']}", json={"brand": "Elegoo"})

        assert response.json()["variant_id"] is None

    @pytest.mark.asyncio
    async def test_a_bulk_edit_moves_the_spools_too(self, async_client: AsyncClient, db_session):
        product = await _product(async_client)
        first = await _new_spool(async_client)
        second = await _new_spool(async_client)

        response = await async_client.post(
            f"{SPOOLS}/bulk-update",
            json={"ids": [first["id"], second["id"]], "update": {"color_name": "Ivory White", "rgba": "FFFFFFFF"}},
        )

        assert response.status_code == 200, response.text
        white = _variant_id(product, "Ivory White", 1000)
        assert (await _fresh(db_session, first["id"])).variant_id == white
        assert (await _fresh(db_session, second["id"])).variant_id == white

    @pytest.mark.asyncio
    async def test_other_edits_leave_the_variant_alone(self, async_client: AsyncClient):
        await _product(async_client)
        spool = await _new_spool(async_client)

        response = await async_client.patch(f"{SPOOLS}/{spool['id']}", json={"note": "top shelf"})

        assert response.json()["variant_id"] == spool["variant_id"]


class TestRfid:
    @pytest.mark.asyncio
    async def test_a_scanned_roll_is_assigned_by_its_colour_value(self, async_client: AsyncClient, db_session):
        product = await _product(async_client)

        spool = await create_spool_from_tray(db_session, _tray())
        await db_session.commit()

        spool = await _fresh(db_session, spool.id)
        assert spool.variant_id == _variant_id(product, "Charcoal", 1000)
        assert spool.material_number == "52"
        assert spool.cost_per_kg == 20.0

    @pytest.mark.asyncio
    async def test_a_booked_in_roll_takes_the_tag(self, async_client: AsyncClient, db_session):
        product = await _product(async_client)
        variant = _variant_id(product, "Charcoal", 1000)
        ids = (
            await async_client.post(f"{API}/variants/{variant}/intake", json={"quantity": 2, "price_per_spool": 20.0})
        ).json()["spool_ids"]

        found = await find_matching_untagged_spool(db_session, _tray())

        assert found is not None
        assert found.id == ids[0]

    @pytest.mark.asyncio
    async def test_the_tag_goes_to_the_roll_of_its_size(self, async_client: AsyncClient, db_session):
        product = await _product(async_client)
        small = (
            await async_client.post(
                f"{API}/variants/{_variant_id(product, 'Charcoal', 500)}/intake", json={"quantity": 1}
            )
        ).json()["spool_ids"][0]
        big = (
            await async_client.post(
                f"{API}/variants/{_variant_id(product, 'Charcoal', 1000)}/intake", json={"quantity": 1}
            )
        ).json()["spool_ids"][0]

        assert (await find_matching_untagged_spool(db_session, _tray(tray_weight="1000"))).id == big
        assert (await find_matching_untagged_spool(db_session, _tray(tray_weight="500"))).id == small


class TestSuppliersOnTheProduct:
    async def _supplier(self, client: AsyncClient, name: str) -> int:
        response = await client.post("/api/v1/inventory/suppliers", json={"name": name})
        assert response.status_code == 201, response.text
        return response.json()["id"]

    @pytest.mark.asyncio
    async def test_a_product_keeps_its_suppliers_and_their_prices(self, async_client: AsyncClient):
        shop = await self._supplier(async_client, "Filament Shop")
        product = await _product(
            async_client,
            suppliers=[
                {"supplier_id": shop, "article_number": "BL-PLA-M", "preferred": True, "prices": {"kg1": 18.5}},
            ],
        )

        [row] = product["suppliers"]
        assert row["supplier_name"] == "Filament Shop"
        assert row["article_number"] == "BL-PLA-M"
        assert row["preferred"] is True
        kg1 = next(s["id"] for s in product["sizes"] if s["label_weight"] == 1000)
        assert row["prices"] == [{"size_id": kg1, "price": 18.5}]

    @pytest.mark.asyncio
    async def test_the_usual_suppliers_price_costs_a_new_spool(self, async_client: AsyncClient):
        shop = await self._supplier(async_client, "Filament Shop")
        await _product(
            async_client,
            suppliers=[{"supplier_id": shop, "preferred": True, "prices": {"kg1": 18.5}}],
        )

        spool = await _new_spool(async_client)

        assert spool["cost_per_kg"] == 18.5

    @pytest.mark.asyncio
    async def test_a_supplier_a_product_uses_cannot_be_deleted(self, async_client: AsyncClient):
        shop = await self._supplier(async_client, "Filament Shop")
        await _product(async_client, suppliers=[{"supplier_id": shop, "prices": {}}])

        response = await async_client.delete(f"/api/v1/inventory/suppliers/{shop}")

        assert response.status_code == 409

    @pytest.mark.asyncio
    async def test_dropping_a_size_drops_its_supplier_prices(self, async_client: AsyncClient):
        shop = await self._supplier(async_client, "Filament Shop")
        product = await _product(
            async_client,
            suppliers=[{"supplier_id": shop, "prices": {"kg1": 18.5, "g500": 11.0}}],
        )
        sizes = {s["label_weight"]: s["id"] for s in product["sizes"]}
        colors = {c["color_name"]: c["id"] for c in product["colors"]}
        document = {
            "brand": "Bambu Lab",
            "material": "PLA",
            "subtype": "Matte",
            "material_number": "52",
            "sizes": [{"id": sizes[1000], "key": "kg1", "label_weight": 1000, "core_weight": 250, "price": 20.0}],
            "colors": [
                {"id": colors["Charcoal"], "key": "charcoal", "color_name": "Charcoal", "rgba": "000000FF"},
                {"id": colors["Ivory White"], "key": "white", "color_name": "Ivory White", "rgba": "FFFFFFFF"},
            ],
            "variants": [{"color_key": "charcoal", "size_key": "kg1"}, {"color_key": "white", "size_key": "kg1"}],
            "suppliers": [{"supplier_id": shop, "prices": {"kg1": 18.5}}],
        }

        response = await async_client.put(f"{API}/{product['id']}", json=document)

        assert response.status_code == 200, response.text
        [row] = response.json()["product"]["suppliers"]
        assert row["prices"] == [{"size_id": sizes[1000], "price": 18.5}]

    @pytest.mark.asyncio
    async def test_the_same_supplier_twice_is_refused(self, async_client: AsyncClient):
        shop = await self._supplier(async_client, "Filament Shop")

        response = await async_client.post(
            API,
            json={"material": "PLA", "suppliers": [{"supplier_id": shop}, {"supplier_id": shop}]},
        )

        assert response.status_code == 400
