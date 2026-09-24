"""Migration coverage for the three-way File Manager root view.

The boolean ``library_root_lists_all_files`` becomes the string
``library_root_view``. An install that had switched the root to its folders
must come out on ``folders``; one that never touched the setting must come out
on ``all``, which is the default and today's behaviour.
"""

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

import backend.app.models  # noqa: F401 - populate Base.metadata
import backend.app.models.external_link  # noqa: F401 - required by a legacy ALTER in run_migrations
import backend.app.models.print_log  # noqa: F401 - required by a legacy ALTER in run_migrations
import backend.app.models.virtual_printer  # noqa: F401 - required by a legacy ALTER in run_migrations
from backend.app.core.database import Base, run_migrations


@pytest.fixture(autouse=True)
def force_sqlite_dialect(monkeypatch):
    """The engine below is SQLite, but settings.database_url may point at Postgres in a
    dev config — and run_migrations branches on the global dialect, not on the
    connection. Without this the Postgres branch runs against SQLite and the migration
    fails on Postgres-only syntax. Same fixture as test_ldap_migration.py."""
    from backend.app.core import db_dialect

    monkeypatch.setattr(db_dialect, "is_sqlite", lambda: True)
    monkeypatch.setattr(db_dialect, "is_postgres", lambda: False)
    # database.py imported is_sqlite at module load time — patch there too.
    from backend.app.core import database as database_module

    monkeypatch.setattr(database_module, "is_sqlite", lambda: True)


async def _migrate_with(tmp_path, name: str, stored: str | None) -> dict[str, str]:
    """Run the migrations over a database holding *stored* for the old key."""
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / name}")
    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
            if stored is not None:
                await conn.execute(
                    text("INSERT INTO settings (key, value) VALUES (:k, :v)"),
                    {"k": "library_root_lists_all_files", "v": stored},
                )
            await run_migrations(conn)
            rows = (
                await conn.execute(
                    text("SELECT key, value FROM settings WHERE key IN (:old, :new)"),
                    {"old": "library_root_lists_all_files", "new": "library_root_view"},
                )
            ).all()
        return dict(rows)
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_a_stored_false_becomes_the_folders_view(tmp_path):
    rows = await _migrate_with(tmp_path, "root-view-false.db", "false")
    assert rows["library_root_view"] == "folders"
    # The old key is gone: the new one is the single source afterwards.
    assert "library_root_lists_all_files" not in rows


@pytest.mark.asyncio
async def test_a_stored_true_becomes_the_all_view(tmp_path):
    rows = await _migrate_with(tmp_path, "root-view-true.db", "true")
    assert rows["library_root_view"] == "all"
    assert "library_root_lists_all_files" not in rows


@pytest.mark.asyncio
async def test_an_install_that_never_set_it_gets_no_row_and_the_default(tmp_path):
    """Absent means default, and the default is ``all`` — nothing to write."""
    rows = await _migrate_with(tmp_path, "root-view-absent.db", None)
    assert rows == {}


@pytest.mark.asyncio
async def test_an_unreadable_row_keeps_the_root_it_was_showing(tmp_path):
    """The literal "None" an explicit null stored read as False, i.e. folders."""
    rows = await _migrate_with(tmp_path, "root-view-none.db", "None")
    assert rows["library_root_view"] == "folders"
