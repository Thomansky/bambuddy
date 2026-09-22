"""Ask for the outcome of prints Bambuddy did not start (#1898 follow-up).

The ask-for-outcome flag rides from a queue item onto the archive at dispatch.
On a farm where most jobs are started at the printer's screen, in Bambu Studio
or in the Handy app there is no queue item to ride from, so every archive
``on_print_start`` created had ``confirm_requested`` false and the feature
looked broken. ``confirm_outcome_external_prints`` is the missing source.

Both archive-creating branches of ``on_print_start`` are driven here against a
real database: the no-3MF fallback (what a P1S/A1 farm actually hits) and the
normal path that archives a downloaded 3MF.
"""

from contextlib import ExitStack
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.app.main import (
    _active_prints,
    _expected_print_creators,
    _expected_print_registered_at,
    _expected_prints,
    _print_ams_mappings,
)
from backend.app.models.archive import PrintArchive
from backend.app.models.print_queue import PrintQueueItem
from backend.app.models.settings import Settings

DISPATCH = "/data/Metadata/plate_1.gcode"
SUBTASK = "Bracket_plate_1"


@pytest.fixture(autouse=True)
def _clear_print_state():
    dicts = (
        _expected_prints,
        _expected_print_registered_at,
        _expected_print_creators,
        _print_ams_mappings,
        _active_prints,
    )
    for d in dicts:
        d.clear()
    yield
    for d in dicts:
        d.clear()


class _StubArchiveService:
    """Stands in for ArchiveService on the downloaded-3MF branch.

    Writes the row the real service would write, without needing a parseable
    3MF on disk. Everything this test asserts happens *after* the row exists.
    """

    def __init__(self, db):
        self.db = db

    async def archive_print(self, **kwargs):
        archive = PrintArchive(
            printer_id=kwargs.get("printer_id"),
            filename=f"{SUBTASK}.gcode.3mf",
            file_path=f"archives/test/{SUBTASK}.gcode.3mf",
            file_size=2048,
            print_name=SUBTASK,
            status="printing",
            started_at=datetime.now(timezone.utc),
        )
        self.db.add(archive)
        await self.db.commit()
        await self.db.refresh(archive)
        return archive


async def _drive_print_start(test_engine, printer, *, download_ok: bool) -> None:
    """Run ``on_print_start`` against the test database.

    ``download_ok`` picks the branch: False leaves the 3MF unreachable and the
    no-3MF fallback archive is written inline; True takes the ArchiveService
    branch.
    """
    session_maker = async_sessionmaker(test_engine, class_=AsyncSession, expire_on_commit=False)
    state = MagicMock(
        current_project_url=f"ftp://{SUBTASK}.gcode.3mf",
        sdcard=True,
        sdcard_reported=True,
    )

    patches = [
        patch("backend.app.main.async_session", session_maker),
        patch("backend.app.core.database.async_session", session_maker),
        patch("backend.app.main.download_file_async", new=AsyncMock(return_value=download_ok)),
        patch("backend.app.main.download_file_try_paths_async", new=AsyncMock(return_value=None)),
        patch("backend.app.main.get_cached_3mf", return_value=None),
        patch("backend.app.main.cache_3mf_download"),
        patch("backend.app.main.peek_plate_index_in_3mf", return_value=None),
        patch("backend.app.main.ArchiveService", _StubArchiveService),
        # Imported inside the function, so patching it anywhere else lets the
        # directory walk open real sockets.
        patch("backend.app.services.bambu_ftp.list_files_async", new=AsyncMock(return_value=[])),
        patch("backend.app.main.ftps_handshake_blocked", return_value=False),
        patch("backend.app.main.get_ftp_retry_settings", new=AsyncMock(return_value=(False, 3, 2.0, 30))),
        patch("backend.app.main._record_energy_start", new_callable=AsyncMock),
        patch("backend.app.main._send_print_start_notification", new_callable=AsyncMock),
        patch("backend.app.main._maybe_start_layer_timelapse"),
        patch("backend.app.main._capture_timelapse_baseline_at_start", new_callable=AsyncMock),
        # Real, it would spawn a task that outlives the test by a minute.
        patch("backend.app.main._schedule_fallback_3mf_retry"),
        patch("backend.app.main._store_spoolman_print_data", new_callable=AsyncMock),
        # Imported inside on_print_start; it would try to persist the mocked
        # printer state's AMS trays and fail on the MagicMock values.
        patch("backend.app.services.usage_tracker.on_print_start", new_callable=AsyncMock),
    ]

    with ExitStack() as stack:
        for p in patches:
            stack.enter_context(p)
        notif = stack.enter_context(patch("backend.app.main.notification_service"))
        plug = stack.enter_context(patch("backend.app.main.smart_plug_manager"))
        ws = stack.enter_context(patch("backend.app.main.ws_manager"))
        relay = stack.enter_context(patch("backend.app.main.mqtt_relay"))
        pm = stack.enter_context(patch("backend.app.main.printer_manager"))

        notif.on_print_start = AsyncMock()
        plug.on_print_start = AsyncMock()
        ws.send_print_start = AsyncMock()
        ws.send_archive_created = AsyncMock()
        ws.send_archive_updated = AsyncMock()
        relay.on_print_start = AsyncMock()
        relay.on_archive_created = AsyncMock()
        pm.get_status = MagicMock(return_value=state)
        pm.get_printer = MagicMock(return_value=MagicMock(serial_number="TEST1898"))

        from backend.app.main import on_print_start

        await on_print_start(printer.id, {"filename": DISPATCH, "subtask_name": SUBTASK})


async def _set_setting(db_session, key: str, value: str) -> None:
    db_session.add(Settings(key=key, value=value))
    await db_session.commit()


async def _created_archive(db_session, printer_id: int) -> PrintArchive:
    db_session.expire_all()
    archive = await db_session.scalar(
        select(PrintArchive).where(PrintArchive.printer_id == printer_id).order_by(PrintArchive.id.desc()).limit(1)
    )
    assert archive is not None, "on_print_start created no archive"
    return archive


class TestExternalPrintGetsTheOutcomePrompt:
    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_no_3mf_fallback_asks_when_the_setting_is_on(self, test_engine, db_session, printer_factory):
        """The P1S/A1 case: the 3MF cannot be fetched, the archive is written
        inline — and that is the row the completion path reads the flag off."""
        printer = await printer_factory()
        await _set_setting(db_session, "confirm_outcome_external_prints", "true")

        await _drive_print_start(test_engine, printer, download_ok=False)

        archive = await _created_archive(db_session, printer.id)
        assert archive.extra_data.get("no_3mf_available") is True
        assert archive.confirm_requested is True
        # What the completion path gates the prompt on.
        assert archive.user_verdict is None

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_the_archive_reaches_the_rest_of_the_1898_machinery(self, test_engine, db_session, printer_factory):
        """An externally started print is now a pending confirmation like any
        other: the prompt the completion path emits is gated on exactly these
        two fields, and the plate-release default resolves the same row."""
        from backend.app.services.print_confirmation import resolve_pending_confirmation_as_good

        printer = await printer_factory()
        printer_id = printer.id
        await _set_setting(db_session, "confirm_outcome_external_prints", "true")

        await _drive_print_start(test_engine, printer, download_ok=False)

        archive = await _created_archive(db_session, printer_id)
        archive.status = "completed"
        await db_session.commit()

        assert await resolve_pending_confirmation_as_good(db_session, printer_id) == archive.id

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_downloaded_3mf_archive_asks_when_the_setting_is_on(self, test_engine, db_session, printer_factory):
        """The other creation branch: the 3MF arrived and ArchiveService wrote
        the row. The flag is set on the row afterwards rather than passed into
        archive_print, which also serves the queue dispatcher."""
        printer = await printer_factory()
        await _set_setting(db_session, "confirm_outcome_external_prints", "true")

        await _drive_print_start(test_engine, printer, download_ok=True)

        archive = await _created_archive(db_session, printer.id)
        assert archive.file_path.endswith(".3mf")
        assert archive.confirm_requested is True

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_default_leaves_external_prints_alone(self, test_engine, db_session, printer_factory):
        """Default off: nothing about today's behaviour changes for an install
        that never touches the new setting — no row for it at all."""
        printer = await printer_factory()

        await _drive_print_start(test_engine, printer, download_ok=False)

        archive = await _created_archive(db_session, printer.id)
        assert archive.confirm_requested is False

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_setting_off_does_not_ask(self, test_engine, db_session, printer_factory):
        printer = await printer_factory()
        await _set_setting(db_session, "confirm_outcome_external_prints", "false")

        await _drive_print_start(test_engine, printer, download_ok=False)

        assert (await _created_archive(db_session, printer.id)).confirm_requested is False

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_an_odd_stored_value_counts_as_off(self, test_engine, db_session, printer_factory):
        """Settings live in a VARCHAR column and every other reader treats
        anything that is not "true" as off."""
        printer = await printer_factory()
        await _set_setting(db_session, "confirm_outcome_external_prints", "None")

        await _drive_print_start(test_engine, printer, download_ok=False)

        assert (await _created_archive(db_session, printer.id)).confirm_requested is False


class TestAQueuedPrintStillDecidesForItself:
    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_a_dispatched_job_is_not_overridden_by_the_setting(
        self, test_engine, db_session, printer_factory, archive_factory
    ):
        """A queue item that deliberately has the ask-for-outcome flag off must
        stay off. A restart mid-print empties the expected-print registry, so
        the queue row — which the scheduler commits to "printing" before the
        MQTT send — is the durable record that Bambuddy started this."""
        printer = await printer_factory()
        source = await archive_factory(printer.id, status="printing", with_run=False)
        db_session.add(
            PrintQueueItem(
                printer_id=printer.id,
                archive_id=source.id,
                status="printing",
                confirm_outcome=False,
            )
        )
        await db_session.commit()
        await _set_setting(db_session, "confirm_outcome_external_prints", "true")

        await _drive_print_start(test_engine, printer, download_ok=False)

        archive = await _created_archive(db_session, printer.id)
        assert archive.confirm_requested is False

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_a_queue_items_own_yes_survives_print_start(
        self, test_engine, db_session, printer_factory, archive_factory
    ):
        """The dispatcher copies ``confirm_outcome`` onto the archive before
        the print starts; the expected-print branch of ``on_print_start`` must
        leave that alone even while the external-print setting is off."""
        from backend.app.main import register_expected_print

        printer = await printer_factory()
        archive = await archive_factory(
            printer.id,
            filename=f"{SUBTASK}.gcode.3mf",
            status="pending",
            confirm_requested=True,
            with_run=False,
        )
        db_session.add(
            PrintQueueItem(
                printer_id=printer.id,
                archive_id=archive.id,
                status="printing",
                confirm_outcome=True,
            )
        )
        await db_session.commit()
        archive_id, printer_id = archive.id, printer.id
        register_expected_print(printer_id, f"{SUBTASK}.gcode.3mf", archive_id)

        await _drive_print_start(test_engine, printer, download_ok=False)

        db_session.expire_all()
        refreshed = await db_session.get(PrintArchive, archive_id)
        assert refreshed.status == "printing"
        assert refreshed.confirm_requested is True
        # No second row for the same print.
        rows = (await db_session.scalars(select(PrintArchive).where(PrintArchive.printer_id == printer_id))).all()
        assert len(rows) == 1


class TestSettingsRoundTrip:
    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_defaults_to_off_and_round_trips(self, async_client):
        response = await async_client.get("/api/v1/settings/")
        assert response.status_code == 200
        assert response.json()["confirm_outcome_external_prints"] is False

        response = await async_client.put("/api/v1/settings/", json={"confirm_outcome_external_prints": True})
        assert response.status_code == 200
        assert response.json()["confirm_outcome_external_prints"] is True

        assert (await async_client.get("/api/v1/settings/")).json()["confirm_outcome_external_prints"] is True

        response = await async_client.put("/api/v1/settings/", json={"confirm_outcome_external_prints": False})
        assert response.status_code == 200
        assert (await async_client.get("/api/v1/settings/")).json()["confirm_outcome_external_prints"] is False

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_updating_it_leaves_the_per_job_default_alone(self, async_client):
        """Two different questions: one seeds the per-job toggle in the print
        dialog, the other covers prints that never see that dialog."""
        await async_client.put("/api/v1/settings/", json={"default_confirm_outcome": True})

        body = (await async_client.put("/api/v1/settings/", json={"confirm_outcome_external_prints": True})).json()
        assert body["default_confirm_outcome"] is True
        assert body["confirm_outcome_external_prints"] is True
