"""The File Manager's "recent" root, served by ``GET /library/files?recent=true``.

A start page rather than a listing: the newest files across the whole library,
ordered by ``COALESCE(fs_modified_at, created_at)`` descending and capped at
``RECENT_ROOT_FILE_LIMIT``. It is a flag on the existing route rather than a
route of its own, so the ownership gate, the internal/external scoping and the
response shape stay the tested ones — these tests pin the three things the flag
itself has to get right:

  * the order, including the fallback for a managed upload that has no
    filesystem mtime;
  * the cap, applied in the query rather than after the rows have crossed the
    wire — the whole point is not to fetch the library;
  * that the folder scoping is dropped, so files inside folders are in the
    answer, while the bucket scoping the root's two halves rely on is not.
"""

from datetime import datetime, timedelta

import pytest
from httpx import AsyncClient
from sqlalchemy import event

from backend.app.api.routes.library import RECENT_ROOT_FILE_LIMIT
from backend.app.models.library import LibraryFile, LibraryFolder

BASE = datetime(2026, 9, 1, 12, 0, 0)


def _file(name: str, **over) -> LibraryFile:
    return LibraryFile(
        filename=name,
        file_path=f"library/{name}",
        file_type="3mf",
        file_size=1,
        **over,
    )


@pytest.mark.asyncio
@pytest.mark.integration
async def test_recent_lists_the_newest_first_across_folders(async_client: AsyncClient, db_session):
    folder = LibraryFolder(name="Kunden")
    db_session.add(folder)
    await db_session.flush()

    db_session.add_all(
        [
            _file("oldest.3mf", fs_modified_at=BASE - timedelta(days=3)),
            _file("filed.3mf", folder_id=folder.id, fs_modified_at=BASE - timedelta(days=1)),
            _file("newest.3mf", fs_modified_at=BASE),
        ]
    )
    await db_session.commit()

    response = await async_client.get("/api/v1/library/files?include_root=false&recent=true")
    assert response.status_code == 200
    # Alphabetically this would be filed / newest / oldest — the order is the
    # timestamp's, and a file inside a folder is in the answer.
    assert [f["filename"] for f in response.json()] == ["newest.3mf", "filed.3mf", "oldest.3mf"]


@pytest.mark.asyncio
@pytest.mark.integration
async def test_recent_falls_back_to_created_at_without_a_filesystem_mtime(async_client: AsyncClient, db_session):
    """A managed upload has no on-disk mtime; its upload time is what it has."""
    db_session.add_all(
        [
            _file("external-old.3mf", fs_modified_at=BASE - timedelta(days=5)),
            _file("uploaded.3mf", created_at=BASE),
        ]
    )
    await db_session.commit()

    response = await async_client.get("/api/v1/library/files?include_root=false&recent=true")
    assert [f["filename"] for f in response.json()] == ["uploaded.3mf", "external-old.3mf"]


@pytest.mark.asyncio
@pytest.mark.integration
async def test_recent_is_capped_in_the_query(async_client: AsyncClient, db_session, test_engine):
    """The cap is the reason for the flag: the rows never leave the database.

    A response of exactly ``RECENT_ROOT_FILE_LIMIT`` rows proves nothing about
    that — a whole-library SELECT followed by ``files[:100]`` in Python looks
    identical from here, while fetching the entire library on every visit to
    the start page. So this reads the statement the database was actually
    given: the row limit has to be in the SQL.
    """
    total = RECENT_ROOT_FILE_LIMIT + 5
    db_session.add_all([_file(f"f{i:04d}.3mf", fs_modified_at=BASE - timedelta(minutes=i)) for i in range(total)])
    await db_session.commit()

    statements: list[tuple[str, object]] = []

    def record(conn, cursor, statement, parameters, context, executemany):
        statements.append((statement, parameters))

    event.listen(test_engine.sync_engine, "before_cursor_execute", record)
    try:
        response = await async_client.get("/api/v1/library/files?include_root=false&recent=true")
    finally:
        event.remove(test_engine.sync_engine, "before_cursor_execute", record)

    body = response.json()
    assert len(body) == RECENT_ROOT_FILE_LIMIT
    # The newest end is the end that is kept.
    assert body[0]["filename"] == "f0000.3mf"

    listings = [(sql, params) for sql, params in statements if "FROM library_files" in sql and "ORDER BY" in sql]
    assert listings, f"no library_files listing was issued: {statements}"
    # SQLAlchemy binds the limit, so the number is in the parameters rather
    # than in the SQL text on every dialect.
    for sql, params in listings:
        assert "LIMIT" in sql, sql
        bound = tuple(params.values()) if isinstance(params, dict) else tuple(params or ())
        assert RECENT_ROOT_FILE_LIMIT in bound, (sql, params)

    # Without the flag the same request is the all-files listing it always was.
    plain = await async_client.get("/api/v1/library/files?include_root=false")
    assert len(plain.json()) == total


@pytest.mark.asyncio
@pytest.mark.integration
async def test_recent_still_honours_the_bucket_scoping(async_client: AsyncClient, db_session):
    """The root's managed / external halves each keep their own recent list."""
    db_session.add_all(
        [
            _file("managed.3mf", fs_modified_at=BASE - timedelta(days=1)),
            _file("/mnt/nas/stray.3mf", is_external=True, fs_modified_at=BASE),
        ]
    )
    await db_session.commit()

    internal = await async_client.get("/api/v1/library/files?include_root=false&recent=true&internal_only=true")
    assert [f["filename"] for f in internal.json()] == ["managed.3mf"]

    external = await async_client.get("/api/v1/library/files?include_root=false&recent=true&external_only=true")
    assert [f["filename"] for f in external.json()] == ["/mnt/nas/stray.3mf"]


@pytest.mark.asyncio
@pytest.mark.integration
async def test_recent_leaves_trashed_files_out(async_client: AsyncClient, db_session):
    db_session.add_all(
        [
            _file("live.3mf", fs_modified_at=BASE - timedelta(days=1)),
            _file("binned.3mf", fs_modified_at=BASE, deleted_at=BASE),
        ]
    )
    await db_session.commit()

    response = await async_client.get("/api/v1/library/files?include_root=false&recent=true")
    assert [f["filename"] for f in response.json()] == ["live.3mf"]
