"""Product master data, stage three (#3165): target stock and reordering.

Every combination can carry a target — how many spools of it should be on the
shelf. A roll below the low-stock threshold is on the shelf but no longer
stock. What is missing, less what the shopping list already waits for, is the
reorder list; its lines go onto the shopping list tied to their combination
and supplier, and goods-in of that combination ticks them off again.
"""

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.models.settings import Settings
from backend.app.models.shopping_list import ShoppingListItem
from backend.app.models.spool import Spool

pytestmark = pytest.mark.integration

API = "/api/v1/inventory/products"
SHOPPING = "/api/v1/inventory/shopping-list"


async def _supplier(client: AsyncClient, name: str) -> int:
    response = await client.post("/api/v1/inventory/suppliers", json={"name": name})
    assert response.status_code == 201, response.text
    return response.json()["id"]


async def _product(client: AsyncClient, **overrides) -> dict:
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
            {"color_key": "black", "size_key": "kg1", "min_stock": 2},
            {"color_key": "black", "size_key": "kg5"},
            {"color_key": "white", "size_key": "kg1", "min_stock": 1},
        ],
    }
    document.update(overrides)
    response = await client.post(API, json=document)
    assert response.status_code == 200, response.text
    return response.json()


def _variant(product: dict, color_name: str, label_weight: int) -> dict:
    color = next(c for c in product["colors"] if c["color_name"] == color_name)
    size = next(s for s in product["sizes"] if s["label_weight"] == label_weight)
    return next(v for v in product["variants"] if v["color_id"] == color["id"] and v["size_id"] == size["id"])


def _document(product: dict, *, drop: tuple[str, int] | None = None) -> dict:
    """The editor's save document for a product as the API returned it —
    targets and suppliers included."""
    kept = [v for v in product["variants"] if drop is None or v["id"] != _variant(product, drop[0], drop[1])["id"]]
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
            {
                "color_key": f"c{v['color_id']}",
                "size_key": f"s{v['size_id']}",
                "price_override": v["price_override"],
                "min_stock": v["min_stock"],
            }
            for v in kept
        ],
        "suppliers": [
            {"supplier_id": row["supplier_id"], "preferred": row["preferred"]} for row in product["suppliers"]
        ],
    }


async def _intake(client: AsyncClient, variant_id: int, quantity: int, **extra) -> dict:
    response = await client.post(f"{API}/variants/{variant_id}/intake", json={"quantity": quantity, **extra})
    assert response.status_code == 200, response.text
    return response.json()


async def _use(db: AsyncSession, spool_id: int, grams: float, own_threshold: int | None = None) -> None:
    spool = (await db.execute(select(Spool).where(Spool.id == spool_id))).scalar_one()
    spool.weight_used = grams
    spool.low_stock_threshold_pct = own_threshold
    await db.commit()


async def _fetch(client: AsyncClient, product_id: int) -> dict:
    response = await client.get(f"{API}/{product_id}")
    assert response.status_code == 200, response.text
    return response.json()


async def _reorder(client: AsyncClient) -> list[dict]:
    response = await client.get(f"{API}/reorder")
    assert response.status_code == 200, response.text
    return response.json()


class TestTargetStock:
    @pytest.mark.asyncio
    async def test_a_target_is_kept_per_combination(self, async_client: AsyncClient):
        product = await _product(async_client)

        black = _variant(product, "Black", 1000)
        assert black["min_stock"] == 2
        assert (black["in_stock"], black["on_order"], black["shortfall"]) == (0, 0, 2)
        big = _variant(product, "Black", 5000)
        assert big["min_stock"] is None
        assert big["shortfall"] == 0

    @pytest.mark.asyncio
    async def test_an_edit_changes_and_clears_a_target(self, async_client: AsyncClient):
        product = await _product(async_client)
        document = _document(product)
        for row in document["variants"]:
            row["min_stock"] = 0 if row["min_stock"] == 2 else 3

        response = await async_client.put(f"{API}/{product['id']}", json=document)

        assert response.status_code == 200, response.text
        saved = response.json()["product"]
        assert _variant(saved, "Black", 1000)["min_stock"] is None
        assert _variant(saved, "Black", 5000)["min_stock"] == 3

    @pytest.mark.asyncio
    async def test_a_nearly_empty_roll_is_no_longer_stock(self, async_client: AsyncClient, db_session: AsyncSession):
        product = await _product(async_client)
        black = _variant(product, "Black", 1000)
        spool_ids = (await _intake(async_client, black["id"], 2))["spool_ids"]
        # 10 % left: below the default threshold of 20 %.
        await _use(db_session, spool_ids[0], 900)

        black = _variant(await _fetch(async_client, product["id"]), "Black", 1000)

        assert (black["spool_count"], black["in_stock"], black["shortfall"]) == (2, 1, 1)

    @pytest.mark.asyncio
    async def test_a_spools_own_threshold_wins(self, async_client: AsyncClient, db_session: AsyncSession):
        product = await _product(async_client)
        black = _variant(product, "Black", 1000)
        spool_ids = (await _intake(async_client, black["id"], 2))["spool_ids"]
        await _use(db_session, spool_ids[0], 900, own_threshold=5)

        black = _variant(await _fetch(async_client, product["id"]), "Black", 1000)

        assert black["in_stock"] == 2
        assert black["shortfall"] == 0

    @pytest.mark.asyncio
    async def test_the_inventory_threshold_is_honoured(self, async_client: AsyncClient, db_session: AsyncSession):
        db_session.add(Settings(key="low_stock_threshold", value="50"))
        await db_session.commit()
        product = await _product(async_client)
        black = _variant(product, "Black", 1000)
        spool_ids = (await _intake(async_client, black["id"], 2))["spool_ids"]
        await _use(db_session, spool_ids[0], 600)  # 40 % left

        black = _variant(await _fetch(async_client, product["id"]), "Black", 1000)

        assert black["in_stock"] == 1


class TestReorderList:
    @pytest.mark.asyncio
    async def test_lists_what_is_missing_with_the_usual_supplier_first(self, async_client: AsyncClient):
        shop = await _supplier(async_client, "Filament Shop")
        other = await _supplier(async_client, "Other Shop")
        await _product(
            async_client,
            suppliers=[{"supplier_id": other}, {"supplier_id": shop, "preferred": True}],
        )

        lines = await _reorder(async_client)

        assert [(line["color_name"], line["label_weight"], line["shortfall"]) for line in lines] == [
            ("Black", 1000, 2),
            ("White", 1000, 1),
        ]
        black = lines[0]
        assert black["product_label"] == "Bambu Lab PLA Matte"
        assert black["material_number"] == "52"
        assert (black["min_stock"], black["in_stock"], black["on_order"]) == (2, 0, 0)
        assert black["list_price"] == 20.0
        assert black["suppliers"] == [
            {"supplier_id": shop, "supplier_name": "Filament Shop", "preferred": True},
            {"supplier_id": other, "supplier_name": "Other Shop", "preferred": False},
        ]

    @pytest.mark.asyncio
    async def test_a_combination_at_its_target_is_not_listed(self, async_client: AsyncClient):
        product = await _product(async_client)
        await _intake(async_client, _variant(product, "Black", 1000)["id"], 2)

        lines = await _reorder(async_client)

        assert [line["color_name"] for line in lines] == ["White"]


class TestShoppingList:
    @pytest.mark.asyncio
    async def test_lines_go_onto_the_shopping_list_tied_to_their_combination(self, async_client: AsyncClient):
        shop = await _supplier(async_client, "Filament Shop")
        product = await _product(async_client, suppliers=[{"supplier_id": shop, "preferred": True}])
        black = _variant(product, "Black", 1000)

        response = await async_client.post(
            f"{API}/reorder", json={"items": [{"variant_id": black["id"], "quantity": 2, "supplier_id": shop}]}
        )

        assert response.status_code == 200, response.text
        assert response.json() == {"added": 1, "merged": 0}
        [line] = (await async_client.get(SHOPPING)).json()
        assert line["material"] == "PLA"
        assert line["subtype"] == "Matte"
        assert line["brand"] == "Bambu Lab"
        assert line["color_name"] == "Black"
        assert line["quantity_spools"] == 2
        assert line["note"] == "1 kg · Filament Shop"
        assert line["variant_id"] == black["id"]
        assert line["supplier_id"] == shop

    @pytest.mark.asyncio
    async def test_what_is_on_the_list_counts_as_coming(self, async_client: AsyncClient):
        product = await _product(async_client)
        black = _variant(product, "Black", 1000)

        await async_client.post(f"{API}/reorder", json={"items": [{"variant_id": black["id"], "quantity": 2}]})

        black = _variant(await _fetch(async_client, product["id"]), "Black", 1000)
        assert (black["on_order"], black["shortfall"]) == (2, 0)
        assert [line["color_name"] for line in await _reorder(async_client)] == ["White"]

    @pytest.mark.asyncio
    async def test_ordering_again_grows_the_open_line(self, async_client: AsyncClient):
        product = await _product(async_client)
        black = _variant(product, "Black", 1000)
        payload = {"items": [{"variant_id": black["id"], "quantity": 1}]}

        await async_client.post(f"{API}/reorder", json=payload)
        response = await async_client.post(f"{API}/reorder", json=payload)

        assert response.json() == {"added": 0, "merged": 1}
        [line] = (await async_client.get(SHOPPING)).json()
        assert line["quantity_spools"] == 2

    @pytest.mark.asyncio
    async def test_a_supplier_that_does_not_carry_the_product_is_refused(self, async_client: AsyncClient):
        stranger = await _supplier(async_client, "Somebody Else")
        product = await _product(async_client)

        response = await async_client.post(
            f"{API}/reorder",
            json={
                "items": [
                    {"variant_id": _variant(product, "Black", 1000)["id"], "quantity": 1, "supplier_id": stranger}
                ]
            },
        )

        assert response.status_code == 400
        assert (await async_client.get(SHOPPING)).json() == []

    @pytest.mark.asyncio
    async def test_goods_in_ticks_the_order_off(self, async_client: AsyncClient):
        product = await _product(async_client)
        black = _variant(product, "Black", 1000)
        await async_client.post(f"{API}/reorder", json={"items": [{"variant_id": black["id"], "quantity": 3}]})

        first = await _intake(async_client, black["id"], 2)

        assert first["orders_settled"] == 2
        [line] = (await async_client.get(SHOPPING)).json()
        assert line["quantity_spools"] == 1

        second = await _intake(async_client, black["id"], 2)

        assert second["orders_settled"] == 1
        assert (await async_client.get(SHOPPING)).json() == []

    @pytest.mark.asyncio
    async def test_lines_already_bought_are_settled_first(self, async_client: AsyncClient, db_session: AsyncSession):
        product = await _product(async_client)
        black = _variant(product, "Black", 1000)
        db_session.add(ShoppingListItem(material="PLA", quantity_spools=1, status="pending", variant_id=black["id"]))
        db_session.add(ShoppingListItem(material="PLA", quantity_spools=1, status="purchased", variant_id=black["id"]))
        await db_session.commit()

        await _intake(async_client, black["id"], 1)

        [line] = (await async_client.get(SHOPPING)).json()
        assert line["status"] == "pending"

    @pytest.mark.asyncio
    async def test_a_plain_shopping_list_line_is_left_alone(self, async_client: AsyncClient, db_session: AsyncSession):
        product = await _product(async_client)
        db_session.add(ShoppingListItem(material="PLA", brand="Bambu Lab", color_name="Black", quantity_spools=2))
        await db_session.commit()

        result = await _intake(async_client, _variant(product, "Black", 1000)["id"], 1)

        assert result["orders_settled"] == 0
        [line] = (await async_client.get(SHOPPING)).json()
        assert line["quantity_spools"] == 2

    @pytest.mark.asyncio
    async def test_receiving_a_line_books_it_in_through_the_product(
        self, async_client: AsyncClient, db_session: AsyncSession
    ):
        shop = await _supplier(async_client, "Filament Shop")
        product = await _product(async_client, suppliers=[{"supplier_id": shop, "preferred": True}])
        big = _variant(product, "Black", 5000)
        await async_client.post(
            f"{API}/reorder", json={"items": [{"variant_id": big["id"], "quantity": 2, "supplier_id": shop}]}
        )
        [line] = (await async_client.get(SHOPPING)).json()

        response = await async_client.post(f"{API}/orders/{line['id']}/receive")

        assert response.status_code == 200, response.text
        spool_ids = response.json()["spool_ids"]
        assert len(spool_ids) == 2
        spools = (await db_session.execute(select(Spool).where(Spool.id.in_(spool_ids)))).scalars().all()
        for spool in spools:
            assert spool.variant_id == big["id"]
            assert (spool.label_weight, spool.core_weight) == (5000, 900)
            assert spool.material_number == "52"
            assert spool.cost_per_kg == 18.0  # the 5 kg spool's price, 90
        assert (await async_client.get(SHOPPING)).json() == []

    @pytest.mark.asyncio
    async def test_a_plain_line_cannot_be_received_through_a_product(
        self, async_client: AsyncClient, db_session: AsyncSession
    ):
        item = ShoppingListItem(material="PLA", quantity_spools=1)
        db_session.add(item)
        await db_session.commit()

        response = await async_client.post(f"{API}/orders/{item.id}/receive")

        assert response.status_code == 400

    @pytest.mark.asyncio
    async def test_a_removed_combination_leaves_its_line_as_a_plain_one(self, async_client: AsyncClient):
        product = await _product(async_client)
        white = _variant(product, "White", 1000)
        await async_client.post(f"{API}/reorder", json={"items": [{"variant_id": white["id"], "quantity": 1}]})

        response = await async_client.put(f"{API}/{product['id']}", json=_document(product, drop=("White", 1000)))

        assert response.status_code == 200, response.text
        [line] = (await async_client.get(SHOPPING)).json()
        assert line["variant_id"] is None
        assert line["color_name"] == "White"
