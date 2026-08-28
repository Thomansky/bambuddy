"""Migration tests for the spool link group (#2936).

A database that predates the feature must gain the group table and the
spool's group column on upgrade, and re-running must be a no-op.
"""

from __future__ import annotations

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from backend.app.core.database import run_migrations


@pytest.fixture(autouse=True)
def force_sqlite_dialect(monkeypatch):
    from backend.app.core import db_dialect

    monkeypatch.setattr(db_dialect, "is_sqlite", lambda: True)
    monkeypatch.setattr(db_dialect, "is_postgres", lambda: False)
    from backend.app.core import database as database_module

    monkeypatch.setattr(database_module, "is_sqlite", lambda: True)


def _register_all_models():
    import backend.app.models  # noqa: F401
    from backend.app.models import (  # noqa: F401
        external_link,
        location,
        print_log,
        print_queue,
        project_bom,
        slot_preset,
        spoolman_k_profile,
        spoolman_slot_assignment,
        virtual_printer,
    )


@pytest.fixture
async def engine_predating_linked_spools():
    """create_all builds the current schema; dropping the additions
    reproduces a database from before #2936."""
    from backend.app.core.database import Base

    _register_all_models()
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        # The index must go before the column can (SQLite refuses to drop a
        # column an index still covers).
        await conn.execute(text("DROP INDEX ix_spool_filament_group_id"))
        await conn.execute(text("ALTER TABLE spool DROP COLUMN filament_group_id"))
        await conn.execute(text("DROP TABLE spool_link_groups"))
        await conn.execute(
            text(
                """
                INSERT INTO spool (material, label_weight, core_weight, weight_used, weight_used_baseline, weight_locked)
                VALUES ('PLA', 1000, 250, 0, 0, 0)
                """
            )
        )
    yield engine
    await engine.dispose()


async def test_migration_adds_group_table_and_column(engine_predating_linked_spools):
    async with engine_predating_linked_spools.begin() as conn:
        await run_migrations(conn)

    async with engine_predating_linked_spools.begin() as conn:
        await conn.execute(text("INSERT INTO spool_link_groups DEFAULT VALUES"))
        await conn.execute(text("UPDATE spool SET filament_group_id = (SELECT MAX(id) FROM spool_link_groups)"))

    async with engine_predating_linked_spools.connect() as conn:
        rows = (await conn.execute(text("SELECT filament_group_id FROM spool"))).scalars().all()
    assert len(rows) == 1
    assert rows[0] is not None


async def test_migration_is_idempotent(engine_predating_linked_spools):
    async with engine_predating_linked_spools.begin() as conn:
        await run_migrations(conn)
    async with engine_predating_linked_spools.begin() as conn:
        await conn.execute(text("INSERT INTO spool_link_groups DEFAULT VALUES"))
    async with engine_predating_linked_spools.begin() as conn:
        await run_migrations(conn)

    async with engine_predating_linked_spools.connect() as conn:
        groups = (await conn.execute(text("SELECT COUNT(*) FROM spool_link_groups"))).scalar_one()
    # The re-run neither recreated the table nor destroyed its contents.
    assert groups == 1
