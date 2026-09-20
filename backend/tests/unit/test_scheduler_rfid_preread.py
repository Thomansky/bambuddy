"""The queue reads unidentified AMS spools before a job starts.

A spool put into the AMS while the printer is printing is detected but never
read: the AMS cannot move filament during a print, and it does not go back to
the slot afterwards. The next queued job is then mapped against a slot whose
filament is unknown, or somebody walks over and presses Re-read RFID per slot
before every job. With ``queue_rfid_reread_before_start`` on, the scheduler
asks the AMS itself, once per item, right before the mapping is resolved --
and the job starts on the next pass whatever came of it.

The contract these tests pin:

- off: the scheduler behaves exactly as before, no command, no hold;
- on, with unread slots and no filament loaded: one ``ams_get_rfid`` per slot,
  the item held for this pass, the printer reserved against every other item
  and released by the read task alone, then the item dispatched on the next
  pass -- even when no tray ever changed;
- on, but filament loaded, or nothing to read: no command, dispatched at once;
- one round per item, and a slot whose read completed gets the same K-profile
  re-apply the manual Re-read RFID button triggers;
- the read runs before ``_ensure_ams_mapping``, never after it: the mapping
  is the reader that has to see the identified spools;
- an "Any <model>" item the matcher turns down because the only spool it
  needs is the unidentified one gets those printers read first, unpinned,
  and is matched again against what the AMS found.
"""

import asyncio
from contextlib import ExitStack
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import backend.app.models  # noqa: F401 - populate Base.metadata
from backend.app.core.database import Base
from backend.app.models.library import LibraryFile
from backend.app.models.print_queue import PrintQueueItem
from backend.app.models.printer import Printer
from backend.app.models.settings import Settings
from backend.app.services.print_scheduler import RFID_REREAD_HOLD, PrintScheduler


@pytest.fixture
async def ctx():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    session_maker = async_sessionmaker(engine, expire_on_commit=False)

    async with session_maker() as db:
        db.add(
            Printer(
                id=1,
                name="X1C-01",
                serial_number="X1C0001",
                ip_address="10.0.0.1",
                access_code="x",
                model="X1C",
                is_active=True,
            )
        )
        await db.commit()

    try:
        yield SimpleNamespace(session_maker=session_maker)
    finally:
        await engine.dispose()


async def _add_item(ctx, *, printer_id=1, target_model=None, position=1, required_filament_types=None):
    async with ctx.session_maker() as db:
        lib = LibraryFile(
            filename="job.gcode.3mf",
            file_path="/library/job.gcode.3mf",
            file_size=10,
            file_type="gcode.3mf",
            file_metadata={"sliced_for_model": "X1C"},
        )
        db.add(lib)
        await db.flush()
        item = PrintQueueItem(
            status="pending",
            position=position,
            printer_id=printer_id,
            target_model=target_model,
            library_file_id=lib.id,
            required_filament_types=required_filament_types,
        )
        db.add(item)
        await db.commit()
        return item.id


async def _set(ctx, key, value):
    async with ctx.session_maker() as db:
        db.add(Settings(key=key, value=value))
        await db.commit()


async def _item(ctx, item_id):
    async with ctx.session_maker() as db:
        return (await db.execute(select(PrintQueueItem).where(PrintQueueItem.id == item_id))).scalar_one()


def _tray(tray_id, *, read=True, tray_type="PLA"):
    if read:
        return {"id": str(tray_id), "tray_type": tray_type, "tray_info_idx": "GFA01", "tag_uid": "3CA4E7DF00000100"}
    return {"id": str(tray_id), "tray_type": "", "tray_info_idx": "", "tag_uid": "0000000000000000"}


def _printer_state(*, tray_now=255, unread=(3,), exist="f"):
    """An idle X1C whose AMS 0 has the slots in *unread* occupied but unread."""
    done = 0xF
    for slot in unread:
        done &= ~(1 << slot)
    return SimpleNamespace(
        state="IDLE",
        subtask_id=None,
        tray_now=tray_now,
        tray_exist_bits=exist,
        tray_read_done_bits=format(done, "x"),
        raw_data={"ams": [{"id": "0", "tray": [_tray(i, read=(i not in unread)) for i in range(4)]}]},
        hms_errors=[],
    )


class _Harness:
    """One scheduler pass with a fake printer, plus the read task it spawned."""

    def __init__(self, ctx, scheduler, state, *, refresh=None, states=None):
        self.ctx = ctx
        self.scheduler = scheduler
        self.state = state
        # Per-printer states for a farm; printers not listed report `state`.
        self.states = states or {}
        self.client = MagicMock()
        self.client.ams_refresh_tray = refresh or MagicMock(return_value=(True, "Refreshing"))
        self.tasks: list[asyncio.Task] = []
        self.launched = MagicMock()
        self.pa_applied = AsyncMock()
        self.ensure_mapping = AsyncMock(return_value=None)
        self.waiting_notified = AsyncMock()

    def _spawn(self, coro, *, name=None):
        task = asyncio.ensure_future(coro)
        self.tasks.append(task)
        return task

    async def run(self, *, slot_timeout=0.05):
        """One pass, then the read task it spawned run to completion under the
        same patches, so its polling sees the fake printer."""
        with self.patched(slot_timeout=slot_timeout):
            await self.scheduler.check_queue()
            for task in self.tasks:
                await task
        return self

    def patched(self, *, slot_timeout=0.05):
        stack = ExitStack()
        for p in _patches(self):
            stack.enter_context(p)
        stack.enter_context(patch("backend.app.services.print_scheduler._RFID_REREAD_SLOT_TIMEOUT", slot_timeout))
        return stack

    def dispatched(self):
        return [ids for (ids, *_rest) in (c.args for c in self.launched.call_args_list)]

    def mapped(self):
        """(printer_id, item_id) per `_ensure_ams_mapping` call, in order."""
        return [(c.args[1], c.args[2].id) for c in self.ensure_mapping.await_args_list]


class TestSettingOff:
    @pytest.mark.asyncio
    async def test_nothing_is_read_and_the_item_goes_straight_out(self, ctx):
        item_id = await _add_item(ctx)
        h = await _Harness(ctx, PrintScheduler(), _printer_state()).run()

        h.client.ams_refresh_tray.assert_not_called()
        assert h.dispatched() == [[item_id]]
        assert h.mapped() == [(1, item_id)]
        assert h.tasks == []
        assert (await _item(ctx, item_id)).rfid_precheck_at is None


class TestSettingOn:
    @pytest.mark.asyncio
    async def test_unread_slots_are_read_and_the_item_waits_one_pass(self, ctx):
        await _set(ctx, "queue_rfid_reread_before_start", "true")
        item_id = await _add_item(ctx)
        scheduler = PrintScheduler()
        state = _printer_state(unread=(1, 3))

        h = _Harness(ctx, scheduler, state)
        # Inspect the hold while the read is still running: the task is
        # awaited by run(), so look before that from inside the refresh call.
        seen: dict[str, object] = {}

        def refresh(ams_id, slot_id):
            seen.setdefault("held", scheduler._printer_in_dispatch_hold(1))
            seen.setdefault("attributed", scheduler._rfid_rereads.get(1))
            return True, "Refreshing"

        h.client.ams_refresh_tray = MagicMock(side_effect=refresh)
        await h.run()

        # One ams_get_rfid per unread slot, in slot order, nothing dispatched
        # -- and nothing mapped: the mapping is what has to wait for the read.
        assert h.client.ams_refresh_tray.call_args_list == [((0, 1),), ((0, 3),)]
        assert h.dispatched() == []
        h.ensure_mapping.assert_not_called()
        row = await _item(ctx, item_id)
        assert row.waiting_reason == RFID_REREAD_HOLD
        assert row.rfid_precheck_at is not None
        # While it ran, the printer was reserved and the item named as the reason.
        assert seen == {"held": True, "attributed": item_id}
        # No tray ever changed, so both reads timed out -- and the task still
        # let go of the printer.
        assert 1 not in scheduler._dispatch_holds
        assert 1 not in scheduler._rfid_rereads
        h.pa_applied.assert_not_called()

        # Next pass: the stamp keeps it from asking again; the mapping is
        # resolved against the printer the read ran on, and the item goes out.
        h2 = await _Harness(ctx, scheduler, state).run()
        h2.client.ams_refresh_tray.assert_not_called()
        assert h2.mapped() == [(1, item_id)]
        assert h2.dispatched() == [[item_id]]
        assert (await _item(ctx, item_id)).waiting_reason is None

    @pytest.mark.asyncio
    async def test_the_reservation_keeps_every_other_item_off_the_printer(self, ctx):
        await _set(ctx, "queue_rfid_reread_before_start", "true")
        first = await _add_item(ctx, position=1)
        second = await _add_item(ctx, position=2)
        scheduler = PrintScheduler()

        h = await _Harness(ctx, scheduler, _printer_state()).run()

        assert h.dispatched() == []
        assert (await _item(ctx, first)).waiting_reason == RFID_REREAD_HOLD
        assert (await _item(ctx, second)).waiting_reason == "Busy: X1C-01"
        # The second item was never stamped: the round belongs to the first.
        assert (await _item(ctx, second)).rfid_precheck_at is None

    @pytest.mark.asyncio
    async def test_the_asking_item_keeps_saying_reading_while_the_read_runs(self, ctx):
        """A pass that lands mid-read finds the printer held. Every other item
        reads that as Busy; the one whose read it is must not."""
        await _set(ctx, "queue_rfid_reread_before_start", "true")
        item_id = await _add_item(ctx)
        scheduler = PrintScheduler()
        h = _Harness(ctx, scheduler, _printer_state())
        started = asyncio.Event()

        def refresh(ams_id, slot_id):
            started.set()
            return True, "Refreshing"

        h.client.ams_refresh_tray = MagicMock(side_effect=refresh)

        with h.patched(slot_timeout=30.0):
            await scheduler.check_queue()
            await started.wait()
            assert (await _item(ctx, item_id)).waiting_reason == RFID_REREAD_HOLD
            # Second pass, mid-read: held by the reservation, still "reading".
            await scheduler.check_queue()
            assert (await _item(ctx, item_id)).waiting_reason == RFID_REREAD_HOLD
            assert h.dispatched() == []
            assert len(h.tasks) == 1

            # A cancelled task still hands the printer back.
            h.tasks[0].cancel()
            with pytest.raises(asyncio.CancelledError):
                await h.tasks[0]
        assert 1 not in scheduler._dispatch_holds
        assert 1 not in scheduler._rfid_rereads

    @pytest.mark.asyncio
    async def test_filament_loaded_skips_the_read_and_dispatches_now(self, ctx):
        await _set(ctx, "queue_rfid_reread_before_start", "true")
        item_id = await _add_item(ctx)

        h = await _Harness(ctx, PrintScheduler(), _printer_state(tray_now=1)).run()

        h.client.ams_refresh_tray.assert_not_called()
        assert h.tasks == []
        assert h.mapped() == [(1, item_id)]
        assert h.dispatched() == [[item_id]]
        assert (await _item(ctx, item_id)).rfid_precheck_at is not None

    @pytest.mark.asyncio
    async def test_nothing_unread_dispatches_now(self, ctx):
        await _set(ctx, "queue_rfid_reread_before_start", "true")
        item_id = await _add_item(ctx)

        h = await _Harness(ctx, PrintScheduler(), _printer_state(unread=())).run()

        h.client.ams_refresh_tray.assert_not_called()
        assert h.mapped() == [(1, item_id)]
        assert h.dispatched() == [[item_id]]
        assert (await _item(ctx, item_id)).rfid_precheck_at is not None

    @pytest.mark.asyncio
    async def test_a_stamped_item_never_gets_a_second_round(self, ctx):
        await _set(ctx, "queue_rfid_reread_before_start", "true")
        item_id = await _add_item(ctx)
        async with ctx.session_maker() as db:
            row = (await db.execute(select(PrintQueueItem).where(PrintQueueItem.id == item_id))).scalar_one()
            from datetime import datetime, timezone

            row.rfid_precheck_at = datetime.now(timezone.utc)
            await db.commit()

        h = await _Harness(ctx, PrintScheduler(), _printer_state()).run()

        h.client.ams_refresh_tray.assert_not_called()
        assert h.dispatched() == [[item_id]]

    @pytest.mark.asyncio
    async def test_a_completed_read_gets_the_k_profile_re_apply(self, ctx):
        await _set(ctx, "queue_rfid_reread_before_start", "true")
        item_id = await _add_item(ctx)
        state = _printer_state(unread=(3,))

        def refresh(ams_id, slot_id):
            # The AMS reads the tag: the read_done bit comes on.
            state.tray_read_done_bits = "f"
            return True, "Refreshing"

        h = await _Harness(ctx, PrintScheduler(), state, refresh=MagicMock(side_effect=refresh)).run()

        h.pa_applied.assert_awaited_once_with(1, 0, 3)
        assert h.dispatched() == []
        h.ensure_mapping.assert_not_called()
        assert (await _item(ctx, item_id)).waiting_reason == RFID_REREAD_HOLD

    @pytest.mark.asyncio
    async def test_without_read_done_bits_a_changed_tray_counts_as_read(self, ctx):
        await _set(ctx, "queue_rfid_reread_before_start", "true")
        await _add_item(ctx)
        state = _printer_state(unread=(2,))
        state.tray_read_done_bits = None

        def refresh(ams_id, slot_id):
            state.raw_data["ams"][0]["tray"][2] = _tray(2)
            return True, "Refreshing"

        h = await _Harness(ctx, PrintScheduler(), state, refresh=MagicMock(side_effect=refresh)).run()

        h.pa_applied.assert_awaited_once_with(1, 0, 2)

    @pytest.mark.asyncio
    async def test_a_refused_slot_is_skipped_not_waited_on(self, ctx):
        await _set(ctx, "queue_rfid_reread_before_start", "true")
        await _add_item(ctx)
        refresh = MagicMock(return_value=(False, "Please unload filament first"))

        h = await _Harness(ctx, PrintScheduler(), _printer_state(unread=(0, 1)), refresh=refresh).run()

        assert refresh.call_count == 2
        h.pa_applied.assert_not_called()
        assert 1 not in h.scheduler._dispatch_holds

    @pytest.mark.asyncio
    async def test_the_printer_is_released_when_the_read_blows_up(self, ctx):
        await _set(ctx, "queue_rfid_reread_before_start", "true")
        await _add_item(ctx)
        refresh = MagicMock(side_effect=RuntimeError("mqtt gone"))
        scheduler = PrintScheduler()

        await _Harness(ctx, scheduler, _printer_state(), refresh=refresh).run()

        assert 1 not in scheduler._dispatch_holds
        assert 1 not in scheduler._rfid_rereads

    @pytest.mark.asyncio
    async def test_at_most_eight_slots_per_round(self, ctx):
        await _set(ctx, "queue_rfid_reread_before_start", "true")
        await _add_item(ctx)
        state = _printer_state(unread=(0, 1, 2, 3))
        state.raw_data["ams"].append({"id": "1", "tray": [_tray(i, read=False) for i in range(4)]})
        state.raw_data["ams"].append({"id": "2", "tray": [_tray(i, read=False) for i in range(4)]})
        state.tray_exist_bits = "fff"
        state.tray_read_done_bits = "0"

        h = await _Harness(ctx, PrintScheduler(), state).run()

        assert h.client.ams_refresh_tray.call_count == 8


class TestModelBasedItems:
    @pytest.mark.asyncio
    async def test_the_read_runs_before_the_mapping_there_too(self, ctx):
        await _set(ctx, "queue_rfid_reread_before_start", "true")
        item_id = await _add_item(ctx, printer_id=None, target_model="X1C")
        scheduler = PrintScheduler()
        h = _Harness(ctx, scheduler, _printer_state())

        with patch.object(scheduler, "_find_idle_printer_for_model", AsyncMock(return_value=(1, None))):
            await h.run()

        assert h.client.ams_refresh_tray.call_args_list == [((0, 3),)]
        assert h.dispatched() == []
        h.ensure_mapping.assert_not_called()
        row = await _item(ctx, item_id)
        assert row.printer_id == 1
        assert row.waiting_reason == RFID_REREAD_HOLD

        # Now pinned, the next pass takes the fixed-printer branch: mapping
        # resolved against the read printer, then out.
        h2 = await _Harness(ctx, scheduler, _printer_state()).run()
        h2.client.ams_refresh_tray.assert_not_called()
        assert h2.mapped() == [(1, item_id)]
        assert h2.dispatched() == [[item_id]]


class TestModelBasedItemsTurnedDownForFilament:
    """The real matcher counts identified spools only. An "Any X1C" job for
    PETG on a printer whose one PETG is the spool put in mid-print is turned
    down for "needs PETG" -- so the read has to happen before a printer is
    matched, not after."""

    NEEDS_PETG = '["PETG"]'

    def _reveals(self, state, tray_type):
        def refresh(ams_id, slot_id):
            state.raw_data["ams"][0]["tray"][slot_id] = _tray(slot_id, tray_type=tray_type)
            state.tray_read_done_bits = "f"
            return True, "Refreshing"

        return MagicMock(side_effect=refresh)

    @pytest.mark.asyncio
    async def test_the_turned_down_printer_is_read_and_matched_on_the_next_pass(self, ctx):
        await _set(ctx, "queue_rfid_reread_before_start", "true")
        item_id = await _add_item(ctx, printer_id=None, target_model="X1C", required_filament_types=self.NEEDS_PETG)
        scheduler = PrintScheduler()
        state = _printer_state(unread=(3,))

        h = await _Harness(ctx, scheduler, state, refresh=self._reveals(state, "PETG")).run()

        # Read, held, reserved -- and not pinned: the matcher decides next pass.
        assert h.client.ams_refresh_tray.call_args_list == [((0, 3),)]
        assert h.dispatched() == []
        h.ensure_mapping.assert_not_called()
        h.waiting_notified.assert_not_called()
        row = await _item(ctx, item_id)
        assert row.printer_id is None
        assert row.waiting_reason == RFID_REREAD_HOLD
        assert row.rfid_precheck_at is not None
        assert 1 not in scheduler._dispatch_holds

        # The AMS found PETG: matched, mapped against it, dispatched. No
        # second read -- the stamp holds on the model-based path too.
        h2 = await _Harness(ctx, scheduler, state).run()
        h2.client.ams_refresh_tray.assert_not_called()
        assert h2.mapped() == [(1, item_id)]
        assert h2.dispatched() == [[item_id]]
        assert (await _item(ctx, item_id)).printer_id == 1

    @pytest.mark.asyncio
    async def test_a_read_that_finds_the_wrong_spool_leaves_the_job_waiting_for_filament(self, ctx):
        await _set(ctx, "queue_rfid_reread_before_start", "true")
        item_id = await _add_item(ctx, printer_id=None, target_model="X1C", required_filament_types=self.NEEDS_PETG)
        scheduler = PrintScheduler()
        state = _printer_state(unread=(3,))

        h = await _Harness(ctx, scheduler, state, refresh=self._reveals(state, "PLA")).run()
        assert (await _item(ctx, item_id)).waiting_reason == RFID_REREAD_HOLD
        h.waiting_notified.assert_not_called()

        h2 = await _Harness(ctx, scheduler, state).run()

        assert h2.dispatched() == []
        h2.ensure_mapping.assert_not_called()
        row = await _item(ctx, item_id)
        assert row.printer_id is None
        assert "PETG" in row.waiting_reason
        # The read hold was a step, not a wait: this is the first thing the
        # user hears about the job, so it is said out loud.
        h2.waiting_notified.assert_awaited_once()
        assert h2.waiting_notified.await_args.kwargs["waiting_reason"] == row.waiting_reason

    @pytest.mark.asyncio
    async def test_the_row_keeps_saying_reading_while_the_read_runs(self, ctx):
        """Mid-read the printer reads as Busy to the matcher; the item whose
        read it is must not say so."""
        await _set(ctx, "queue_rfid_reread_before_start", "true")
        item_id = await _add_item(ctx, printer_id=None, target_model="X1C", required_filament_types=self.NEEDS_PETG)
        scheduler = PrintScheduler()
        h = _Harness(ctx, scheduler, _printer_state(unread=(3,)))
        started = asyncio.Event()

        def refresh(ams_id, slot_id):
            started.set()
            return True, "Refreshing"

        h.client.ams_refresh_tray = MagicMock(side_effect=refresh)

        with h.patched(slot_timeout=30.0):
            await scheduler.check_queue()
            await started.wait()
            await scheduler.check_queue()
            assert (await _item(ctx, item_id)).waiting_reason == RFID_REREAD_HOLD
            assert h.dispatched() == []
            assert len(h.tasks) == 1
            h.tasks[0].cancel()
            with pytest.raises(asyncio.CancelledError):
                await h.tasks[0]
        assert 1 not in scheduler._rfid_rereads

    @pytest.mark.asyncio
    async def test_nothing_to_read_leaves_the_item_unstamped(self, ctx):
        """No read, no stamp: a printer that finishes its print later with an
        unidentified spool in it still gets its turn for this item."""
        await _set(ctx, "queue_rfid_reread_before_start", "true")
        item_id = await _add_item(ctx, printer_id=None, target_model="X1C", required_filament_types=self.NEEDS_PETG)

        h = await _Harness(ctx, PrintScheduler(), _printer_state(unread=())).run()

        h.client.ams_refresh_tray.assert_not_called()
        assert h.dispatched() == []
        row = await _item(ctx, item_id)
        assert "PETG" in row.waiting_reason
        assert row.rfid_precheck_at is None

    @pytest.mark.asyncio
    async def test_filament_loaded_on_the_turned_down_printer_means_no_read(self, ctx):
        await _set(ctx, "queue_rfid_reread_before_start", "true")
        item_id = await _add_item(ctx, printer_id=None, target_model="X1C", required_filament_types=self.NEEDS_PETG)

        h = await _Harness(ctx, PrintScheduler(), _printer_state(unread=(3,), tray_now=3)).run()

        h.client.ams_refresh_tray.assert_not_called()
        assert h.dispatched() == []
        assert (await _item(ctx, item_id)).rfid_precheck_at is None

    @pytest.mark.asyncio
    async def test_setting_off_changes_nothing_here(self, ctx):
        item_id = await _add_item(ctx, printer_id=None, target_model="X1C", required_filament_types=self.NEEDS_PETG)

        h = await _Harness(ctx, PrintScheduler(), _printer_state(unread=(3,))).run()

        h.client.ams_refresh_tray.assert_not_called()
        assert h.tasks == []
        assert "PETG" in (await _item(ctx, item_id)).waiting_reason

    @pytest.mark.asyncio
    async def test_every_turned_down_printer_of_the_model_is_read_at_once(self, ctx):
        """A farm: one round per item reads every idle printer of the model
        that was passed over, so the next pass sees the whole fleet."""
        await _set(ctx, "queue_rfid_reread_before_start", "true")
        async with ctx.session_maker() as db:
            db.add(
                Printer(
                    id=2,
                    name="X1C-02",
                    serial_number="X1C0002",
                    ip_address="10.0.0.2",
                    access_code="x",
                    model="X1C",
                    is_active=True,
                )
            )
            await db.commit()
        item_id = await _add_item(ctx, printer_id=None, target_model="X1C", required_filament_types=self.NEEDS_PETG)
        scheduler = PrintScheduler()
        states = {1: _printer_state(unread=(3,)), 2: _printer_state(unread=(0,))}
        seen: dict[int, tuple] = {}

        def refresh(ams_id, slot_id):
            seen[slot_id] = (scheduler._printer_in_dispatch_hold(1), scheduler._printer_in_dispatch_hold(2))
            return True, "Refreshing"

        h = _Harness(ctx, scheduler, states[1], states=states, refresh=MagicMock(side_effect=refresh))
        await h.run()

        assert sorted(h.client.ams_refresh_tray.call_args_list) == [((0, 0),), ((0, 3),)]
        assert seen == {3: (True, True), 0: (True, True)}
        assert len(h.tasks) == 2
        assert h.dispatched() == []
        assert (await _item(ctx, item_id)).waiting_reason == RFID_REREAD_HOLD
        assert scheduler._dispatch_holds == {}
        assert scheduler._rfid_rereads == {}


class TestTheHoldReasonIsSelfResolving:
    def test_it_counts_as_busy_for_notifications(self):
        assert PrintScheduler._is_busy_only(RFID_REREAD_HOLD) is True
        assert PrintScheduler._is_busy_only(f"Busy: X1C-01 | {RFID_REREAD_HOLD}") is True
        assert PrintScheduler._is_busy_only("Waiting for plate confirmation: X1C-01") is False


def _patches(h: _Harness):
    return [
        patch("backend.app.services.print_scheduler.async_session", h.ctx.session_maker),
        patch("backend.app.core.database.async_session", h.ctx.session_maker),
        patch("backend.app.services.print_scheduler.printer_manager.is_connected", MagicMock(return_value=True)),
        patch(
            "backend.app.services.print_scheduler.printer_manager.get_status",
            MagicMock(side_effect=lambda printer_id: h.states.get(printer_id, h.state)),
        ),
        patch("backend.app.services.print_scheduler.printer_manager.get_client", MagicMock(return_value=h.client)),
        patch(
            "backend.app.services.print_scheduler.printer_manager.is_awaiting_plate_clear",
            MagicMock(return_value=False),
        ),
        patch("backend.app.services.print_scheduler.ha_sensor_manager.blocked_printers", AsyncMock(return_value={})),
        patch(
            "backend.app.services.notification_service.notification_service.on_queue_job_waiting", h.waiting_notified
        ),
        patch("backend.app.services.notification_service.notification_service.on_queue_job_assigned", AsyncMock()),
        patch("backend.app.services.print_scheduler.spawn_background_task", h._spawn),
        patch("backend.app.api.routes.printers._apply_pa_after_refresh", h.pa_applied),
        patch("backend.app.services.print_scheduler._RFID_REREAD_POLL_INTERVAL", 0.01),
        patch.object(h.scheduler, "_is_printer_idle", MagicMock(return_value=True)),
        patch.object(h.scheduler, "_check_auto_drying", AsyncMock()),
        patch.object(h.scheduler, "_ensure_ams_mapping", h.ensure_mapping),
        patch.object(h.scheduler, "_block_on_filament_deficit", AsyncMock(return_value=False)),
        patch.object(h.scheduler, "_get_smart_plugs", AsyncMock(return_value=[])),
        patch.object(h.scheduler, "_launch_uploads", h.launched),
    ]
