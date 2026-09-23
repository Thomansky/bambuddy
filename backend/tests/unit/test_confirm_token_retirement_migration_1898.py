"""Migration coverage for the one-tap token retirement columns (#1898).

``confirm_token_used_at`` is what turns a spent link into "already answered"
instead of a 404, and ``user_verdict_source`` is what the hint next to the
verdict badge reads — both have to reach an install that upgraded rather than
one created fresh from the models.
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
    """run_migrations branches on the global dialect, not on the connection, so
    a dev config pointing at Postgres would run Postgres-only syntax against the
    SQLite engine below. Same fixture as test_billing_run_id_migration.py."""
    from backend.app.core import db_dialect

    monkeypatch.setattr(db_dialect, "is_sqlite", lambda: True)
    monkeypatch.setattr(db_dialect, "is_postgres", lambda: False)
    # database.py imported is_sqlite at module load time — patch there too.
    from backend.app.core import database as database_module

    monkeypatch.setattr(database_module, "is_sqlite", lambda: True)


async def _archive_columns(conn) -> set[str]:
    return {row[1] for row in (await conn.execute(text("PRAGMA table_info(print_archives)"))).all()}


@pytest.mark.asyncio
async def test_retirement_columns_are_added_and_migration_is_idempotent(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'confirm-retirement.db'}")
    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
            # Simulate the pre-#1898-follow-up schema: the columns only exist
            # here because create_all built the table from today's models.
            await conn.execute(text("ALTER TABLE print_archives DROP COLUMN confirm_token_used_at"))
            await conn.execute(text("ALTER TABLE print_archives DROP COLUMN user_verdict_source"))
            assert "confirm_token_used_at" not in await _archive_columns(conn)

            await run_migrations(conn)
            columns = await _archive_columns(conn)
            assert "confirm_token_used_at" in columns
            assert "user_verdict_source" in columns

            # Re-running the migrations on an already-migrated install is a
            # no-op, not an error: _safe_execute swallows the duplicate ALTER.
            await run_migrations(conn)
            assert await _archive_columns(conn) >= {"confirm_token_used_at", "user_verdict_source"}

            # ...with the SQLite-flavoured types the ALTERs declare, so a
            # stamp round-trips as a datetime rather than as opaque text.
            types = {
                row[1]: row[2].upper() for row in (await conn.execute(text("PRAGMA table_info(print_archives)"))).all()
            }
            assert types["confirm_token_used_at"] == "DATETIME"
            assert types["user_verdict_source"].startswith("VARCHAR")
    finally:
        await engine.dispose()
