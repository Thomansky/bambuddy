"""A 7-byte NFC UID read only to cascade level 1 still finds its spool.

The SpoolBuddy daemon's PN5180 driver stops after ISO 14443-3 cascade level 1,
so an NTAG with the UID 04 D3 AE 73 D3 2A 81 reaches the backend as 8804D3AE:
the cascade tag 0x88 plus the first three UID bytes. The ESP SpoolBuddy and
phones store all seven bytes, so a third-party spool tagged there was "unknown"
every time it went onto the scale. These tests scan through the real endpoint
against real spools.
"""

from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.models.spool import Spool

API = "/api/v1/spoolbuddy"
FULL_UID = "04D3AE73D32A81"
LEVEL1_UID = "8804D3AE"


@pytest.fixture
def tagged_spool(db_session: AsyncSession):
    async def _create(tag_uid: str, **kwargs) -> Spool:
        spool = Spool(
            material="ABS",
            brand="CR-3D",
            color_name="Black",
            rgba="000000FF",
            label_weight=1000,
            core_weight=250,
            weight_used=0,
            tag_uid=tag_uid,
            tag_type="ntag",
            **kwargs,
        )
        db_session.add(spool)
        await db_session.commit()
        await db_session.refresh(spool)
        return spool

    return _create


async def _scan(async_client: AsyncClient, tag_uid: str) -> dict:
    with patch("backend.app.api.routes.spoolbuddy.ws_manager") as mock_ws:
        mock_ws.broadcast = AsyncMock()
        resp = await async_client.post(
            f"{API}/nfc/tag-scanned",
            json={"device_id": "sb-1", "tag_uid": tag_uid, "sak": 4, "tag_type": "ntag"},
        )
    assert resp.status_code == 200
    return resp.json()


@pytest.mark.asyncio
@pytest.mark.integration
async def test_a_level1_read_finds_the_spool_written_with_the_full_uid(async_client: AsyncClient, tagged_spool):
    spool = await tagged_spool(FULL_UID)

    data = await _scan(async_client, LEVEL1_UID)

    assert data["matched"] is True
    assert data["spool_id"] == spool.id


@pytest.mark.asyncio
@pytest.mark.integration
async def test_a_full_read_finds_a_spool_linked_from_a_level1_read(async_client: AsyncClient, tagged_spool):
    """Once the reader reads all seven bytes, spools linked on the scale keep matching."""
    spool = await tagged_spool(LEVEL1_UID)

    data = await _scan(async_client, FULL_UID)

    assert data["spool_id"] == spool.id


@pytest.mark.asyncio
@pytest.mark.integration
async def test_an_exact_match_still_wins(async_client: AsyncClient, tagged_spool):
    await tagged_spool(FULL_UID)
    linked_on_the_scale = await tagged_spool(LEVEL1_UID)

    data = await _scan(async_client, LEVEL1_UID)

    assert data["spool_id"] == linked_on_the_scale.id


@pytest.mark.asyncio
@pytest.mark.integration
async def test_two_uids_starting_alike_are_not_guessed_between(async_client: AsyncClient, tagged_spool):
    await tagged_spool(FULL_UID)
    await tagged_spool("04D3AE11223344")

    data = await _scan(async_client, LEVEL1_UID)

    assert data["matched"] is False


@pytest.mark.asyncio
@pytest.mark.integration
async def test_a_4_byte_uid_without_the_cascade_tag_is_not_stretched(async_client: AsyncClient, tagged_spool):
    """Only 0x88 marks a level-1 read; any other 4-byte UID is a whole UID."""
    await tagged_spool(FULL_UID)

    data = await _scan(async_client, "1104D3AE")

    assert data["matched"] is False


@pytest.mark.asyncio
@pytest.mark.integration
async def test_an_archived_spool_is_not_matched(async_client: AsyncClient, tagged_spool):
    await tagged_spool(FULL_UID, archived_at=datetime.now(timezone.utc))

    data = await _scan(async_client, LEVEL1_UID)

    assert data["matched"] is False
