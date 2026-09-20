"""Migration coverage for the once-per-item pre-dispatch RFID read stamp."""

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

import backend.app.models  # noqa: F401 - populate Base.metadata
import backend.app.models.external_link  # noqa: F401 - required by a legacy ALTER in run_migrations
import backend.app.models.print_log  # noqa: F401 - required by a legacy ALTER in run_migrations
from backend.app.core.database import Base, run_migrations


@pytest.fixture(autouse=True)
def force_sqlite_dialect(monkeypatch):
    """Same fixture as test_billing_run_id_migration.py: run_migrations branches on
    the global dialect, not on the connection, and the engine below is SQLite."""
    from backend.app.core import db_dialect

    monkeypatch.setattr(db_dialect, "is_sqlite", lambda: True)
    monkeypatch.setattr(db_dialect, "is_postgres", lambda: False)
    from backend.app.core import database as database_module

    monkeypatch.setattr(database_module, "is_sqlite", lambda: True)


async def _print_queue_columns(conn) -> set[str]:
    return {row[1] for row in (await conn.execute(text("PRAGMA table_info(print_queue)"))).all()}


@pytest.mark.asyncio
async def test_rfid_precheck_at_is_added_to_an_existing_print_queue(tmp_path):
    """An upgrade from a schema without the column gets it; a second run is a no-op."""
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'rfid-precheck.db'}")
    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
            await conn.execute(text("ALTER TABLE print_queue DROP COLUMN rfid_precheck_at"))
            assert "rfid_precheck_at" not in await _print_queue_columns(conn)

            await run_migrations(conn)
            assert "rfid_precheck_at" in await _print_queue_columns(conn)

            await run_migrations(conn)
            assert "rfid_precheck_at" in await _print_queue_columns(conn)
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_a_fresh_install_has_the_column_from_create_all(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'rfid-precheck-fresh.db'}")
    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
            assert "rfid_precheck_at" in await _print_queue_columns(conn)
            await run_migrations(conn)
            assert "rfid_precheck_at" in await _print_queue_columns(conn)
    finally:
        await engine.dispose()
