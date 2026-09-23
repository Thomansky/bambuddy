"""The queue item's running number reaches the archive at dispatch.

A queue row is deleted once the order is done; the archive is what Print
History reads. If the number does not travel across at dispatch it is lost the
moment the job finishes, which is the one thing the whole feature exists to
prevent.
"""

from __future__ import annotations

import json
import zipfile
from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import backend.app.models  # noqa: F401 - populate Base.metadata
import backend.app.services.print_scheduler as scheduler_module
from backend.app.core.database import Base
from backend.app.models.archive import PrintArchive
from backend.app.models.print_queue import PrintQueueItem
from backend.app.models.printer import Printer
from backend.app.models.settings import Settings  # noqa: F401 - registers the table
from backend.app.services.print_scheduler import PrintScheduler
from backend.tests._fixtures.background_tasks import discarding_spawn_patch

pytestmark = pytest.mark.integration


def _write_3mf(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("Metadata/project_settings.config", json.dumps({}))
        zf.writestr(
            "Metadata/slice_info.config",
            '<config><plate><metadata key="index" value="1"/></plate></config>',
        )


@pytest.fixture
async def dispatch_case(tmp_path):
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    session_maker = async_sessionmaker(engine, expire_on_commit=False)

    base_dir = tmp_path / "dispatch"
    archive_rel = Path("archives") / "benchy.gcode.3mf"
    _write_3mf(base_dir / archive_rel)

    async def _build(*, item_job_number: str | None, archive_job_number: str | None = None):
        async with session_maker() as db:
            printer = Printer(
                name="X1C-1",
                serial_number="NUMBER-SERIAL",
                ip_address="127.0.0.1",
                access_code="access-code",
                model="X1C",
            )
            db.add(printer)
            await db.flush()
            archive = PrintArchive(
                printer_id=printer.id,
                filename="benchy.gcode.3mf",
                file_path=str(archive_rel),
                file_size=(base_dir / archive_rel).stat().st_size,
                status="completed",
                job_number=archive_job_number,
            )
            db.add(archive)
            await db.flush()
            item = PrintQueueItem(
                printer_id=printer.id,
                archive_id=archive.id,
                plate_id=1,
                status="pending",
                job_number=item_job_number,
            )
            db.add(item)
            await db.commit()
            return SimpleNamespace(item_id=item.id, archive_id=archive.id)

    try:
        yield SimpleNamespace(session_maker=session_maker, base_dir=base_dir, build=_build)
    finally:
        await engine.dispose()


async def _dispatch(ctx, ids) -> PrintArchive:
    scheduler = PrintScheduler()
    status = SimpleNamespace(state="IDLE", nozzle_rack=None)

    with ExitStack() as stack:
        for patcher in (
            patch.object(scheduler_module, "async_session", ctx.session_maker),
            patch.object(scheduler_module.settings, "base_dir", ctx.base_dir),
            patch("backend.app.services.print_scheduler.printer_manager.is_connected", MagicMock(return_value=True)),
            patch("backend.app.services.print_scheduler.printer_manager.get_status", MagicMock(return_value=status)),
            patch("backend.app.services.print_scheduler.printer_manager.start_print", MagicMock(return_value=True)),
            patch("backend.app.services.print_scheduler.printer_manager.set_awaiting_plate_clear", MagicMock()),
            patch("backend.app.services.print_scheduler.delete_file_async", AsyncMock(return_value=True)),
            patch("backend.app.services.print_scheduler.upload_file_async", AsyncMock(return_value=True)),
            patch(
                "backend.app.services.print_scheduler.get_ftp_retry_settings",
                AsyncMock(return_value=(False, 3, 2.0, 30.0)),
            ),
            patch("backend.app.services.print_scheduler.cache_3mf_download", MagicMock()),
            discarding_spawn_patch(),
            patch("backend.app.services.notification_service.notification_service.on_queue_job_started", AsyncMock()),
            patch("backend.app.services.notification_service.notification_service.on_queue_job_failed", AsyncMock()),
            patch("backend.app.services.mqtt_relay.mqtt_relay.on_queue_job_started", AsyncMock()),
            patch.object(scheduler, "_propagate_owner_to_printer_manager", AsyncMock()),
            patch.object(scheduler, "_power_off_if_needed", AsyncMock()),
            patch.object(scheduler, "_preheat_and_soak", AsyncMock()),
        ):
            stack.enter_context(patcher)
        await scheduler._dispatch_one(ids.item_id)

    async with ctx.session_maker() as db:
        return await db.get(PrintArchive, ids.archive_id)


class TestJobNumberReachesTheArchive:
    async def test_the_queue_items_number_is_copied_onto_the_archive(self, dispatch_case):
        ids = await dispatch_case.build(item_job_number="J0042")

        archive = await _dispatch(dispatch_case, ids)

        assert archive.job_number == "J0042"

    async def test_an_archive_that_already_has_one_is_not_relabelled(self, dispatch_case):
        """A reprint of a numbered archive runs under a new queue row; the
        archive keeps the number it was first printed under."""
        ids = await dispatch_case.build(item_job_number="J0099", archive_job_number="J0001")

        archive = await _dispatch(dispatch_case, ids)

        assert archive.job_number == "J0001"

    async def test_an_unnumbered_job_leaves_the_archive_unnumbered(self, dispatch_case):
        ids = await dispatch_case.build(item_job_number=None)

        archive = await _dispatch(dispatch_case, ids)

        assert archive.job_number is None
