"""An explicit null for one of the fork's choice settings is refused.

The update path stores a null as the literal string "None". For
``price_vat_basis`` that string fails the ``^(gross|net)$`` pattern when the
settings response is built, so every later read of /settings 500s until the row
is fixed by hand. ``library_storage_mode`` and ``webdav_mode`` survive the read
through their fallbacks, but the fallback is the silent part: a stored "None"
reads as ``managed`` and takes a directory-mode library off its share (new
uploads land in the managed store while existing rows still point into the
tree), or reads as ``off`` and closes the WebDAV share. Upstream's
``_ENUM_SETTING_KEYS`` guard answers 422 instead, so these keys belong in it.
"""

import pytest
from httpx import AsyncClient
from sqlalchemy import select

from backend.app.models.settings import Settings

pytestmark = pytest.mark.integration

FORK_ENUM_KEYS = ["price_vat_basis", "library_storage_mode", "webdav_mode"]


async def _stored(db_session, key: str) -> str | None:
    """The raw row value as the database has it now, or None when unset."""
    db_session.expire_all()
    row = (await db_session.execute(select(Settings).where(Settings.key == key))).scalar_one_or_none()
    return None if row is None else row.value


@pytest.mark.asyncio
@pytest.mark.parametrize("key", FORK_ENUM_KEYS)
async def test_a_null_is_refused_and_nothing_is_stored(async_client: AsyncClient, db_session, key):
    """The whole request is refused, so the field sent alongside is not applied."""
    before = (await async_client.get("/api/v1/settings/")).json()
    assert before["currency"] != "EUR"

    response = await async_client.put("/api/v1/settings/", json={key: None, "currency": "EUR"})

    assert response.status_code == 422
    assert key in response.json()["detail"]
    assert await _stored(db_session, key) is None
    after = await async_client.get("/api/v1/settings/")
    assert after.status_code == 200
    assert after.json()[key] == before[key]
    assert after.json()["currency"] == before["currency"]


@pytest.mark.asyncio
async def test_a_null_vat_basis_does_not_take_the_settings_response_down(async_client: AsyncClient, db_session):
    """Without the guard the stored "None" fails the pattern on every read."""
    saved = await async_client.put("/api/v1/settings/", json={"price_vat_basis": "net"})
    assert saved.status_code == 200

    response = await async_client.put("/api/v1/settings/", json={"price_vat_basis": None})

    assert response.status_code == 422
    assert await _stored(db_session, "price_vat_basis") == "net"
    reread = await async_client.get("/api/v1/settings/")
    assert reread.status_code == 200
    assert reread.json()["price_vat_basis"] == "net"


@pytest.mark.asyncio
async def test_a_null_does_not_take_a_directory_library_off_its_share(
    async_client: AsyncClient, db_session, tmp_path, monkeypatch
):
    """The storage switch reads a null as "unchanged" and would let it through;
    the stored "None" would then read as managed. The guard refuses it first."""
    monkeypatch.setenv("BAMBUDDY_EXTERNAL_ROOTS", str(tmp_path.parent))
    tree = tmp_path / "nas" / "Bambuddy"
    tree.mkdir(parents=True)
    db_session.add(Settings(key="library_storage_mode", value="directory"))
    db_session.add(Settings(key="library_storage_path", value=str(tree)))
    await db_session.commit()

    response = await async_client.put("/api/v1/settings/", json={"library_storage_mode": None})

    assert response.status_code == 422
    assert await _stored(db_session, "library_storage_mode") == "directory"
    reread = await async_client.get("/api/v1/settings/")
    assert reread.status_code == 200
    assert reread.json()["library_storage_mode"] == "directory"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("key", "value"),
    [("price_vat_basis", "net"), ("library_storage_mode", "managed"), ("webdav_mode", "read")],
)
async def test_a_valid_value_still_saves(async_client: AsyncClient, db_session, key, value):
    response = await async_client.put("/api/v1/settings/", json={key: value})

    assert response.status_code == 200
    assert response.json()[key] == value
    assert await _stored(db_session, key) == value
    reread = await async_client.get("/api/v1/settings/")
    assert reread.json()[key] == value
