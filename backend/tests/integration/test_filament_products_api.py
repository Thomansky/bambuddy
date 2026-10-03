"""Product master data (#3165) for other systems: the flat article list.

An ERP, a cost calculator or a shop reads the product master one row per
colour × size, with the product's master data, the price, the stock and the
codes on the row. It is a read endpoint, so an API key with the read-status
permission reaches it and one without does not.
"""

import pytest
from httpx import AsyncClient
from sqlalchemy import select

from backend.app.models.filament_product import FilamentProductSupport

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


def _document(product: dict, **overrides) -> dict:
    """The editor's save document for a product as the API returned it."""
    fields = ("brand", "material", "subtype", "material_number", "slicer_filament", "slicer_filament_name")
    document = {
        **{key: product[key] for key in fields},
        "sizes": [
            {
                "id": s["id"],
                "key": f"s{s['id']}",
                "label_weight": s["label_weight"],
                "core_weight": s["core_weight"],
                "price": s["price"],
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
    document.update(overrides)
    return document


class TestPriceDate:
    """When the prices were last checked: one date for the whole product, its
    standard prices at the sizes and its special prices alike."""

    @pytest.mark.asyncio
    async def test_a_product_keeps_its_price_date_and_its_articles_carry_it(self, async_client: AsyncClient):
        product = await _product(async_client, price_date="2026-09-29")

        rows = (await async_client.get(f"{API}/articles")).json()

        assert product["price_date"] == "2026-09-29"
        assert {row["price_date"] for row in rows} == {"2026-09-29"}

    @pytest.mark.asyncio
    async def test_an_edit_without_it_keeps_it_and_null_clears_it(self, async_client: AsyncClient):
        product = await _product(async_client, price_date="2026-09-29")

        kept = await async_client.put(f"{API}/{product['id']}", json=_document(product))
        cleared = await async_client.put(f"{API}/{product['id']}", json=_document(product, price_date=None))

        assert kept.status_code == 200, kept.text
        assert kept.json()["product"]["price_date"] == "2026-09-29"
        assert cleared.json()["product"]["price_date"] is None

    @pytest.mark.asyncio
    async def test_a_product_without_one_has_none(self, async_client: AsyncClient):
        product = await _product(async_client)

        assert product["price_date"] is None


class TestStandardSizeAndRefill:
    """A product marks the size it is usually ordered in, and a weight can be
    sold on a spool and as a refill side by side."""

    SIZES = [
        {"key": "kg1", "label_weight": 1000, "core_weight": 250, "price": 20.0},
        {"key": "kg1r", "label_weight": 1000, "core_weight": 250, "price": 17.0, "refill": True, "standard": True},
    ]
    VARIANTS = [{"color_key": "black", "size_key": "kg1"}, {"color_key": "black", "size_key": "kg1r"}]

    @pytest.mark.asyncio
    async def test_a_weight_can_be_sold_on_a_spool_and_as_a_refill(self, async_client: AsyncClient):
        product = await _product(async_client, sizes=self.SIZES, variants=self.VARIANTS)

        sizes = sorted((s["label_weight"], s["refill"], s["standard"], s["price"]) for s in product["sizes"])
        assert sizes == [(1000, False, False, 20.0), (1000, True, True, 17.0)]

    @pytest.mark.asyncio
    async def test_the_same_size_twice_is_refused(self, async_client: AsyncClient):
        for refill in (False, True):
            twice = [
                {"key": "a", "label_weight": 1000, "refill": refill},
                {"key": "b", "label_weight": 1000, "refill": refill},
            ]
            response = await async_client.post(API, json={"material": "PLA", "sizes": twice})

            assert response.status_code == 400, refill

    @pytest.mark.asyncio
    async def test_one_standard_size_at_most(self, async_client: AsyncClient):
        product = await _product(
            async_client,
            sizes=[
                {"key": "kg1", "label_weight": 1000, "standard": True},
                {"key": "kg5", "label_weight": 5000, "standard": True},
            ],
            variants=[],
        )

        assert {s["label_weight"]: s["standard"] for s in product["sizes"]} == {1000: True, 5000: False}

    @pytest.mark.asyncio
    async def test_the_articles_say_refill_and_standard_size(self, async_client: AsyncClient):
        await _product(async_client, sizes=self.SIZES, variants=self.VARIANTS)

        rows = (await async_client.get(f"{API}/articles")).json()

        assert sorted((row["refill"], row["standard_size"], row["price"]) for row in rows) == [
            (False, False, 20.0),
            (True, True, 17.0),
        ]

    @pytest.mark.asyncio
    async def test_the_shopping_list_names_a_refill(self, async_client: AsyncClient):
        product = await _product(
            async_client,
            sizes=self.SIZES,
            variants=[{"color_key": "black", "size_key": "kg1r", "min_stock": 2}],
        )
        refill = next(v for v in product["variants"] if v["min_stock"] == 2)

        lines = (await async_client.get(f"{API}/reorder")).json()
        await async_client.post(f"{API}/reorder", json={"items": [{"variant_id": refill["id"], "quantity": 2}]})
        [item] = (await async_client.get("/api/v1/inventory/shopping-list")).json()

        assert [line["refill"] for line in lines] == [True]
        assert item["note"] == "1 kg Refill"


class TestSupportMaterial:
    """A product names the support materials that go with it — other products
    of the master — each rated from 1 (poor) to 4 (very good) stars, or not
    rated yet."""

    @staticmethod
    async def _support(client: AsyncClient, material: str, number: str, subtype: str | None = None) -> dict:
        return await _product(client, material=material, subtype=subtype, material_number=number)

    @pytest.mark.asyncio
    async def test_they_come_back_named_the_best_rated_first(self, async_client: AsyncClient):
        pla_support = await self._support(async_client, "Support for PLA", "90")
        pva = await self._support(async_client, "PVA", "91")
        petg = await self._support(async_client, "PETG", "92", "Basic")

        product = await _product(
            async_client,
            supports=[
                {"support_product_id": petg["id"]},
                {"support_product_id": pva["id"], "rating": 2},
                {"support_product_id": pla_support["id"], "rating": 4},
            ],
        )

        assert [(row["label"], row["rating"]) for row in product["supports"]] == [
            ("Bambu Lab Support for PLA", 4),
            ("Bambu Lab PVA", 2),
            ("Bambu Lab PETG Basic", None),
        ]
        listed = next(p for p in (await async_client.get(API)).json() if p["id"] == product["id"])
        assert listed["supports"] == product["supports"]

    @pytest.mark.asyncio
    async def test_an_edit_without_them_keeps_them_and_an_empty_list_clears_them(self, async_client: AsyncClient):
        pva = await self._support(async_client, "PVA", "91")
        product = await _product(async_client, supports=[{"support_product_id": pva["id"], "rating": 3}])
        url = f"{API}/{product['id']}"

        kept = await async_client.put(url, json=_document(product))
        rerated = await async_client.put(
            url, json=_document(product, supports=[{"support_product_id": pva["id"], "rating": 1}])
        )
        cleared = await async_client.put(url, json=_document(product, supports=[]))

        assert kept.status_code == 200, kept.text
        assert [row["rating"] for row in kept.json()["product"]["supports"]] == [3]
        assert [row["rating"] for row in rerated.json()["product"]["supports"]] == [1]
        assert cleared.json()["product"]["supports"] == []

    @pytest.mark.asyncio
    async def test_a_document_that_cannot_be_right_is_refused(self, async_client: AsyncClient):
        pva = await self._support(async_client, "PVA", "91")
        product = await _product(async_client)
        url = f"{API}/{product['id']}"

        for supports in (
            [{"support_product_id": product["id"]}],
            [{"support_product_id": pva["id"]}, {"support_product_id": pva["id"], "rating": 2}],
            [{"support_product_id": 9999}],
        ):
            response = await async_client.put(url, json=_document(product, supports=supports))
            assert response.status_code == 400, supports
        for rating in (0, 5):
            supports = [{"support_product_id": pva["id"], "rating": rating}]
            response = await async_client.put(url, json=_document(product, supports=supports))
            assert response.status_code == 422, rating

    @pytest.mark.asyncio
    async def test_a_deleted_support_material_is_gone_from_the_products_it_went_with(self, async_client: AsyncClient):
        pva = await self._support(async_client, "PVA", "91")
        product = await _product(async_client, supports=[{"support_product_id": pva["id"], "rating": 3}])

        response = await async_client.delete(f"{API}/{pva['id']}")

        assert response.status_code == 200, response.text
        assert (await async_client.get(f"{API}/{product['id']}")).json()["supports"] == []

    @pytest.mark.asyncio
    async def test_a_deleted_product_takes_its_ratings_along(self, async_client: AsyncClient, db_session):
        pva = await self._support(async_client, "PVA", "91")
        product = await _product(async_client, supports=[{"support_product_id": pva["id"], "rating": 3}])

        await async_client.delete(f"{API}/{product['id']}")

        db_session.expire_all()
        assert (await db_session.execute(select(FilamentProductSupport))).scalars().all() == []
        assert (await async_client.get(f"{API}/{pva['id']}")).status_code == 200
