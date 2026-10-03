"""Product master data for filament (#3165).

A product is declared once with its colours and sizes; a variant is a colour ×
size combination that exists; spools are created from a variant at intake and
keep pointing at it. What is pinned here: existing spools convert into that
shape without being changed, intake fills a spool in completely and turns the
invoice price into cost per kg, master-data edits reach the spools but the
price never does, and codes are learnt once and recognised after.
"""

from datetime import datetime

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.models.spool import Spool

pytestmark = pytest.mark.integration

API = "/api/v1/inventory/products"


async def _spool(db: AsyncSession, **values) -> Spool:
    defaults = {
        "material": "PLA",
        "subtype": "Matte",
        "brand": "Bambu Lab",
        "color_name": "Black",
        "rgba": "000000FF",
        "label_weight": 1000,
        "core_weight": 250,
        "weight_used": 0,
    }
    defaults.update(values)
    spool = Spool(**defaults)
    db.add(spool)
    await db.commit()
    await db.refresh(spool)
    return spool


async def _reload(db: AsyncSession, spool_id: int) -> Spool:
    db.expire_all()
    return (await db.execute(select(Spool).where(Spool.id == spool_id))).scalar_one()


def _document(product: dict) -> dict:
    """The editor's save document for a product as the API returned it."""
    return {
        **{
            key: product[key]
            for key in (
                "brand",
                "material",
                "subtype",
                "material_number",
                "slicer_filament",
                "slicer_filament_name",
                "note",
            )
        },
        "sizes": [
            {
                "id": s["id"],
                "key": f"s{s['id']}",
                "label_weight": s["label_weight"],
                "core_weight": s["core_weight"],
                "price": s["price"],
                "price_vat_included": s["price_vat_included"],
            }
            for s in product["sizes"]
        ],
        "colors": [
            {"id": c["id"], "key": f"c{c['id']}", "color_name": c["color_name"], "rgba": c["rgba"]}
            for c in product["colors"]
        ],
        "variants": [
            {"color_key": f"c{v['color_id']}", "size_key": f"s{v['size_id']}", "price_override": v["price_override"]}
            for v in product["variants"]
        ],
    }


def _variant(product: dict, color_name: str, label_weight: int) -> dict:
    color = next(c for c in product["colors"] if c["color_name"] == color_name)
    size = next(s for s in product["sizes"] if s["label_weight"] == label_weight)
    return next(v for v in product["variants"] if v["color_id"] == color["id"] and v["size_id"] == size["id"])


async def _new_product(client: AsyncClient, **overrides) -> dict:
    document = {
        "brand": "Bambu Lab",
        "material": "PLA",
        "subtype": "Matte",
        "material_number": "52",
        "sizes": [
            {"key": "kg1", "label_weight": 1000, "core_weight": 250, "price": 20.0},
            {"key": "kg5", "label_weight": 5000, "core_weight": 900, "price": 90.0},
        ],
        "colors": [
            {"key": "black", "color_name": "Black", "rgba": "000000FF"},
            {"key": "white", "color_name": "White", "rgba": "FFFFFFFF"},
        ],
        "variants": [
            {"color_key": "black", "size_key": "kg1"},
            {"color_key": "black", "size_key": "kg5"},
            {"color_key": "white", "size_key": "kg1"},
        ],
    }
    document.update(overrides)
    response = await client.post(API, json=document)
    assert response.status_code == 200, response.text
    return response.json()


class TestTakingOverTheSpools:
    @pytest.mark.asyncio
    async def test_one_product_with_its_colours_and_sizes(self, async_client: AsyncClient, db_session):
        black = (await _spool(db_session, material_number="52", cost_per_kg=20.0)).id
        white = (
            await _spool(db_session, material_number="52", color_name="White", rgba="FFFFFFFF", cost_per_kg=20.0)
        ).id
        big = (await _spool(db_session, material_number="52", label_weight=5000, core_weight=900, cost_per_kg=18.0)).id

        preview = (await async_client.get(f"{API}/conversion")).json()
        assert preview["spools_to_assign"] == 3
        assert preview["new_products"] == 1
        assert preview["new_variants"] == 3

        response = await async_client.post(f"{API}/conversion")
        assert response.status_code == 200, response.text

        products = (await async_client.get(API)).json()
        assert len(products) == 1
        product = products[0]
        assert product["label"] == "Bambu Lab PLA Matte"
        assert product["material_number"] == "52"
        assert sorted(c["color_name"] for c in product["colors"]) == ["Black", "White"]
        assert sorted(s["label_weight"] for s in product["sizes"]) == [1000, 5000]
        # The size price is what one spool costs: 18 €/kg × 5 kg.
        assert next(s for s in product["sizes"] if s["label_weight"] == 5000)["price"] == 90.0
        assert (await _reload(db_session, black)).variant_id == _variant(product, "Black", 1000)["id"]
        assert (await _reload(db_session, white)).variant_id == _variant(product, "White", 1000)["id"]
        assert (await _reload(db_session, big)).variant_id == _variant(product, "Black", 5000)["id"]

    @pytest.mark.asyncio
    async def test_the_spools_themselves_are_not_changed(self, async_client: AsyncClient, db_session):
        """Master data that differs between rolls stays until someone edits
        the product — converting only sets the reference."""
        usual = (await _spool(db_session, nozzle_temp_min=190, nozzle_temp_max=230)).id
        await _spool(db_session, nozzle_temp_min=190, nozzle_temp_max=230)
        odd_id = (await _spool(db_session, nozzle_temp_min=200, nozzle_temp_max=240, note="hot one")).id

        await async_client.post(f"{API}/conversion")

        odd = await _reload(db_session, odd_id)
        assert (odd.nozzle_temp_min, odd.nozzle_temp_max, odd.note) == (200, 240, "hot one")
        odd_variant = odd.variant_id
        assert odd_variant == (await _reload(db_session, usual)).variant_id

    @pytest.mark.asyncio
    async def test_running_again_picks_up_only_new_spools(self, async_client: AsyncClient, db_session):
        await _spool(db_session)
        await async_client.post(f"{API}/conversion")
        assert (await async_client.get(f"{API}/conversion")).json()["spools_to_assign"] == 0

        refill = (await _spool(db_session)).id
        preview = (await async_client.get(f"{API}/conversion")).json()
        assert preview["spools_to_assign"] == 1
        assert preview["new_products"] == 0
        assert preview["new_variants"] == 0

        await async_client.post(f"{API}/conversion")
        product = (await async_client.get(API)).json()[0]
        assert (await _reload(db_session, refill)).variant_id == _variant(product, "Black", 1000)["id"]

    @pytest.mark.asyncio
    async def test_spelling_of_brand_and_colour_does_not_split_a_product(self, async_client: AsyncClient, db_session):
        await _spool(db_session, brand="Bambu Lab", color_name="Black")
        await _spool(db_session, brand="bambu lab ", color_name="black")

        preview = (await async_client.get(f"{API}/conversion")).json()
        assert preview["new_products"] == 1
        assert preview["new_colors"] == 1

    @pytest.mark.asyncio
    async def test_conflicts_are_reported(self, async_client: AsyncClient, db_session):
        await _spool(db_session, material_number="52")
        await _spool(db_session, material_number="57")
        await _spool(db_session, material="PETG", subtype="Basic", material_number="52")

        conflicts = (await async_client.get(f"{API}/conversion")).json()["conflicts"]
        kinds = {c["kind"] for c in conflicts}
        assert "several_numbers" in kinds
        assert "shared_number" in kinds


class TestIntake:
    @pytest.mark.asyncio
    async def test_spools_come_out_filled_in(self, async_client: AsyncClient, db_session):
        product = await _new_product(async_client)
        location = (await async_client.post("/api/v1/inventory/locations", json={"name": "Shelf A2"})).json()["id"]
        variant = _variant(product, "Black", 5000)

        response = await async_client.post(
            f"{API}/variants/{variant['id']}/intake",
            json={"quantity": 3, "price_per_spool": 90.0, "location_id": location},
        )
        assert response.status_code == 200, response.text
        body = response.json()
        assert len(body["spool_ids"]) == 3
        assert body["cost_per_kg"] == 18.0

        spool = await _reload(db_session, body["spool_ids"][0])
        assert (spool.brand, spool.material, spool.subtype) == ("Bambu Lab", "PLA", "Matte")
        assert (spool.color_name, spool.rgba) == ("Black", "000000FF")
        assert (spool.label_weight, spool.core_weight) == (5000, 900)
        assert spool.material_number == "52"
        # The product has no nozzle range: the slot takes the material's.
        assert (spool.nozzle_temp_min, spool.nozzle_temp_max) == (None, None)
        assert spool.cost_per_kg == 18.0
        assert spool.location_id == location
        assert spool.weight_used == 0
        assert spool.variant_id == variant["id"]

    @pytest.mark.asyncio
    async def test_the_price_can_differ_for_one_delivery(self, async_client: AsyncClient, db_session):
        product = await _new_product(async_client)
        variant = _variant(product, "Black", 1000)

        body = (
            await async_client.post(
                f"{API}/variants/{variant['id']}/intake", json={"quantity": 1, "price_per_spool": 15.0}
            )
        ).json()

        assert body["cost_per_kg"] == 15.0
        product = (await async_client.get(f"{API}/{product['id']}")).json()
        assert next(s for s in product["sizes"] if s["label_weight"] == 1000)["price"] == 20.0

    @pytest.mark.asyncio
    async def test_a_combination_can_cost_more_than_its_size(self, async_client: AsyncClient):
        document = {
            "material": "PLA",
            "subtype": "Silk",
            "sizes": [{"key": "kg1", "label_weight": 1000, "price": 20.0}],
            "colors": [
                {"key": "gold", "color_name": "Gold", "rgba": "D4AF37FF"},
                {"key": "black", "color_name": "Black", "rgba": "000000FF"},
            ],
            "variants": [
                {"color_key": "gold", "size_key": "kg1", "price_override": 26.0},
                {"color_key": "black", "size_key": "kg1"},
            ],
        }
        product = (await async_client.post(API, json=document)).json()

        assert _variant(product, "Gold", 1000)["effective_price"] == 26.0
        assert _variant(product, "Black", 1000)["effective_price"] == 20.0

    @pytest.mark.asyncio
    async def test_quantity_is_bounded(self, async_client: AsyncClient):
        product = await _new_product(async_client)
        variant = _variant(product, "Black", 1000)

        response = await async_client.post(f"{API}/variants/{variant['id']}/intake", json={"quantity": 0})
        assert response.status_code == 422


class TestEditingTheProduct:
    @pytest.mark.asyncio
    async def test_master_data_reaches_the_active_spools(self, async_client: AsyncClient, db_session):
        product = await _new_product(async_client)
        variant = _variant(product, "Black", 1000)
        ids = (
            await async_client.post(
                f"{API}/variants/{variant['id']}/intake", json={"quantity": 2, "price_per_spool": 20.0}
            )
        ).json()["spool_ids"]
        archived = await _reload(db_session, ids[1])
        archived.archived_at = datetime.now()
        await db_session.commit()

        document = _document(product)
        document["material_number"] = "77"
        document["slicer_filament"] = "GFA01"
        response = await async_client.put(f"{API}/{product['id']}", json=document)
        assert response.status_code == 200, response.text
        assert response.json()["spools_updated"] == 1

        active = await _reload(db_session, ids[0])
        assert (active.material_number, active.slicer_filament) == ("77", "GFA01")
        # History stays what it was.
        old = await _reload(db_session, ids[1])
        assert (old.material_number, old.slicer_filament) == ("52", None)

    @pytest.mark.asyncio
    async def test_a_spool_keeps_its_own_nozzle_range(self, async_client: AsyncClient, db_session):
        """The product does not carry a nozzle range: a range set on a spool
        stays, even when an older editor still sends one along."""
        product = await _new_product(async_client)
        spool_id = (
            await async_client.post(
                f"{API}/variants/{_variant(product, 'Black', 1000)['id']}/intake", json={"quantity": 1}
            )
        ).json()["spool_ids"][0]
        spool = await _reload(db_session, spool_id)
        spool.nozzle_temp_min, spool.nozzle_temp_max = 205, 225
        await db_session.commit()

        document = _document(product)
        document["material_number"] = "77"
        document.update(nozzle_temp_min=190, nozzle_temp_max=230)
        response = await async_client.put(f"{API}/{product['id']}", json=document)

        assert response.status_code == 200, response.text
        assert "nozzle_temp_min" not in response.json()["product"]
        spool = await _reload(db_session, spool_id)
        assert (spool.material_number, spool.nozzle_temp_min, spool.nozzle_temp_max) == ("77", 205, 225)

    @pytest.mark.asyncio
    async def test_a_colour_edit_reaches_only_that_colour(self, async_client: AsyncClient, db_session):
        product = await _new_product(async_client)
        black_id = (
            await async_client.post(
                f"{API}/variants/{_variant(product, 'Black', 1000)['id']}/intake", json={"quantity": 1}
            )
        ).json()["spool_ids"][0]
        white_id = (
            await async_client.post(
                f"{API}/variants/{_variant(product, 'White', 1000)['id']}/intake", json={"quantity": 1}
            )
        ).json()["spool_ids"][0]

        document = _document(product)
        for color in document["colors"]:
            if color["color_name"] == "Black":
                color["color_name"] = "Charcoal"
        await async_client.put(f"{API}/{product['id']}", json=document)

        assert (await _reload(db_session, black_id)).color_name == "Charcoal"
        assert (await _reload(db_session, white_id)).color_name == "White"

    @pytest.mark.asyncio
    async def test_a_new_price_never_reprices_the_shelf(self, async_client: AsyncClient, db_session):
        product = await _new_product(async_client)
        variant = _variant(product, "Black", 1000)
        old_id = (
            await async_client.post(
                f"{API}/variants/{variant['id']}/intake", json={"quantity": 1, "price_per_spool": 20.0}
            )
        ).json()["spool_ids"][0]

        document = _document(product)
        for size in document["sizes"]:
            if size["label_weight"] == 1000:
                size["price"] = 24.0
        response = await async_client.put(f"{API}/{product['id']}", json=document)
        assert response.json()["spools_updated"] == 0

        assert (await _reload(db_session, old_id)).cost_per_kg == 20.0
        product = response.json()["product"]
        new_id = (
            await async_client.post(
                f"{API}/variants/{variant['id']}/intake",
                json={"quantity": 1, "price_per_spool": _variant(product, "Black", 1000)["effective_price"]},
            )
        ).json()["spool_ids"][0]
        assert (await _reload(db_session, new_id)).cost_per_kg == 24.0

    @pytest.mark.asyncio
    async def test_a_combination_with_spools_cannot_be_removed(self, async_client: AsyncClient):
        product = await _new_product(async_client)
        variant = _variant(product, "Black", 5000)
        await async_client.post(f"{API}/variants/{variant['id']}/intake", json={"quantity": 1})

        document = _document(product)
        document["variants"] = [
            v
            for v in document["variants"]
            if v["size_key"] != f"s{variant['size_id']}" or v["color_key"] != f"c{variant['color_id']}"
        ]
        response = await async_client.put(f"{API}/{product['id']}", json=document)
        assert response.status_code == 409
        assert response.json()["detail"]["spool_count"] == 1

    @pytest.mark.asyncio
    async def test_an_empty_combination_can_be_removed(self, async_client: AsyncClient):
        product = await _new_product(async_client)
        variant = _variant(product, "White", 1000)

        document = _document(product)
        document["variants"] = [v for v in document["variants"] if v["color_key"] != f"c{variant['color_id']}"]
        response = await async_client.put(f"{API}/{product['id']}", json=document)
        assert response.status_code == 200, response.text
        assert len(response.json()["product"]["variants"]) == 2

    @pytest.mark.asyncio
    async def test_a_size_listed_twice_is_refused(self, async_client: AsyncClient):
        response = await async_client.post(
            API,
            json={
                "material": "PLA",
                "sizes": [{"key": "a", "label_weight": 1000}, {"key": "b", "label_weight": 1000}],
            },
        )
        assert response.status_code == 400

    @pytest.mark.asyncio
    async def test_deleting_a_product_keeps_its_spools(self, async_client: AsyncClient, db_session):
        product = await _new_product(async_client)
        variant = _variant(product, "Black", 1000)
        spool_id = (await async_client.post(f"{API}/variants/{variant['id']}/intake", json={"quantity": 1})).json()[
            "spool_ids"
        ][0]

        response = await async_client.delete(f"{API}/{product['id']}")
        assert response.status_code == 200
        assert response.json()["spools_unlinked"] == 1

        spool = await _reload(db_session, spool_id)
        assert spool.variant_id is None
        assert spool.color_name == "Black"


class TestCodes:
    @pytest.mark.asyncio
    async def test_a_code_is_learnt_once_and_recognised_after(self, async_client: AsyncClient):
        product = await _new_product(async_client)
        variant = _variant(product, "White", 1000)

        assert (await async_client.get(f"{API}/lookup", params={"code": "4012345678901"})).status_code == 404
        response = await async_client.post(f"{API}/variants/{variant['id']}/codes", json={"code": " 4012345678901 "})
        assert response.status_code == 200, response.text

        found = (await async_client.get(f"{API}/lookup", params={"code": "4012345678901"})).json()
        assert found["variant_id"] == variant["id"]
        assert found["product"]["id"] == product["id"]

    @pytest.mark.asyncio
    async def test_one_code_names_one_variant(self, async_client: AsyncClient):
        product = await _new_product(async_client)
        await async_client.post(f"{API}/variants/{_variant(product, 'White', 1000)['id']}/codes", json={"code": "X1"})

        response = await async_client.post(
            f"{API}/variants/{_variant(product, 'Black', 1000)['id']}/codes", json={"code": "X1"}
        )
        assert response.status_code == 409
        assert "White" in response.json()["detail"]["label"]
