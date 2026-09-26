"""A running number for material numbers (#2870 follow-up).

The material number names a *product*: 123 spools of one PLA share "52". So
the series is drawn from on request, for a product that has no number yet —
never on every create, or each of those spools would get its own. What is
pinned here is that the series hands out numbers nobody holds, skips the ones
somebody typed by hand, and stays out of the way while it is switched off.
"""

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.models.number_series import NumberSeries
from backend.app.models.spool import Spool
from backend.app.services.number_series import DEFAULT_SERIES_KEYS, SERIES_MATERIAL

pytestmark = pytest.mark.integration


async def _seed_series(db: AsyncSession, **values) -> NumberSeries:
    series = (await db.execute(select(NumberSeries).where(NumberSeries.key == SERIES_MATERIAL))).scalar_one_or_none()
    if series is None:
        series = NumberSeries(key=SERIES_MATERIAL)
        db.add(series)
    series.enabled = values.get("enabled", True)
    series.prefix = values.get("prefix", "")
    series.suffix = values.get("suffix", "")
    series.next_value = values.get("next_value", 1)
    series.padding = values.get("padding", 0)
    await db.commit()
    return series


async def _spool(db: AsyncSession, number: str) -> None:
    db.add(Spool(material="PLA", material_number=number, label_weight=1000))
    await db.commit()


async def _next_value(db: AsyncSession) -> int:
    db.expire_all()
    return (await db.execute(select(NumberSeries.next_value).where(NumberSeries.key == SERIES_MATERIAL))).scalar_one()


class TestTheSeries:
    def test_every_install_gets_it(self):
        """Seeded with the others, so an upgrade finds it in Settings."""
        assert SERIES_MATERIAL in DEFAULT_SERIES_KEYS

    @pytest.mark.asyncio
    async def test_it_hands_out_the_next_free_number(self, async_client: AsyncClient, db_session):
        await _seed_series(db_session, next_value=77)

        response = await async_client.post("/api/v1/inventory/material-numbers/next")
        assert response.status_code == 200, response.text
        assert response.json()["number"] == "77"
        assert await _next_value(db_session) == 78

    @pytest.mark.asyncio
    async def test_a_number_somebody_typed_by_hand_is_skipped(self, async_client: AsyncClient, db_session):
        """The farm numbered materials by hand before the series existed."""
        await _seed_series(db_session, next_value=52)
        await _spool(db_session, "52")
        await _spool(db_session, "53")

        response = await async_client.post("/api/v1/inventory/material-numbers/next")
        assert response.status_code == 200, response.text
        assert response.json()["number"] == "54"

    @pytest.mark.asyncio
    async def test_prefix_and_padding_apply(self, async_client: AsyncClient, db_session):
        await _seed_series(db_session, next_value=7, prefix="M-", padding=3)

        response = await async_client.post("/api/v1/inventory/material-numbers/next")
        assert response.json()["number"] == "M-007"

    @pytest.mark.asyncio
    async def test_switched_off_it_hands_out_nothing(self, async_client: AsyncClient, db_session):
        await _seed_series(db_session, enabled=False, next_value=77)

        response = await async_client.post("/api/v1/inventory/material-numbers/next")
        assert response.status_code == 409
        assert "off" in response.json()["detail"]
        assert await _next_value(db_session) == 77

    @pytest.mark.asyncio
    async def test_two_requests_never_get_the_same_number(self, async_client: AsyncClient, db_session):
        await _seed_series(db_session, next_value=77)

        first = (await async_client.post("/api/v1/inventory/material-numbers/next")).json()["number"]
        second = (await async_client.post("/api/v1/inventory/material-numbers/next")).json()["number"]
        assert first != second
        assert {first, second} == {"77", "78"}
