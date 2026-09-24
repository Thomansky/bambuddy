"""The File Manager root can show folders instead of every file.

`library_root_lists_all_files` defaults to True — exactly today's behaviour, so
no existing install changes on upgrade — and the frontend turns it off to make
the root list its top-level folders instead. Two things have to hold for that
to work:

  * the flag survives a round trip as a *bool*, including from a row whose
    stored value is not a boolean the response model can parse. The settings
    table stores strings, and the PUT/PATCH path writes the literal "None" for
    an explicit null; a key missing from the boolean list in
    ``_build_settings_response`` reaches ``AppSettings(**settings_dict)`` as
    that raw string, and validation then fails and takes the *whole* settings
    response down with it — the #2905 failure mode, one endpoint the entire
    app depends on. (Pydantic coerces "true"/"false" itself, so the happy
    round trip alone does not pin that list entry.)
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
async def test_root_lists_all_files_defaults_to_true(async_client: AsyncClient):
    """An install that has never touched the setting keeps its old root."""
    response = await async_client.get("/api/v1/settings/")
    assert response.status_code == 200
    assert response.json()["library_root_lists_all_files"] is True


@pytest.mark.asyncio
@pytest.mark.integration
async def test_root_lists_all_files_round_trips_as_a_bool(async_client: AsyncClient):
    """Off must read back as False, from the PATCH echo and from a fresh GET."""
    patched = await async_client.patch("/api/v1/settings/", json={"library_root_lists_all_files": False})
    assert patched.status_code == 200
    assert patched.json()["library_root_lists_all_files"] is False

    reread = await async_client.get("/api/v1/settings/")
    assert reread.status_code == 200
    assert reread.json()["library_root_lists_all_files"] is False

    back_on = await async_client.patch("/api/v1/settings/", json={"library_root_lists_all_files": True})
    assert back_on.json()["library_root_lists_all_files"] is True


@pytest.mark.asyncio
@pytest.mark.integration
async def test_a_stored_none_does_not_take_the_settings_response_down(async_client: AsyncClient):
    """The key belongs in the boolean list in ``_build_settings_response``.

    A null in the payload is stored as the literal string "None" (see the
    ``value is None`` branch of ``update_settings``). Only the boolean list
    turns that back into a bool; without it the raw string reaches the
    response model, fails validation and 500s every settings read — not just
    this one field.
    """
    patched = await async_client.patch("/api/v1/settings/", json={"library_root_lists_all_files": None})
    assert patched.status_code == 200
    assert patched.json()["library_root_lists_all_files"] is False

    reread = await async_client.get("/api/v1/settings/")
    assert reread.status_code == 200
    assert reread.json()["library_root_lists_all_files"] is False


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
