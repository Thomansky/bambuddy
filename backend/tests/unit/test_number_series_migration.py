"""``run_migrations`` builds the number-series schema on an upgrade, twice over.

The columns arrive on databases that predate them, so the table, its unique key
and all four number columns have to appear without create_all's help — and a
second startup must be a no-op rather than an error.
"""

import importlib
import pkgutil

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import backend.app.models as _models_package
from backend.app.core.database import Base, run_migrations, seed_number_series
from backend.app.models.number_series import NumberSeries
from backend.app.models.project import Project
from backend.app.services.number_series import DEFAULT_SERIES_KEYS

# run_migrations touches nearly every table, so the whole model package has to
# be imported before create_all — importing by walk rather than by hand so a
# model added later cannot quietly drop out of this schema.
for _module in pkgutil.iter_modules(_models_package.__path__):
    importlib.import_module(f"{_models_package.__name__}.{_module.name}")

pytestmark = pytest.mark.unit


@pytest.fixture
def force_sqlite_dialect(monkeypatch):
    """Force the SQLite branch of run_migrations regardless of the test env's
    DATABASE_URL."""
    from backend.app.core import database as database_module, db_dialect

    monkeypatch.setattr(db_dialect, "is_sqlite", lambda: True)
    monkeypatch.setattr(db_dialect, "is_postgres", lambda: False)
    monkeypatch.setattr(database_module, "is_sqlite", lambda: True)


async def _columns(conn, table: str) -> set[str]:
    return {row[1] for row in await conn.execute(text(f"PRAGMA table_info({table})"))}


@pytest.mark.asyncio
async def test_migrations_build_the_schema_and_re_run_cleanly(tmp_path, force_sqlite_dialect):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'upgrade.db'}")
    try:
        # A database that predates the feature: every table except this one.
        async with engine.begin() as conn:
            await conn.run_sync(
                Base.metadata.create_all,
                tables=[t for name, t in Base.metadata.tables.items() if name != "number_series"],
            )
            # The index goes first: SQLite refuses to drop a column one refers to.
            await conn.execute(text("DROP INDEX IF EXISTS ix_projects_number"))
            await conn.execute(text("ALTER TABLE projects DROP COLUMN number"))
            await conn.execute(text("ALTER TABLE print_queue DROP COLUMN job_number"))
            await conn.execute(text("ALTER TABLE print_archives DROP COLUMN job_number"))
            await conn.execute(text("ALTER TABLE print_log_entries DROP COLUMN job_number"))

        async with engine.begin() as conn:
            await run_migrations(conn)

        async with engine.begin() as conn:
            assert "number" in await _columns(conn, "projects")
            assert "job_number" in await _columns(conn, "print_queue")
            assert "job_number" in await _columns(conn, "print_archives")
            assert "job_number" in await _columns(conn, "print_log_entries")
            assert (await conn.execute(text("SELECT COUNT(*) FROM number_series"))).scalar() == 0

        # A further startup re-runs migrations: idempotent, not an error.
        async with engine.begin() as conn:
            await run_migrations(conn)
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_a_project_number_is_unique_but_missing_ones_are_not(tmp_path, force_sqlite_dialect):
    """The index has to tolerate the NULLs an upgrade starts with — every
    existing project is unnumbered and none of them may collide."""
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'unique.db'}")
    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
            await run_migrations(conn)

        sm = async_sessionmaker(engine, expire_on_commit=False)
        async with sm() as db:
            db.add_all([Project(name="No number"), Project(name="Also none"), Project(name="Numbered", number="A-1")])
            await db.commit()

        async with sm() as db:
            db.add(Project(name="Clash", number="A-1"))
            with pytest.raises(Exception):  # noqa: B017 - IntegrityError, whatever the driver wraps it in
                await db.commit()
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_seeding_adds_only_the_series_that_are_missing(tmp_path, monkeypatch):
    """Per key rather than "skip when the table has rows", so a later version
    that adds a third series still gets it on an install that has the first two."""
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'seed.db'}")
    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        sm = async_sessionmaker(engine, expire_on_commit=False)
        monkeypatch.setattr("backend.app.core.database.async_session", sm)

        async with sm() as db:
            db.add(NumberSeries(key=DEFAULT_SERIES_KEYS[0], next_value=500, enabled=True))
            await db.commit()

        await seed_number_series()
        await seed_number_series()

        async with sm() as db:
            rows = {r["key"]: r for r in (await db.execute(text("SELECT * FROM number_series"))).mappings()}
            assert set(rows) == set(DEFAULT_SERIES_KEYS)
            # The existing row is left exactly as the user set it.
            assert rows[DEFAULT_SERIES_KEYS[0]]["next_value"] == 500
            # And the new one arrives disabled, starting at 1.
            assert rows[DEFAULT_SERIES_KEYS[1]]["next_value"] == 1
            assert not rows[DEFAULT_SERIES_KEYS[1]]["enabled"]
    finally:
        await engine.dispose()
