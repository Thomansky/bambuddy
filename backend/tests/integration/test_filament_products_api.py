"""Product master data (#3165) for other systems: the flat article list.

An ERP, a cost calculator or a shop reads the product master one row per
colour × size, with the product's master data, the price, the stock and the
codes on the row. It is a read endpoint, so an API key with the read-status
permission reaches it and one without does not.
"""

import pytest
from httpx import AsyncClient

pytestmark = pytest.mark.integration

API = "/api/v1/inventory/products"


async def _product(client: AsyncClient, **overrides) -> dict:
    document = {
        "brand": "Bambu Lab",
        "material": "PLA",
        "subtype": "Matte",
        "material_number": "52",
        "slicer_filament": "GFA01",
        "slicer_filament_name": "Bambu PLA Matte",
        "sizes": [
            {"key": "kg1", "label_weight": 1000, "core_weight": 250, "price": 20.0},
            {"key": "kg5", "label_weight": 5000, "core_weight": 900, "price": 90.0},
        ],
        "colors": [
            {"key": "black", "color_name": "Black", "rgba": "000000FF"},
            {"key": "white", "color_name": "White", "rgba": "FFFFFFFF"},
        ],
        "variants": [
            {"color_key": "black", "size_key": "kg1", "min_stock": 2},
            {"color_key": "black", "size_key": "kg5"},
            {"color_key": "white", "size_key": "kg1", "price_override": 24.0},
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


async def _api_key(db_session, *, can_read_status: bool) -> str:
    from backend.app.core.auth import generate_api_key
    from backend.app.models.api_key import APIKey
    from backend.app.models.settings import Settings

    full_key, key_hash, key_prefix = generate_api_key()
    db_session.add(
        APIKey(
            name="erp",
            key_hash=key_hash,
            key_prefix=key_prefix,
            can_queue=False,
            can_control_printer=False,
            can_read_status=can_read_status,
            enabled=True,
        )
    )
    db_session.add(Settings(key="auth_enabled", value="true"))
    await db_session.commit()
    return full_key


class TestArticles:
    @pytest.mark.asyncio
    async def test_one_row_per_combination_with_its_product(self, async_client: AsyncClient):
        product = await _product(async_client)
        await async_client.post(f"{API}/variants/{_variant_id(product, 'Black', 1000)}/intake", json={"quantity": 1})
        await async_client.post(f"{API}/variants/{_variant_id(product, 'Black', 1000)}/codes", json={"code": "4056"})

        response = await async_client.get(f"{API}/articles")

        assert response.status_code == 200, response.text
        rows = response.json()
        # Colour by colour, lightest size first.
        assert [(row["color_name"], row["label_weight"]) for row in rows] == [
            ("Black", 1000),
            ("Black", 5000),
            ("White", 1000),
        ]
        black = rows[0]
        assert black["variant_id"] == _variant_id(product, "Black", 1000)
        assert black["product_id"] == product["id"]
        assert black["material_number"] == "52"
        assert black["label"] == "Bambu Lab PLA Matte"
        assert (black["rgba"], black["core_weight"]) == ("000000FF", 250)
        assert (black["price"], black["cost_per_kg"]) == (20.0, 20.0)
        assert (black["min_stock"], black["in_stock"], black["spools"], black["shortfall"]) == (2, 1, 1, 1)
        assert black["codes"] == ["4056"]
        assert black["slicer_filament"] == "GFA01"
        # A combination with its own price carries that one.
        assert rows[2]["price"] == 24.0
        assert rows[1]["cost_per_kg"] == 18.0

    @pytest.mark.asyncio
    async def test_narrowed_down_by_material_number_or_product(self, async_client: AsyncClient):
        matte = await _product(async_client)
        await _product(async_client, subtype="Basic", material_number="10")

        by_number = (await async_client.get(f"{API}/articles", params={"material_number": "10"})).json()
        by_product = (await async_client.get(f"{API}/articles", params={"product_id": matte["id"]})).json()

        assert {row["label"] for row in by_number} == {"Bambu Lab PLA Basic"}
        assert {row["product_id"] for row in by_product} == {matte["id"]}
        assert len(by_product) == 3

    @pytest.mark.asyncio
    async def test_the_suppliers_come_with_the_usual_one_first(self, async_client: AsyncClient):
        shop = (await async_client.post("/api/v1/inventory/suppliers", json={"name": "Filament Shop"})).json()["id"]
        other = (await async_client.post("/api/v1/inventory/suppliers", json={"name": "Other Shop"})).json()["id"]
        await _product(async_client, suppliers=[{"supplier_id": other}, {"supplier_id": shop, "preferred": True}])

        rows = (await async_client.get(f"{API}/articles")).json()

        assert rows[0]["suppliers"] == ["Filament Shop", "Other Shop"]


class TestApiKeys:
    @pytest.mark.asyncio
    async def test_a_key_that_may_read_status_reads_the_product_master(self, async_client: AsyncClient, db_session):
        await _product(async_client)
        key = await _api_key(db_session, can_read_status=True)

        articles = await async_client.get(f"{API}/articles", headers={"X-API-Key": key})
        products = await async_client.get(API, headers={"Authorization": f"Bearer {key}"})

        assert articles.status_code == 200, articles.text
        assert len(articles.json()) == 3
        assert products.status_code == 200, products.text
        assert products.json()[0]["material_number"] == "52"

    @pytest.mark.asyncio
    async def test_a_key_without_it_is_refused(self, async_client: AsyncClient, db_session):
        await _product(async_client)
        key = await _api_key(db_session, can_read_status=False)

        response = await async_client.get(f"{API}/articles", headers={"X-API-Key": key})

        assert response.status_code == 403

    @pytest.mark.asyncio
    async def test_nobody_without_a_key_or_login(self, async_client: AsyncClient, db_session):
        await _product(async_client)
        await _api_key(db_session, can_read_status=True)

        response = await async_client.get(f"{API}/articles")

        assert response.status_code == 401
