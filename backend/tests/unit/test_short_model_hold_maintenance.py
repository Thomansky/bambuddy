"""Releasing a low-filament hold respects the maintenance reservation (#3137 x #3127).

An "any model" job held for low filament on printer A is moved to another idle
printer of its model once one has the filament (#3137). The release asked the
matcher without the maintenance reservation (#3127) the model-based branch
passes, so it could move the job onto printer B, which has a calibration run
pending, while printer C sat free. The fixed-printer branch then held the job
on B until the run was done.

It also kept the item's ``rfid_precheck_at`` stamp, which was set for A: the
pre-dispatch RFID read is per printer, and the printer the job moves to never
got its own.

Now the release passes over a reserved printer for a free one, and a move
clears the stamp.
"""

from contextlib import ExitStack
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, call, patch

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.orm import selectinload

import backend.app.models  # noqa: F401 - populate Base.metadata
from backend.app.core.database import Base
from backend.app.core.printer_scope import ALL_PRINTERS
from backend.app.models.library import LibraryFile
from backend.app.models.maintenance import MaintenanceRun, MaintenanceType, PrinterMaintenance
from backend.app.models.print_queue import PrintQueueItem, PrintQueueVariant
from backend.app.models.printer import Printer
from backend.app.models.settings import Settings
from backend.app.services import maintenance_actions
from backend.app.services.print_scheduler import PrintScheduler

pytestmark = pytest.mark.unit

# The stamp printer 1 left on the item when the job was assigned there.
_STAMPED = datetime(2026, 10, 1, 12, 0, 0)


@pytest.fixture
async def ctx(monkeypatch):
    """Three H2S printers in id order: 1 holds the job, 2 comes first after it."""
    monkeypatch.setenv("TZ", "UTC")
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    session_maker = async_sessionmaker(engine, expire_on_commit=False)

    async with session_maker() as db:
        db.add_all(
            [
                Printer(
                    id=pid,
                    name=f"H2S-0{pid}",
                    serial_number=f"H2S000{pid}",
                    ip_address=f"10.0.0.{pid}",
                    access_code="x",
                    model="H2S",
                    is_active=True,
                )
                for pid in (1, 2, 3)
            ]
        )
        db.add(
            MaintenanceType(
                id=10,
                name="Printer Calibration",
                description="",
                default_interval_hours=100.0,
                interval_type="hours",
                is_system=True,
                action="calibration",
            )
        )
        await db.commit()

    try:
        yield SimpleNamespace(session_maker=session_maker)
    finally:
        await engine.dispose()


async def _add_held_item(ctx, **fields):
    """An "any H2S" job the deficit gate held on printer 1, RFID-stamped there."""
    async with ctx.session_maker() as db:
        lib = LibraryFile(
            filename="job.gcode.3mf",
            file_path="/library/job.gcode.3mf",
            file_size=10,
            file_type="gcode.3mf",
            file_metadata={"sliced_for_model": "H2S"},
        )
        db.add(lib)
        await db.flush()
        values = {
            "status": "pending",
            "position": 1,
            "target_model": "H2S",
            "library_file_id": lib.id,
            "printer_id": 1,
            "manual_start": True,
            "filament_short": True,
            "rfid_precheck_at": _STAMPED,
        }
        values.update(fields)
        item = PrintQueueItem(**values)
        db.add(item)
        await db.commit()
        return item.id


async def _add_pending_run(ctx, printer_id):
    """A calibration run on *printer_id* that waits for the bed to cool."""
    async with ctx.session_maker() as db:
        maint = PrinterMaintenance(
            printer_id=printer_id,
            maintenance_type_id=10,
            enabled=True,
            action_options={"bed_temp_below": 30},
        )
        db.add(maint)
        await db.flush()
        db.add(
            MaintenanceRun(
                printer_maintenance_id=maint.id,
                printer_id=printer_id,
                status="pending",
                source="schedule",
                options=maintenance_actions.normalize_calibration_options(None),
            )
        )
        await db.commit()


async def _item(ctx, item_id):
    async with ctx.session_maker() as db:
        return (await db.execute(select(PrintQueueItem).where(PrintQueueItem.id == item_id))).scalar_one()


def _short_on(short: set[int]) -> AsyncMock:
    """``_filament_short_on`` stand-in: the printers in ``short`` are too light."""

    async def check(db, item, printer_id, *, require_known=False):
        return printer_id in short

    return AsyncMock(side_effect=check)


class TestRelease:
    """``_release_short_model_hold`` called on its own."""

    async def _release(self, ctx, item_id, *, short=frozenset({1}), maintenance_hold=None):
        scheduler = PrintScheduler()
        with (
            patch("backend.app.services.print_scheduler.printer_manager.is_connected", MagicMock(return_value=True)),
            patch(
                "backend.app.services.notification_service.notification_service.on_queue_job_assigned",
                AsyncMock(),
            ),
            patch.object(scheduler, "_is_printer_idle", MagicMock(return_value=True)),
            patch.object(scheduler, "_filament_short_on", _short_on(set(short))),
        ):
            async with ctx.session_maker() as db:
                item = (
                    await db.execute(
                        select(PrintQueueItem)
                        .where(PrintQueueItem.id == item_id)
                        .options(
                            selectinload(PrintQueueItem.archive),
                            selectinload(PrintQueueItem.library_file),
                            selectinload(PrintQueueItem.variants).selectinload(PrintQueueVariant.library_file),
                        )
                    )
                ).scalar_one()
                kwargs = {"maintenance_hold": maintenance_hold} if maintenance_hold is not None else {}
                return await scheduler._release_short_model_hold(db, item, set(), True, ALL_PRINTERS, **kwargs)

    @pytest.mark.asyncio
    async def test_without_the_hold_the_reserved_printer_comes_first(self, ctx):
        """The case the hold is for: printer 2 is the first idle printer with
        the filament, so it is the one the job lands on when nobody asks."""
        item_id = await _add_held_item(ctx)

        assert await self._release(ctx, item_id) is True
        assert (await _item(ctx, item_id)).printer_id == 2

    @pytest.mark.asyncio
    async def test_a_reserved_printer_is_passed_over_for_a_free_one(self, ctx):
        item_id = await _add_held_item(ctx)
        asked: list[int] = []

        def maintenance_hold(pid):
            asked.append(pid)
            return "Maintenance run pending: Printer Calibration" if pid == 2 else None

        assert await self._release(ctx, item_id, maintenance_hold=maintenance_hold) is True

        item = await _item(ctx, item_id)
        assert item.printer_id == 3
        assert item.manual_start is False
        assert item.filament_short is False
        assert 2 in asked
        # The printer it is held on is left out before the hold is asked.
        assert 1 not in asked

    @pytest.mark.asyncio
    async def test_stays_held_while_every_free_printer_is_reserved(self, ctx):
        item_id = await _add_held_item(ctx)

        moved = await self._release(ctx, item_id, maintenance_hold=lambda pid: "Maintenance run pending: x")

        assert moved is False
        item = await _item(ctx, item_id)
        assert item.printer_id == 1
        assert item.manual_start is True
        assert item.filament_short is True
        # Not moved, so the read printer 1 had stays good.
        assert item.rfid_precheck_at == _STAMPED

    @pytest.mark.asyncio
    async def test_a_move_clears_the_rfid_precheck_stamp(self, ctx):
        """The read was printer 1's; the printer the job moves to gets its own."""
        item_id = await _add_held_item(ctx)

        assert await self._release(ctx, item_id) is True

        item = await _item(ctx, item_id)
        assert item.printer_id == 2
        assert item.rfid_precheck_at is None


class TestQueuePass:
    """Through ``check_queue``: real rows, the printer manager and the upload launcher mocked."""

    @staticmethod
    async def _pass(ctx, scheduler, *, slots_to_reread=None):
        state = MagicMock()
        state.state = "IDLE"
        state.connected = True
        # Warm beds: printer 2's run waits for its bed to cool, so it stays
        # pending and reserves the printer rather than going out this pass.
        state.temperatures = {"bed": 45.0}
        state.internal_gcode_dir = "O1S"
        launched = MagicMock()
        patches = [
            patch("backend.app.services.print_scheduler.async_session", ctx.session_maker),
            patch("backend.app.core.database.async_session", ctx.session_maker),
            patch("backend.app.services.print_scheduler.printer_manager.is_connected", MagicMock(return_value=True)),
            patch("backend.app.services.print_scheduler.printer_manager.get_status", MagicMock(return_value=state)),
            patch(
                "backend.app.services.print_scheduler.printer_manager.is_awaiting_plate_clear",
                MagicMock(return_value=False),
            ),
            patch(
                "backend.app.services.print_scheduler.printer_manager.start_calibration", MagicMock(return_value=True)
            ),
            patch(
                "backend.app.services.print_scheduler.ha_sensor_manager.blocked_printers", AsyncMock(return_value={})
            ),
            patch(
                "backend.app.services.notification_service.notification_service.on_queue_job_waiting",
                AsyncMock(),
            ),
            patch(
                "backend.app.services.notification_service.notification_service.on_queue_job_assigned",
                AsyncMock(),
            ),
            patch.object(scheduler, "_filament_short_on", _short_on({1})),
            patch.object(scheduler, "_is_printer_idle", MagicMock(return_value=True)),
            patch.object(scheduler, "_check_auto_drying", AsyncMock()),
            patch.object(scheduler, "_ensure_ams_mapping", AsyncMock(return_value=None)),
            patch.object(scheduler, "_block_on_unmatched_filament", AsyncMock(return_value=False)),
            patch.object(scheduler, "_block_on_filament_deficit", AsyncMock(return_value=False)),
            patch.object(scheduler, "_get_smart_plugs", AsyncMock(return_value=[])),
            patch.object(scheduler, "_launch_uploads", launched),
        ]
        if slots_to_reread is not None:
            patches.append(patch.object(scheduler, "_slots_to_reread", slots_to_reread))
        with ExitStack() as stack:
            for p in patches:
                stack.enter_context(p)
            await scheduler.check_queue()
        return launched

    @pytest.mark.asyncio
    async def test_the_held_job_goes_to_the_free_printer_not_the_reserved_one(self, ctx):
        """Printer 2 has a calibration run waiting for its bed to cool."""
        await _add_pending_run(ctx, printer_id=2)
        item_id = await _add_held_item(ctx)

        launched = await self._pass(ctx, PrintScheduler())

        launched.assert_called_once()
        assert launched.call_args[0][0] == [item_id]
        item = await _item(ctx, item_id)
        assert item.printer_id == 3
        assert item.manual_start is False
        assert item.filament_short is False

    @pytest.mark.asyncio
    async def test_the_new_printer_gets_its_own_pre_dispatch_read(self, ctx):
        """With the read enabled, the dispatch branch asks the printer the job
        moved to for its unidentified slots. A stamp carried over from
        printer 1 would have skipped the question."""
        async with ctx.session_maker() as db:
            db.add(Settings(key="queue_rfid_reread_before_start", value="true"))
            await db.commit()
        item_id = await _add_held_item(ctx)
        # Nothing to read on printer 2, so the dispatch carries on.
        slots_to_reread = MagicMock(return_value=None)

        launched = await self._pass(ctx, PrintScheduler(), slots_to_reread=slots_to_reread)

        assert call(2, item_id) in slots_to_reread.call_args_list
        launched.assert_called_once()
        assert (await _item(ctx, item_id)).printer_id == 2
