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
from backend.app.models.spool_filament_preset import SpoolFilamentPreset
from backend.app.services.spool_filament_preset import resolve_spool_preset
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
    async def test_a_product_keeps_where_it_was_bought(self, async_client: AsyncClient):
        shop = await self._supplier(async_client, "Filament Shop")
        other = await self._supplier(async_client, "Other Shop")
        product = await _product(
            async_client,
            suppliers=[{"supplier_id": other}, {"supplier_id": shop, "preferred": True}],
        )

        # The usual supplier first, and nothing but where it was bought.
        assert product["suppliers"] == [
            {"supplier_id": shop, "supplier_name": "Filament Shop", "preferred": True},
            {"supplier_id": other, "supplier_name": "Other Shop", "preferred": False},
        ]

    @pytest.mark.asyncio
    async def test_a_new_spool_costs_the_manufacturers_price(self, async_client: AsyncClient):
        shop = await self._supplier(async_client, "Filament Shop")
        # What an editor from before still sends: a number and a price at the
        # supplier. Accepted, and neither is kept.
        product = await _product(
            async_client,
            suppliers=[{"supplier_id": shop, "preferred": True, "article_number": "FS-1", "prices": {"kg1": 18.5}}],
        )

        spool = await _new_spool(async_client)

        assert product["suppliers"] == [{"supplier_id": shop, "supplier_name": "Filament Shop", "preferred": True}]
        assert spool["cost_per_kg"] == 20.0

    @pytest.mark.asyncio
    async def test_a_supplier_a_product_uses_cannot_be_deleted(self, async_client: AsyncClient):
        shop = await self._supplier(async_client, "Filament Shop")
        await _product(async_client, suppliers=[{"supplier_id": shop}])

        response = await async_client.delete(f"/api/v1/inventory/suppliers/{shop}")

        assert response.status_code == 409

    @pytest.mark.asyncio
    async def test_the_same_supplier_twice_is_refused(self, async_client: AsyncClient):
        shop = await self._supplier(async_client, "Filament Shop")

        response = await async_client.post(
            API,
            json={"material": "PLA", "suppliers": [{"supplier_id": shop}, {"supplier_id": shop}]},
        )

        assert response.status_code == 400


class TestPresetPerPrinterModel:
    """A cloud or Orca preset is bound to a printer model, so a product names
    one per model. A spool's slot on that model gets it, unless the spool has
    been set up by hand for that model."""

    H2D = {"printer_model": "H2D", "slicer_filament": "GFSA01_02", "slicer_filament_name": "Bambu PLA Matte @BBL H2D"}

    @staticmethod
    def _document(product: dict, **overrides) -> dict:
        fields = ("brand", "material", "subtype", "material_number", "slicer_filament", "slicer_filament_name")
        document = {
            **{key: product[key] for key in fields},
            "sizes": [
                {
                    "id": s["id"],
                    "key": f"s{s['id']}",
                    "label_weight": s["label_weight"],
                    "core_weight": s["core_weight"],
                }
                for s in product["sizes"]
            ],
            "colors": [
                {"id": c["id"], "key": f"c{c['id']}", "color_name": c["color_name"], "rgba": c["rgba"]}
                for c in product["colors"]
            ],
            "variants": [
                {"color_key": f"c{v['color_id']}", "size_key": f"s{v['size_id']}"} for v in product["variants"]
            ],
        }
        document.update(overrides)
        return document

    @pytest.mark.asyncio
    async def test_a_product_keeps_one_per_model(self, async_client: AsyncClient):
        product = await _product(async_client, presets=[self.H2D, {"printer_model": "X1C", "slicer_filament": " "}])

        # A model without a preset is no row.
        assert product["presets"] == [self.H2D]

    @pytest.mark.asyncio
    async def test_an_edit_without_them_keeps_them_and_an_empty_list_clears_them(self, async_client: AsyncClient):
        product = await _product(async_client, presets=[self.H2D])

        kept = await async_client.put(f"{API}/{product['id']}", json=self._document(product))
        cleared = await async_client.put(f"{API}/{product['id']}", json=self._document(product, presets=[]))

        assert kept.status_code == 200, kept.text
        assert kept.json()["product"]["presets"] == [self.H2D]
        assert cleared.json()["product"]["presets"] == []

    @pytest.mark.asyncio
    async def test_a_model_named_twice_is_refused(self, async_client: AsyncClient):
        response = await async_client.post(API, json={"material": "PLA", "presets": [self.H2D, self.H2D]})

        assert response.status_code == 400

    @pytest.mark.asyncio
    async def test_a_spool_gets_its_products_preset_on_that_model(
        self, async_client: AsyncClient, db_session: AsyncSession
    ):
        await _product(
            async_client,
            slicer_filament="GFA01",
            slicer_filament_name="Bambu PLA Matte @BBL H2S",
            presets=[self.H2D],
        )
        spool = await _fresh(db_session, (await _new_spool(async_client))["id"])

        async def resolve(model: str) -> tuple:
            return await resolve_spool_preset(
                db_session,
                spool_id=spool.id,
                printer_model=model,
                nozzle_diameter="0.4",
                fallback_filament=spool.slicer_filament,
                fallback_name=spool.slicer_filament_name,
            )

        assert await resolve("H2D") == ("GFSA01_02", "Bambu PLA Matte @BBL H2D")
        # A model the product names nothing for keeps the spool's own preset.
        assert await resolve("H2S") == ("GFA01", "Bambu PLA Matte @BBL H2S")

        # A spool set up by hand for the model keeps what it was given.
        db_session.add(
            SpoolFilamentPreset(
                spool_id=spool.id,
                printer_model="H2D",
                nozzle_diameter="0.4",
                slicer_filament="PFUS0123",
                slicer_filament_name="My PLA @BBL H2D",
            )
        )
        await db_session.commit()
        assert await resolve("H2D") == ("PFUS0123", "My PLA @BBL H2D")


class TestASpoolOfAWeightSoldTwoWays:
    """A spool only knows its weight. Where a product sells that weight on a
    spool and as a refill, a new spool goes to the standard size if it is one
    of them, else to the one on a spool."""

    @staticmethod
    async def _sold_two_ways(client: AsyncClient, standard: str | None) -> dict:
        sizes = [
            {"key": "kg1", "label_weight": 1000, "core_weight": 250, "price": 20.0},
            {"key": "kg1r", "label_weight": 1000, "core_weight": 250, "price": 17.0, "refill": True},
        ]
        for size in sizes:
            size["standard"] = size["key"] == standard
        return await _product(
            client,
            sizes=sizes,
            variants=[{"color_key": "charcoal", "size_key": "kg1"}, {"color_key": "charcoal", "size_key": "kg1r"}],
        )

    @staticmethod
    def _size_of(product: dict, variant_id: int) -> dict:
        variant = next(v for v in product["variants"] if v["id"] == variant_id)
        return next(s for s in product["sizes"] if s["id"] == variant["size_id"])

    @pytest.mark.asyncio
    async def test_it_goes_to_the_standard_size(self, async_client: AsyncClient, db_session: AsyncSession):
        product = await self._sold_two_ways(async_client, standard="kg1r")

        spool = await _fresh(db_session, (await _new_spool(async_client))["id"])

        assert self._size_of(product, spool.variant_id)["refill"] is True

    @pytest.mark.asyncio
    async def test_a_roll_booked_in_as_a_refill_takes_the_tag(
        self, async_client: AsyncClient, db_session: AsyncSession
    ):
        product = await self._sold_two_ways(async_client, standard=None)
        refill = next(v["id"] for v in product["variants"] if self._size_of(product, v["id"])["refill"])
        [booked] = (
            await async_client.post(f"{API}/variants/{refill}/intake", json={"quantity": 1, "price_per_spool": 17.0})
        ).json()["spool_ids"]

        found = await find_matching_untagged_spool(db_session, _tray())

        assert found is not None
        assert found.id == booked

    @pytest.mark.asyncio
    async def test_without_one_it_goes_to_the_size_on_a_spool(
        self, async_client: AsyncClient, db_session: AsyncSession
    ):
        product = await self._sold_two_ways(async_client, standard=None)

        spool = await _fresh(db_session, (await _new_spool(async_client))["id"])

        assert self._size_of(product, spool.variant_id)["refill"] is False
