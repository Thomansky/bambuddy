"""The print queue yields to maintenance (#3127).

Two holds, both on the automatic dispatch only:

- **Maintenance first.** A printer with a run pending or running takes no job
  from the queue until the run has closed. The user's Sunday: the calibration
  finishes, the printer is idle, and the queue used to grab it before the
  vision encoder run -- which then waited for hours.
- **Keep clear of the slot.** A scheduled run is only a lower bound when a
  Saturday-afternoon job can run straight through Sunday noon. With
  *reserve_before_schedule* on, the queue starts only jobs expected to be done
  15 minutes before the slot, and holds a job of unknown length from two hours
  before it.

Neither applies to an item a person started by hand -- ▶ on a staged item or
"Print now" in the print dialog -- which is what ``user_started`` records.

Driven through ``check_queue`` like the #3074 tests: real rows, the printer
manager and the upload launcher mocked.
"""

from contextlib import ExitStack
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import backend.app.models  # noqa: F401 - populate Base.metadata
from backend.app.core.database import Base
from backend.app.models.library import LibraryFile
from backend.app.models.maintenance import MaintenanceRun, MaintenanceType, PrinterMaintenance
from backend.app.models.print_queue import PrintQueueItem
from backend.app.models.printer import Printer
from backend.app.services import maintenance_actions
from backend.app.services.print_scheduler import PrintScheduler

pytestmark = pytest.mark.unit

WEEKDAYS = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")


def _utcnow_naive() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


@pytest.fixture
async def ctx(monkeypatch):
    monkeypatch.setenv("TZ", "UTC")
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    session_maker = async_sessionmaker(engine, expire_on_commit=False)

    async with session_maker() as db:
        db.add(
            Printer(
                id=1,
                name="H2S-01",
                serial_number="H2S0001",
                ip_address="10.0.0.1",
                access_code="x",
                model="H2S",
                is_active=True,
            )
        )
        calibration = MaintenanceType(
            id=10,
            name="Printer Calibration",
            description="",
            default_interval_hours=100.0,
            interval_type="hours",
            is_system=True,
            action="calibration",
        )
        db.add(calibration)
        await db.commit()

    try:
        yield SimpleNamespace(session_maker=session_maker)
    finally:
        await engine.dispose()


async def _add_printer(ctx, printer_id, name):
    async with ctx.session_maker() as db:
        db.add(
            Printer(
                id=printer_id,
                name=name,
                serial_number=f"H2S000{printer_id}",
                ip_address=f"10.0.0.{printer_id}",
                access_code="x",
                model="H2S",
                is_active=True,
            )
        )
        await db.commit()


async def _add_item(
    ctx,
    *,
    printer_id=1,
    target_model=None,
    position=1,
    print_time_seconds=3600,
    user_started=False,
):
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
        item = PrintQueueItem(
            status="pending",
            position=position,
            printer_id=printer_id,
            target_model=target_model,
            library_file_id=lib.id,
            print_time_seconds=print_time_seconds,
            user_started=user_started,
        )
        db.add(item)
        await db.commit()
        return item.id


async def _add_maintenance(ctx, *, printer_id=1, **kwargs):
    """The printer's calibration item, plus a run when ``run`` says so."""
    run_kwargs = kwargs.pop("run", None)
    defaults = {"printer_id": printer_id, "maintenance_type_id": 10, "enabled": True}
    defaults.update(kwargs)
    async with ctx.session_maker() as db:
        item = PrinterMaintenance(**defaults)
        db.add(item)
        await db.flush()
        run_id = None
        if run_kwargs is not None:
            run_values = {
                "status": "pending",
                "source": "schedule",
                "options": maintenance_actions.normalize_calibration_options(None),
                **run_kwargs,
            }
            run = MaintenanceRun(printer_maintenance_id=item.id, printer_id=printer_id, **run_values)
            db.add(run)
            await db.flush()
            run_id = run.id
        await db.commit()
        return SimpleNamespace(item_id=item.id, run_id=run_id)


async def _item(ctx, item_id):
    async with ctx.session_maker() as db:
        return (await db.execute(select(PrintQueueItem).where(PrintQueueItem.id == item_id))).scalar_one()


async def _run(ctx, run_id):
    async with ctx.session_maker() as db:
        return (await db.execute(select(MaintenanceRun).where(MaintenanceRun.id == run_id))).scalar_one()


async def _set_run_status(ctx, run_id, status):
    async with ctx.session_maker() as db:
        run = (await db.execute(select(MaintenanceRun).where(MaintenanceRun.id == run_id))).scalar_one()
        run.status = status
        run.completed_at = _utcnow_naive()
        await db.commit()


def _status(bed=20.0):
    state = MagicMock()
    state.state = "IDLE"
    state.connected = True
    state.temperatures = {"bed": bed}
    state.internal_gcode_dir = "O1S"
    return state


async def _pass(ctx, scheduler, *, idle=True, connected=True, bed=20.0, launched=None, waiting=None):
    """One check_queue pass with every printer connected and (by default) idle."""
    launched = launched or MagicMock()
    waiting = waiting or AsyncMock()
    patches = [
        patch("backend.app.services.print_scheduler.async_session", ctx.session_maker),
        patch("backend.app.core.database.async_session", ctx.session_maker),
        patch("backend.app.services.print_scheduler.printer_manager.is_connected", MagicMock(return_value=connected)),
        patch(
            "backend.app.services.print_scheduler.printer_manager.get_status",
            MagicMock(return_value=_status(bed) if connected else None),
        ),
        patch(
            "backend.app.services.print_scheduler.printer_manager.is_awaiting_plate_clear",
            MagicMock(return_value=False),
        ),
        patch("backend.app.services.print_scheduler.printer_manager.start_calibration", MagicMock(return_value=True)),
        patch("backend.app.services.print_scheduler.ha_sensor_manager.blocked_printers", AsyncMock(return_value={})),
        patch("backend.app.services.notification_service.notification_service.on_queue_job_waiting", waiting),
        patch("backend.app.services.notification_service.notification_service.on_queue_job_assigned", AsyncMock()),
        patch.object(scheduler, "_is_printer_idle", MagicMock(return_value=idle)),
        patch.object(scheduler, "_check_auto_drying", AsyncMock()),
        patch.object(scheduler, "_ensure_ams_mapping", AsyncMock(return_value=None)),
        patch.object(scheduler, "_block_on_filament_deficit", AsyncMock(return_value=False)),
        patch.object(scheduler, "_get_smart_plugs", AsyncMock(return_value=[])),
        patch.object(scheduler, "_launch_uploads", launched),
    ]
    with ExitStack() as stack:
        for p in patches:
            stack.enter_context(p)
        await scheduler.check_queue()
    return launched


def _launched_ids(launched) -> list[int]:
    return launched.call_args[0][0] if launched.call_args else []


class TestMaintenanceFirst:
    @pytest.mark.asyncio
    async def test_a_run_waiting_for_the_bed_keeps_the_queue_off_the_idle_printer(self, ctx):
        """The Sunday case: the calibration is done, the bed is at 45 °C, the
        vision encoder run waits for it to cool -- and the queue, which used
        to grab the idle printer here, waits too and says why."""
        await _add_maintenance(ctx, action_options={"bed_temp_below": 30}, run={})
        item_id = await _add_item(ctx)

        launched = await _pass(ctx, PrintScheduler(), bed=45.0)

        launched.assert_not_called()
        assert (await _item(ctx, item_id)).waiting_reason == (
            "Maintenance run pending: Printer Calibration (bed still warm, 45 °C)"
        )

    @pytest.mark.asyncio
    async def test_a_running_run_holds_the_item_even_while_the_printer_reports_idle(self, ctx):
        """Long after the dispatch window: the run is on the printer as far as
        Bambuddy knows, whatever the state feed says this second."""
        started = _utcnow_naive() - timedelta(minutes=20)
        await _add_maintenance(ctx, run={"status": "running", "started_at": started})
        item_id = await _add_item(ctx)

        launched = await _pass(ctx, PrintScheduler())

        launched.assert_not_called()
        assert (await _item(ctx, item_id)).waiting_reason == "Maintenance run pending: Printer Calibration (running)"

    @pytest.mark.asyncio
    async def test_a_run_dispatched_this_pass_reads_as_busy(self, ctx):
        """The run goes out first on the pass that finds the printer free; the
        item behind it sees the printer taken, as it does for any dispatch."""
        maint = await _add_maintenance(ctx, run={})
        item_id = await _add_item(ctx)

        launched = await _pass(ctx, PrintScheduler())

        launched.assert_not_called()
        assert (await _run(ctx, maint.run_id)).status == "running"
        assert (await _item(ctx, item_id)).waiting_reason == "Busy: H2S-01"

    @pytest.mark.asyncio
    async def test_the_item_goes_out_on_the_pass_after_the_run_closed(self, ctx):
        maint = await _add_maintenance(ctx, action_options={"bed_temp_below": 30}, run={})
        item_id = await _add_item(ctx)
        scheduler = PrintScheduler()
        await _pass(ctx, scheduler, bed=45.0)
        assert (await _item(ctx, item_id)).waiting_reason.startswith("Maintenance run pending")

        await _set_run_status(ctx, maint.run_id, "completed")
        launched = await _pass(ctx, scheduler, bed=45.0)

        assert _launched_ids(launched) == [item_id]
        assert (await _item(ctx, item_id)).waiting_reason is None

    @pytest.mark.asyncio
    async def test_every_item_behind_the_hold_on_that_printer_reads_the_same(self, ctx):
        await _add_maintenance(ctx, action_options={"bed_temp_below": 30}, run={})
        first = await _add_item(ctx, position=1)
        second = await _add_item(ctx, position=2)

        launched = await _pass(ctx, PrintScheduler(), bed=45.0)

        launched.assert_not_called()
        hold = "Maintenance run pending: Printer Calibration (bed still warm, 45 °C)"
        assert (await _item(ctx, first)).waiting_reason == hold
        assert (await _item(ctx, second)).waiting_reason == hold

    @pytest.mark.asyncio
    async def test_an_item_a_person_started_is_not_held(self, ctx):
        """▶ on a staged item and "Print now" mean now: the run waits for the
        print instead, as it does for any print already on the printer."""
        await _add_maintenance(ctx, action_options={"bed_temp_below": 30}, run={})
        item_id = await _add_item(ctx, user_started=True)

        launched = await _pass(ctx, PrintScheduler(), bed=45.0)

        assert _launched_ids(launched) == [item_id]
        assert (await _item(ctx, item_id)).waiting_reason is None

    @pytest.mark.asyncio
    async def test_a_model_based_item_takes_a_free_sibling_over_the_reserved_printer(self, ctx):
        await _add_printer(ctx, 2, "H2S-02")
        await _add_maintenance(ctx, printer_id=1, action_options={"bed_temp_below": 30}, run={})
        item_id = await _add_item(ctx, printer_id=None, target_model="H2S")

        launched = await _pass(ctx, PrintScheduler(), bed=45.0)

        assert _launched_ids(launched) == [item_id]
        assert (await _item(ctx, item_id)).printer_id == 2

    @pytest.mark.asyncio
    async def test_a_model_based_item_waits_with_the_hold_when_no_printer_is_free_of_it(self, ctx):
        await _add_maintenance(ctx, action_options={"bed_temp_below": 30}, run={})
        item_id = await _add_item(ctx, printer_id=None, target_model="H2S")

        launched = await _pass(ctx, PrintScheduler(), bed=45.0)

        launched.assert_not_called()
        assert (await _item(ctx, item_id)).waiting_reason == (
            "Maintenance run pending: Printer Calibration (bed still warm, 45 °C)"
        )

    @pytest.mark.asyncio
    async def test_a_reserved_printer_still_reads_as_busy_while_it_prints(self, ctx):
        """The stronger fact first: a printer that is printing is busy, and
        the queue does not pretend to know more than that about it."""
        await _add_maintenance(ctx, run={})
        item_id = await _add_item(ctx)

        launched = await _pass(ctx, PrintScheduler(), idle=False)

        launched.assert_not_called()
        assert (await _item(ctx, item_id)).waiting_reason == "Busy: H2S-01"

    @pytest.mark.asyncio
    async def test_the_hold_is_not_worth_a_job_waiting_notification(self, ctx):
        """It resolves itself, like a busy printer: the run closes."""
        await _add_maintenance(ctx, action_options={"bed_temp_below": 30}, run={})
        await _add_item(ctx)
        waiting = AsyncMock()

        await _pass(ctx, PrintScheduler(), bed=45.0, waiting=waiting)

        waiting.assert_not_called()


class TestKeepClearOfTheSlot:
    """A schedule item with reserve_before_schedule, its slot four hours out."""

    async def _schedule(self, ctx, *, hours_ahead=4.0, reserve=True, printer_id=1):
        next_at = (_utcnow_naive() + timedelta(hours=hours_ahead)).replace(microsecond=0)
        await _add_maintenance(
            ctx,
            printer_id=printer_id,
            trigger_mode="schedule",
            schedule_days=[next_at.weekday()],
            schedule_time=f"{next_at:%H:%M}",
            schedule_next_at=next_at,
            reserve_before_schedule=reserve,
        )
        return next_at

    @staticmethod
    def _hold(next_at, estimate):
        when = f"{WEEKDAYS[next_at.weekday()]} {next_at:%H:%M}"
        return f"Scheduled maintenance at {when} — this job would run into it ({estimate})"

    @pytest.mark.asyncio
    async def test_a_job_that_will_be_done_before_the_slot_starts(self, ctx):
        await self._schedule(ctx)
        item_id = await _add_item(ctx, print_time_seconds=3 * 3600)

        launched = await _pass(ctx, PrintScheduler())

        assert _launched_ids(launched) == [item_id]

    @pytest.mark.asyncio
    async def test_a_job_that_would_run_into_the_slot_is_held_with_the_reason(self, ctx):
        next_at = await self._schedule(ctx)
        item_id = await _add_item(ctx, print_time_seconds=3 * 3600 + 50 * 60)

        launched = await _pass(ctx, PrintScheduler())

        launched.assert_not_called()
        assert (await _item(ctx, item_id)).waiting_reason == self._hold(next_at, "estimated 3h 50m")

    @pytest.mark.asyncio
    async def test_a_job_of_unknown_length_is_held_inside_two_hours_and_started_outside(self, ctx):
        next_at = await self._schedule(ctx, hours_ahead=1.5)
        item_id = await _add_item(ctx, print_time_seconds=None)
        launched = await _pass(ctx, PrintScheduler())
        launched.assert_not_called()
        assert (await _item(ctx, item_id)).waiting_reason == self._hold(next_at, "duration unknown")

        # Move the slot out to three hours: nothing known against it any more
        async with ctx.session_maker() as db:
            item = (await db.execute(select(PrinterMaintenance))).scalar_one()
            item.schedule_next_at = _utcnow_naive() + timedelta(hours=3)
            await db.commit()
        launched = await _pass(ctx, PrintScheduler())
        assert _launched_ids(launched) == [item_id]
        assert (await _item(ctx, item_id)).waiting_reason is None

    @pytest.mark.asyncio
    async def test_with_the_flag_off_nothing_is_held(self, ctx):
        await self._schedule(ctx, reserve=False)
        item_id = await _add_item(ctx, print_time_seconds=3 * 3600 + 50 * 60)

        launched = await _pass(ctx, PrintScheduler())

        assert _launched_ids(launched) == [item_id]

    @pytest.mark.asyncio
    async def test_the_hold_lifts_once_the_slot_moves_to_next_week(self, ctx):
        """What happens after the run: schedule_next_at advances and the
        long job flows again."""
        await self._schedule(ctx)
        item_id = await _add_item(ctx, print_time_seconds=3 * 3600 + 50 * 60)
        scheduler = PrintScheduler()
        await _pass(ctx, scheduler)
        assert (await _item(ctx, item_id)).waiting_reason.startswith("Scheduled maintenance at")

        async with ctx.session_maker() as db:
            item = (await db.execute(select(PrinterMaintenance))).scalar_one()
            item.schedule_next_at = item.schedule_next_at + timedelta(days=7)
            await db.commit()
        launched = await _pass(ctx, scheduler)

        assert _launched_ids(launched) == [item_id]
        assert (await _item(ctx, item_id)).waiting_reason is None

    @pytest.mark.asyncio
    async def test_a_shorter_job_behind_a_held_one_keeps_flowing(self, ctx):
        """Only the job that is too long is held, like an item waiting on its
        own scheduled time; the printer stays productive until the slot."""
        await self._schedule(ctx)
        long_id = await _add_item(ctx, position=1, print_time_seconds=3 * 3600 + 50 * 60)
        short_id = await _add_item(ctx, position=2, print_time_seconds=30 * 60)

        launched = await _pass(ctx, PrintScheduler())

        assert _launched_ids(launched) == [short_id]
        assert (await _item(ctx, long_id)).waiting_reason.startswith("Scheduled maintenance at")

    @pytest.mark.asyncio
    async def test_a_model_based_job_waits_with_the_same_reason_when_no_printer_fits(self, ctx):
        next_at = await self._schedule(ctx)
        item_id = await _add_item(ctx, printer_id=None, target_model="H2S", print_time_seconds=5 * 3600)

        launched = await _pass(ctx, PrintScheduler())

        launched.assert_not_called()
        assert (await _item(ctx, item_id)).waiting_reason == self._hold(next_at, "estimated 5h 0m")

    @pytest.mark.asyncio
    async def test_a_model_based_job_takes_the_printer_without_a_slot_ahead(self, ctx):
        await _add_printer(ctx, 2, "H2S-02")
        await self._schedule(ctx, printer_id=1)
        item_id = await _add_item(ctx, printer_id=None, target_model="H2S", print_time_seconds=5 * 3600)

        launched = await _pass(ctx, PrintScheduler())

        assert _launched_ids(launched) == [item_id]
        assert (await _item(ctx, item_id)).printer_id == 2

    @pytest.mark.asyncio
    async def test_an_item_a_person_started_ignores_the_slot(self, ctx):
        await self._schedule(ctx)
        item_id = await _add_item(ctx, print_time_seconds=5 * 3600, user_started=True)

        launched = await _pass(ctx, PrintScheduler())

        assert _launched_ids(launched) == [item_id]

    @pytest.mark.asyncio
    async def test_a_disabled_or_manual_item_reserves_nothing(self, ctx):
        next_at = _utcnow_naive() + timedelta(hours=1)
        await _add_maintenance(ctx, trigger_mode="manual", schedule_next_at=next_at, reserve_before_schedule=True)
        await _add_maintenance(
            ctx,
            trigger_mode="schedule",
            schedule_days=[0],
            schedule_time="06:00",
            schedule_next_at=next_at,
            reserve_before_schedule=True,
            enabled=False,
        )
        item_id = await _add_item(ctx, print_time_seconds=5 * 3600)

        launched = await _pass(ctx, PrintScheduler())

        assert _launched_ids(launched) == [item_id]

    @pytest.mark.asyncio
    async def test_the_hold_is_not_worth_a_job_waiting_notification(self, ctx):
        await self._schedule(ctx)
        await _add_item(ctx, print_time_seconds=5 * 3600)
        waiting = AsyncMock()

        await _pass(ctx, PrintScheduler(), waiting=waiting)

        waiting.assert_not_called()
