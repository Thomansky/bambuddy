"""The boolean ``webdav_enabled`` becomes the three-way ``webdav_mode`` (#3152).

The mapping is the whole test. An install that switched WebDAV on was offered a
read-only share and said yes to *that*; an upgrade that read the same yes as
"and you may also delete the library from a mapped drive" would be answering a
question nobody was asked. So ``true`` becomes ``read``, never ``readwrite``,
and anything else becomes ``off``.

Runs the real ``run_migrations`` against a throwaway SQLite database, twice, the
way ``test_archive_plate_2603.py`` does — a settings migration that is not
idempotent breaks the second startup rather than the first.
"""

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from backend.app.core.database import Base, run_migrations

# run_migrations touches many tables, so the whole model set has to be imported
# before create_all -- same list as test_archive_plate_2603.py, and for the same
# reason (imports for their Base.metadata side effect only).
from backend.app.models import (  # noqa: F401
    ams_history,
    ams_label,
    api_key,
    archive,
    auth_ephemeral,
    color_catalog,
    external_link,
    filament,
    group,
    kprofile_note,
    library,
    maintenance,
    notification,
    notification_template,
    oidc_provider,
    print_log,
    print_queue,
    printer,
    project,
    project_bom,
    slot_preset,
    smart_plug,
    smart_plug_energy_snapshot,
    sponsor_toast_state,
    spool,
    spool_assignment,
    spool_catalog,
    spool_k_profile,
    spool_usage_history,
    spoolbuddy_device,
    spoolman_k_profile,
    spoolman_slot_assignment,
    user,
    user_email_pref,
    user_otp_code,
    user_totp,
    virtual_printer,
)
from backend.app.models.settings import Settings

pytestmark = pytest.mark.asyncio


@pytest.fixture
def force_sqlite_dialect(monkeypatch):
    """Force the SQLite branch of run_migrations whatever DATABASE_URL says."""
    from backend.app.core import database as database_module, db_dialect

    monkeypatch.setattr(db_dialect, "is_sqlite", lambda: True)
    monkeypatch.setattr(db_dialect, "is_postgres", lambda: False)
    monkeypatch.setattr(database_module, "is_sqlite", lambda: True)


@pytest.fixture
async def sm():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    try:
        yield engine, async_sessionmaker(engine, expire_on_commit=False)
    finally:
        await engine.dispose()


async def _webdav_rows(session) -> dict[str, str]:
    result = await session.execute(text("SELECT key, value FROM settings WHERE key LIKE 'webdav%'"))
    return dict(result.fetchall())


@pytest.mark.parametrize(
    ("stored", "expected"),
    [
        ("true", "read"),
        ("True", "read"),
        ("false", "off"),
        # The literal the update path writes for an explicit null (#2905), and
        # a spelling no reader in the app treats as on.
        ("None", "off"),
        ("1", "off"),
    ],
)
async def test_a_stored_boolean_maps_to_a_mode(sm, force_sqlite_dialect, stored, expected):
    engine, session_maker = sm
    async with session_maker() as session:
        session.add(Settings(key="webdav_enabled", value=stored))
        await session.commit()

    async with engine.begin() as conn:
        await run_migrations(conn)

    async with session_maker() as session:
        assert await _webdav_rows(session) == {"webdav_mode": expected}


async def test_an_install_that_never_had_the_setting_gets_no_row(sm, force_sqlite_dialect):
    """No row at all, which the schema and the router both read as ``off``."""
    engine, session_maker = sm

    async with engine.begin() as conn:
        await run_migrations(conn)

    async with session_maker() as session:
        assert await _webdav_rows(session) == {}


async def test_a_second_pass_leaves_the_mode_alone(sm, force_sqlite_dialect):
    """Including a mode the user has since changed.

    The old key is dropped with the first pass, so a later startup has nothing
    left to map — and must not reset a ``readwrite`` the operator chose after
    the upgrade back to ``read``.
    """
    engine, session_maker = sm
    async with session_maker() as session:
        session.add(Settings(key="webdav_enabled", value="true"))
        await session.commit()

    async with engine.begin() as conn:
        await run_migrations(conn)

    async with session_maker() as session:
        row = (await session.execute(text("SELECT id FROM settings WHERE key = 'webdav_mode'"))).scalar_one()
        await session.execute(
            text("UPDATE settings SET value = 'readwrite' WHERE id = :id"),
            {"id": row},
        )
        await session.commit()

    async with engine.begin() as conn:
        await run_migrations(conn)

    async with session_maker() as session:
        assert await _webdav_rows(session) == {"webdav_mode": "readwrite"}


async def test_an_upgraded_library_row_is_not_left_waiting_for_bytes(sm, force_sqlite_dialect):
    """The ``ingest_pending`` column an upgrade adds must default to false.

    Dropping the column first is how a pre-#3152 database is simulated here:
    ``create_all`` always builds the current schema, so the ALTER path the
    migration takes would otherwise never run. A row that arrives as NULL or 1
    would make every file in an upgraded library look like one whose content
    has not turned up yet.
    """
    engine, session_maker = sm
    columns = "filename, file_path, file_type, file_size, is_external, print_count, variant_position"
    async with session_maker() as session:
        await session.execute(text("ALTER TABLE library_files DROP COLUMN ingest_pending"))
        await session.execute(
            text(f"INSERT INTO library_files ({columns}) VALUES ('part.3mf', 'library/files/a.3mf', '3mf', 4, 0, 0, 0)")
        )
        await session.commit()

    async with engine.begin() as conn:
        await run_migrations(conn)

    async with session_maker() as session:
        pending = (await session.execute(text("SELECT ingest_pending FROM library_files"))).scalar_one()

    assert not pending
