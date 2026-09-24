"""What the File Manager root shows, and the counts the choice needs.

`library_root_view` is one of `all` / `folders` / `recent` and defaults to
`all` — exactly today's behaviour, so no existing install changes on upgrade.
Three things have to hold for the setting to work:

  * it survives a round trip as one of the three strings, including from a row
    whose stored value is none of them. The settings table stores strings, and
    the PUT/PATCH path writes the literal "None" for an explicit null; without
    the ``_coerce_library_root_view`` fallback that raw string reaches the
    response model, fails validation and takes the *whole* settings response
    down with it — the #2905 failure mode, one endpoint the entire app depends
    on.
  * a value outside the three is refused on the way in, rather than stored and
    silently reinterpreted on the way out.
  * ``/library/stats`` reports how many files sit in no folder at all, per
    bucket. The root offers a "No folder" entry only when there is something
    behind it, and asking the listing endpoint that question would issue the
    very all-files query the setting exists to avoid.
"""

import pytest
from httpx import AsyncClient

from backend.app.models.library import LibraryFile, LibraryFolder


@pytest.mark.asyncio
@pytest.mark.integration
async def test_root_view_defaults_to_all(async_client: AsyncClient):
    """An install that has never touched the setting keeps its old root."""
    response = await async_client.get("/api/v1/settings/")
    assert response.status_code == 200
    assert response.json()["library_root_view"] == "all"


@pytest.mark.asyncio
@pytest.mark.integration
@pytest.mark.parametrize("view", ["folders", "recent", "all"])
async def test_root_view_round_trips(async_client: AsyncClient, view: str):
    """Each of the three reads back from the PATCH echo and from a fresh GET."""
    patched = await async_client.patch("/api/v1/settings/", json={"library_root_view": view})
    assert patched.status_code == 200
    assert patched.json()["library_root_view"] == view

    reread = await async_client.get("/api/v1/settings/")
    assert reread.status_code == 200
    assert reread.json()["library_root_view"] == view


@pytest.mark.asyncio
@pytest.mark.integration
async def test_a_fourth_value_is_refused(async_client: AsyncClient):
    """Refused on the way in, so nothing unreadable is ever stored."""
    response = await async_client.patch("/api/v1/settings/", json={"library_root_view": "everything"})
    assert response.status_code == 422

    reread = await async_client.get("/api/v1/settings/")
    assert reread.json()["library_root_view"] == "all"


@pytest.mark.asyncio
@pytest.mark.integration
async def test_a_stored_none_does_not_take_the_settings_response_down(async_client: AsyncClient):
    """An unreadable row falls back to the default instead of 500ing.

    A null in the payload is stored as the literal string "None" (see the
    ``value is None`` branch of ``update_settings``). Only the
    ``_coerce_library_root_view`` fallback turns that back into a view;
    without it the raw string reaches the response model, fails validation
    and 500s every settings read — not just this one field.
    """
    await async_client.patch("/api/v1/settings/", json={"library_root_view": "recent"})
    patched = await async_client.patch("/api/v1/settings/", json={"library_root_view": None})
    assert patched.status_code == 200
    assert patched.json()["library_root_view"] == "all"

    reread = await async_client.get("/api/v1/settings/")
    assert reread.status_code == 200
    assert reread.json()["library_root_view"] == "all"


@pytest.mark.asyncio
@pytest.mark.integration
async def test_stats_count_files_outside_any_folder_per_bucket(async_client: AsyncClient, db_session):
    """Only ``folder_id IS NULL`` rows count, split managed / external."""
    folder = LibraryFolder(name="Kunden")
    db_session.add(folder)
    await db_session.flush()

    db_session.add_all(
        [
            LibraryFile(filename="loose.3mf", file_path="library/loose.3mf", file_type="3mf", file_size=1),
            LibraryFile(filename="also.stl", file_path="library/also.stl", file_type="stl", file_size=1),
            LibraryFile(
                filename="in-folder.3mf",
                file_path="library/in-folder.3mf",
                file_type="3mf",
                file_size=1,
                folder_id=folder.id,
            ),
            LibraryFile(
                filename="stray.3mf",
                file_path="/mnt/nas/stray.3mf",
                file_type="3mf",
                file_size=1,
                is_external=True,
            ),
        ]
    )
    await db_session.commit()

    response = await async_client.get("/api/v1/library/stats")
    assert response.status_code == 200
    result = response.json()
    assert result["unfoldered_files"] == 2
    assert result["unfoldered_external_files"] == 1
    # The headline figures still count the whole library.
    assert result["total_files"] == 4
    assert result["total_folders"] == 1


@pytest.mark.asyncio
@pytest.mark.integration
async def test_stats_report_zero_unfoldered_when_everything_is_filed(async_client: AsyncClient, db_session):
    """No "No folder" entry to offer — the keys must still be there, as 0."""
    folder = LibraryFolder(name="Kunden")
    db_session.add(folder)
    await db_session.flush()
    db_session.add(
        LibraryFile(
            filename="filed.3mf",
            file_path="library/filed.3mf",
            file_type="3mf",
            file_size=1,
            folder_id=folder.id,
        )
    )
    await db_session.commit()

    result = (await async_client.get("/api/v1/library/stats")).json()
    assert result["unfoldered_files"] == 0
    assert result["unfoldered_external_files"] == 0


@pytest.mark.asyncio
@pytest.mark.integration
async def test_stats_ignore_trashed_files_outside_folders(async_client: AsyncClient, db_session):
    """A trashed loose file is not a reason to offer "No folder"."""
    from datetime import datetime

    db_session.add(
        LibraryFile(
            filename="deleted.3mf",
            file_path="library/deleted.3mf",
            file_type="3mf",
            file_size=1,
            deleted_at=datetime.utcnow(),
        )
    )
    await db_session.commit()

    result = (await async_client.get("/api/v1/library/stats")).json()
    assert result["unfoldered_files"] == 0
