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

Neither applies to an item a person started by hand -- ▶ on a staged item,
which is what ``user_started`` records. Nothing the print dialog queues
counts, "ASAP" included: that is a place at the top of the queue, and the
scheduler then dispatches the item like any other.

Both are holds on the item, not on the printer: the printer is idle, and
``busy_printers`` keeps meaning "a print is on it or imminent", which is
what keep-warm reads it as. So a ▶ item behind a held one on the same
printer still goes out, and no bed is heated for a job the run is waiting
to cool it for.

Driven through ``check_queue`` like the #3074 tests: real rows, the printer
manager and the upload launcher mocked.
"""

from contextlib import ExitStack
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import backend.app.models  # noqa: F401 - populate Base.metadata
from backend.app.core.database import Base
from backend.app.models.archive import PrintArchive
from backend.app.models.library import LibraryFile
from backend.app.models.maintenance import MaintenanceRun, MaintenanceType, PrinterMaintenance
from backend.app.models.print_queue import PrintQueueItem
from backend.app.models.printer import Printer
from backend.app.models.settings import Settings
from backend.app.models.smart_plug import SmartPlug
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
    **columns,
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
            **columns,
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


async def _add_auto_on_plug(ctx, printer_id=1):
    async with ctx.session_maker() as db:
        db.add(SmartPlug(name=f"plug-{printer_id}", printer_id=printer_id, enabled=True, auto_on=True))
        await db.commit()


async def _plugs(ctx):
    async with ctx.session_maker() as db:
        return list((await db.execute(select(SmartPlug))).scalars().all())


async def _set_settings(ctx, **values):
    async with ctx.session_maker() as db:
        for key, value in values.items():
            db.add(Settings(key=key, value="true" if value is True else "false" if value is False else str(value)))
        await db.commit()


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


def _status(bed=20.0, state_name="IDLE", trays=None, bed_target=None):
    state = MagicMock()
    state.state = state_name
    state.connected = True
    state.temperatures = {"bed": bed} if bed_target is None else {"bed": bed, "bed_target": bed_target}
    state.internal_gcode_dir = "O1S"
    if trays is not None:
        state.raw_data = {"ams": [{"tray": trays}], "vt_tray": []}
    return state


def _tray(filament_type, color):
    return {"tray_type": filament_type, "tray_color": color, "tray_info_idx": ""}


async def _pass(
    ctx, scheduler, *, idle=True, connected=True, bed=20.0, launched=None, waiting=None, status=None, patches=()
):
    """One check_queue pass with every printer connected and (by default) idle.

    *status* replaces the one status every printer reports; *patches* are
    applied after the defaults and win over them.
    """
    launched = launched or MagicMock()
    waiting = waiting or AsyncMock()
    if status is None:
        status = MagicMock(return_value=_status(bed) if connected else None)
    patches = [
        patch("backend.app.services.print_scheduler.async_session", ctx.session_maker),
        patch("backend.app.core.database.async_session", ctx.session_maker),
        patch("backend.app.services.print_scheduler.printer_manager.is_connected", MagicMock(return_value=connected)),
        patch("backend.app.services.print_scheduler.printer_manager.get_status", status),
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
        *patches,
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
        """▶ on a staged item means now: the run waits for the print instead,
        as it does for any print already on the printer."""
        await _add_maintenance(ctx, action_options={"bed_temp_below": 30}, run={})
        item_id = await _add_item(ctx, user_started=True)

        launched = await _pass(ctx, PrintScheduler(), bed=45.0)

        assert _launched_ids(launched) == [item_id]
        assert (await _item(ctx, item_id)).waiting_reason is None

    @pytest.mark.asyncio
    async def test_an_item_a_person_started_goes_out_behind_a_held_one(self, ctx):
        """▶ is pressed on a staged item that is not first in line for its
        printer -- the normal case, /start does not move it. The hold on the
        item ahead is a hold on that item, not on the printer, so the ▶ item
        goes out and the one ahead keeps reading the run."""
        await _add_maintenance(ctx, action_options={"bed_temp_below": 30}, run={})
        ahead = await _add_item(ctx, position=1)
        started = await _add_item(ctx, position=2, user_started=True)

        launched = await _pass(ctx, PrintScheduler(), bed=45.0)

        assert _launched_ids(launched) == [started]
        assert (await _item(ctx, ahead)).waiting_reason == (
            "Maintenance run pending: Printer Calibration (bed still warm, 45 °C)"
        )
        assert (await _item(ctx, started)).waiting_reason is None

    @pytest.mark.asyncio
    async def test_a_run_hold_releases_keep_warm_rather_than_engaging_it(self, ctx):
        """The Sunday afternoon: an ASA job finishes, keep-warm holds the bed
        at 90 °C for the ASA job behind it while the plate is not confirmed.
        The plate is confirmed, the calibration run is created and waits for
        the bed to cool -- and keep-warm must let go of the bed now, not heat
        it for the job the run is holding. Held, the printer is idle and not
        in busy_printers, which is what keep-warm reads as the gap to heat."""
        await _set_settings(ctx, queue_keep_bed_warm=True, preheat_enabled=True, require_plate_clear=True)
        item_id = await _add_item(ctx, preheat_chamber_target_override=60)
        client = MagicMock()
        scheduler = PrintScheduler()
        client_patch = patch(
            "backend.app.services.print_scheduler.printer_manager.get_client", MagicMock(return_value=client)
        )

        # Plate not confirmed yet: the printer is FINISH and held, keep-warm engages.
        finish = MagicMock(return_value=_status(bed=60.0, state_name="FINISH"))
        await _pass(ctx, scheduler, idle=False, status=finish, patches=[client_patch])
        assert scheduler._keep_warm[1].held_target == 90
        client.set_bed_temperature.assert_called_once_with(90)
        assert (await _item(ctx, item_id)).waiting_reason == "Busy: H2S-01"

        # Plate confirmed, the run created and waiting for the bed: released.
        await _add_maintenance(ctx, action_options={"bed_temp_below": 30}, run={})
        client.set_bed_temperature.reset_mock()
        finish = MagicMock(return_value=_status(bed=60.0, state_name="FINISH", bed_target=90))
        launched = await _pass(ctx, scheduler, status=finish, patches=[client_patch])

        launched.assert_not_called()
        assert 1 not in scheduler._keep_warm
        client.set_bed_temperature.assert_called_once_with(0)
        assert (await _item(ctx, item_id)).waiting_reason == (
            "Maintenance run pending: Printer Calibration (bed still warm, 60 °C)"
        )

    @pytest.mark.asyncio
    async def test_keep_warm_leaves_the_bed_of_a_reserved_printer_alone(self, ctx):
        """The same the other way round: the plate is not confirmed yet, so the
        printer is busy for a reason of its own and keep-warm would engage --
        but the item it would heat the bed for is not going out there while the
        run is pending, and the run may be the one waiting for a cold bed."""
        await _set_settings(ctx, queue_keep_bed_warm=True, preheat_enabled=True, require_plate_clear=True)
        await _add_maintenance(ctx, action_options={"bed_temp_below": 30}, run={})
        await _add_item(ctx, preheat_chamber_target_override=60)
        client = MagicMock()
        scheduler = PrintScheduler()
        finish = MagicMock(return_value=_status(bed=60.0, state_name="FINISH"))

        await _pass(
            ctx,
            scheduler,
            idle=False,
            status=finish,
            patches=[
                patch("backend.app.services.print_scheduler.printer_manager.get_client", MagicMock(return_value=client))
            ],
        )

        assert 1 not in scheduler._keep_warm
        client.set_bed_temperature.assert_not_called()

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
        """Named like "Busy: H2S-01" is: the row says which printer it means."""
        await _add_maintenance(ctx, action_options={"bed_temp_below": 30}, run={})
        item_id = await _add_item(ctx, printer_id=None, target_model="H2S")

        launched = await _pass(ctx, PrintScheduler(), bed=45.0)

        launched.assert_not_called()
        assert (await _item(ctx, item_id)).waiting_reason == (
            "Maintenance run pending: Printer Calibration (bed still warm, 45 °C) — H2S-01"
        )

    @pytest.mark.asyncio
    async def test_printers_under_the_same_hold_share_one_clause(self, ctx):
        """The user's farm: every printer has its run waiting on the bed. One
        sentence naming them all, not the sentence once per printer."""
        await _add_printer(ctx, 2, "H2S-02")
        await _add_maintenance(ctx, printer_id=1, action_options={"bed_temp_below": 30}, run={})
        await _add_maintenance(ctx, printer_id=2, action_options={"bed_temp_below": 30}, run={})
        item_id = await _add_item(ctx, printer_id=None, target_model="H2S")
        waiting = AsyncMock()

        launched = await _pass(ctx, PrintScheduler(), bed=45.0, waiting=waiting)

        launched.assert_not_called()
        assert (await _item(ctx, item_id)).waiting_reason == (
            "Maintenance run pending: Printer Calibration (bed still warm, 45 °C) — H2S-01, H2S-02"
        )
        waiting.assert_not_called()

    @pytest.mark.asyncio
    async def test_a_reserved_printer_that_has_the_colour_is_named_as_reserved(self, ctx):
        """Force-colour job for red PLA. H2S-01 has it loaded and a run
        pending; H2S-02 has only black. The row names the run on H2S-01 --
        the job starts by itself when it closes -- rather than asking for a
        spool change, and nobody is notified about one."""
        await _add_printer(ctx, 2, "H2S-02")
        await _add_maintenance(ctx, printer_id=1, action_options={"bed_temp_below": 30}, run={})
        item_id = await _add_item(
            ctx,
            printer_id=None,
            target_model="H2S",
            required_filament_types='["PLA"]',
            filament_overrides='[{"type": "PLA", "color": "#FF0000", "color_name": "red", "force_color_match": true}]',
        )
        loaded = {1: _status(bed=45.0, trays=[_tray("PLA", "FF0000FF")]), 2: _status(trays=[_tray("PLA", "000000FF")])}
        waiting = AsyncMock()

        launched = await _pass(ctx, PrintScheduler(), status=MagicMock(side_effect=loaded.get), waiting=waiting)

        launched.assert_not_called()
        assert (await _item(ctx, item_id)).waiting_reason == (
            "Maintenance run pending: Printer Calibration (bed still warm, 45 °C) — H2S-01"
        )
        waiting.assert_not_called()

    @pytest.mark.asyncio
    async def test_a_reserved_printer_without_the_colour_still_asks_for_the_spool(self, ctx):
        """The same job when neither printer has red loaded: the run on
        H2S-01 is beside the point, the spool is what the job waits for."""
        await _add_printer(ctx, 2, "H2S-02")
        await _add_maintenance(ctx, printer_id=1, action_options={"bed_temp_below": 30}, run={})
        item_id = await _add_item(
            ctx,
            printer_id=None,
            target_model="H2S",
            required_filament_types='["PLA"]',
            filament_overrides='[{"type": "PLA", "color": "#FF0000", "color_name": "red", "force_color_match": true}]',
        )
        loaded = {1: _status(bed=45.0, trays=[_tray("PLA", "000000FF")]), 2: _status(trays=[_tray("PLA", "000000FF")])}

        launched = await _pass(ctx, PrintScheduler(), status=MagicMock(side_effect=loaded.get))

        launched.assert_not_called()
        assert (await _item(ctx, item_id)).waiting_reason == "No matching material/color. Waiting on PLA (red)"

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
        assert (await _item(ctx, item_id)).waiting_reason == self._hold(next_at, "estimated 5h 0m") + " — H2S-01"

    @pytest.mark.asyncio
    async def test_the_farm_scheduled_for_the_same_slot_reads_as_one_clause(self, ctx):
        """Every H2S scheduled for Sunday noon: the "Any H2S" row names the
        slot once, with the printers under it, for the two hours it is held."""
        await _add_printer(ctx, 2, "H2S-02")
        next_at = await self._schedule(ctx, hours_ahead=1.5, printer_id=1)
        await self._schedule(ctx, hours_ahead=1.5, printer_id=2)
        item_id = await _add_item(ctx, printer_id=None, target_model="H2S", print_time_seconds=None)

        launched = await _pass(ctx, PrintScheduler())

        launched.assert_not_called()
        assert (await _item(ctx, item_id)).waiting_reason == (
            self._hold(next_at, "duration unknown") + " — H2S-01, H2S-02"
        )

    @pytest.mark.asyncio
    async def test_an_offline_printer_is_not_switched_on_for_a_job_the_slot_would_hold(self, ctx):
        """The printer is off with an Auto On plug and the job would run into
        the slot: nothing to gain from a boot, the item is held as it would
        be with the printer up, and the plug is left alone."""
        next_at = await self._schedule(ctx, hours_ahead=1.5)
        await _add_auto_on_plug(ctx)
        item_id = await _add_item(ctx, print_time_seconds=None)
        scheduler = PrintScheduler()
        power_on = AsyncMock(return_value=True)
        plugs = await _plugs(ctx)

        launched = await _pass(
            ctx,
            scheduler,
            connected=False,
            patches=[
                patch.object(scheduler, "_get_smart_plugs", AsyncMock(return_value=plugs)),
                patch.object(scheduler, "_power_on_and_wait", power_on),
            ],
        )

        launched.assert_not_called()
        power_on.assert_not_called()
        assert (await _item(ctx, item_id)).waiting_reason == self._hold(next_at, "duration unknown")

    @pytest.mark.asyncio
    async def test_a_model_based_job_does_not_wake_a_printer_the_slot_would_hold(self, ctx):
        """The same for "Any H2S": the wake step passes the printer over, and
        the row names the slot rather than an offline printer Bambuddy is
        deliberately not switching on -- so nobody is told to go and do it."""
        next_at = await self._schedule(ctx, hours_ahead=1.5)
        await _add_auto_on_plug(ctx)
        item_id = await _add_item(ctx, printer_id=None, target_model="H2S", print_time_seconds=None)
        scheduler = PrintScheduler()
        power_on = AsyncMock(return_value=True)
        plugs = await _plugs(ctx)
        waiting = AsyncMock()

        launched = await _pass(
            ctx,
            scheduler,
            connected=False,
            waiting=waiting,
            patches=[
                patch.object(scheduler, "_get_smart_plugs", AsyncMock(return_value=plugs)),
                patch.object(scheduler, "_power_on_and_wait", power_on),
            ],
        )

        launched.assert_not_called()
        power_on.assert_not_called()
        assert (await _item(ctx, item_id)).waiting_reason == self._hold(next_at, "duration unknown") + " — H2S-01"
        waiting.assert_not_called()

    @pytest.mark.asyncio
    async def test_an_offline_printer_is_still_switched_on_for_a_job_that_fits(self, ctx):
        """The control: with the slot far enough out the wake goes ahead."""
        await self._schedule(ctx, hours_ahead=4.0)
        await _add_auto_on_plug(ctx)
        await _add_item(ctx, print_time_seconds=3600)
        scheduler = PrintScheduler()
        power_on = AsyncMock(return_value=False)
        plugs = await _plugs(ctx)

        await _pass(
            ctx,
            scheduler,
            connected=False,
            patches=[
                patch.object(scheduler, "_get_smart_plugs", AsyncMock(return_value=plugs)),
                patch.object(scheduler, "_power_on_and_wait", power_on),
            ],
        )

        power_on.assert_called_once()

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


class TestTheExemptionIsSpentAtDispatch:
    """``user_started`` is a one-shot, not a latch (#3127).

    ▶ asks for *this* dispatch. The row outlives it -- the watchdog and the
    busy-printer path revert to pending, and the failure gate parks items as
    skipped for a later resume -- so an exemption that is never cleared keeps
    overriding every maintenance hold the item ever meets again.
    """

    @pytest.fixture
    async def dispatch(self, ctx, tmp_path):
        base_dir = tmp_path / "case"
        archive_rel = Path("archives") / "job.3mf"
        archive_abs = base_dir / archive_rel
        archive_abs.parent.mkdir(parents=True, exist_ok=True)
        archive_abs.write_bytes(b"archive payload")

        async with ctx.session_maker() as db:
            archive = PrintArchive(
                printer_id=1,
                filename="job.3mf",
                file_path=str(archive_rel),
                file_size=archive_abs.stat().st_size,
                status="completed",
            )
            db.add(archive)
            await db.flush()
            item = PrintQueueItem(
                status="pending",
                position=1,
                printer_id=1,
                archive_id=archive.id,
                print_time_seconds=3600,
                user_started=True,
            )
            db.add(item)
            await db.commit()
            item_id = item.id
        return SimpleNamespace(base_dir=base_dir, item_id=item_id)

    async def _dispatch(self, ctx, dispatch, *, start_print: bool):
        import backend.app.services.print_scheduler as scheduler_module
        from backend.tests._fixtures.background_tasks import discarding_spawn_patch

        scheduler = PrintScheduler()
        async with ctx.session_maker() as db:
            item = await db.get(PrintQueueItem, dispatch.item_id)
            patches = [
                patch.object(scheduler_module.settings, "base_dir", dispatch.base_dir),
                patch("backend.app.services.print_scheduler.async_session", ctx.session_maker),
                patch(
                    "backend.app.services.print_scheduler.printer_manager.is_connected",
                    MagicMock(return_value=True),
                ),
                patch(
                    "backend.app.services.print_scheduler.printer_manager.get_status",
                    MagicMock(return_value=_status()),
                ),
                patch(
                    "backend.app.services.print_scheduler.printer_manager.start_print",
                    MagicMock(return_value=start_print),
                ),
                patch(
                    "backend.app.services.print_scheduler.get_ftp_retry_settings",
                    AsyncMock(return_value=(False, 0, 0, 1.0)),
                ),
                patch("backend.app.services.print_scheduler.upload_file_async", AsyncMock(return_value=True)),
                patch("backend.app.services.print_scheduler.delete_file_async", AsyncMock(return_value=True)),
                discarding_spawn_patch(),
                patch.object(scheduler, "_propagate_owner_to_printer_manager", AsyncMock()),
                patch.object(scheduler, "_power_off_if_needed", AsyncMock()),
                patch.object(scheduler, "_preheat_and_soak", AsyncMock()),
            ]
            with ExitStack() as stack:
                for p in patches:
                    stack.enter_context(p)
                await scheduler._start_print(db, item)

        async with ctx.session_maker() as db:
            return await db.get(PrintQueueItem, dispatch.item_id)

    @pytest.mark.asyncio
    async def test_a_dispatched_item_no_longer_carries_the_exemption(self, ctx, dispatch):
        row = await self._dispatch(ctx, dispatch, start_print=True)

        assert row.status == "printing"
        assert row.user_started is False

    @pytest.mark.asyncio
    async def test_an_item_reverted_after_dispatch_is_held_like_any_other(self, ctx, dispatch):
        """start_print() refused, so the row goes back to the queue. It has
        had its turn; the next pass weighs it against the schedule again."""
        row = await self._dispatch(ctx, dispatch, start_print=False)

        assert row.status in ("pending", "failed")
        assert row.user_started is False
