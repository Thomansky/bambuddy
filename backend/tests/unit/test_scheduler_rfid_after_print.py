"""The AMS reads its unidentified spools when a print ends, not when a job needs them.

The pre-dispatch read (``queue_rfid_reread_before_start``) asks while a job is
looking for a printer, which on a working farm is never a good moment: it only
runs for a printer the matcher has already turned down *and* that counts as
idle, and a busy farm has neither. Measured over three days and fifteen queued
jobs on an eight-printer farm, it fired exactly zero times.

``ams_read_unidentified_after_print`` is the other end of the same idea. A
printer that has just finished -- completed or failed, both leave it idle -- is
free, the AMS can move filament, and nobody is waiting on the answer.

The contract these tests pin:

- off: nothing is asked, nothing is reserved;
- on: one round per finished print, reading the occupied slots the AMS cannot
  name, with the same rules, the same cap and the same per-slot memory as the
  pre-dispatch read;
- filament loaded stands the whole round down, because that is the rule the
  transport enforces: ``ams_refresh_tray`` refuses every slot on the printer
  while ``tray_now`` says anything is loaded. A printer that has finished
  normally has retracted and reports 255; one that has not is read after the
  next print that does;
- the second hotend of a dual-nozzle machine, which ``tray_now`` cannot
  describe, is dropped from the round by ``extruder_slots``;
- the plate-clear gate is NOT consulted: a finished plate nobody has released
  is exactly the window this exists for;
- a print that takes the printer back ends the round at once, and a slot it
  cut short is not remembered as unreadable;
- the printer is reserved for the round and handed back on every exit,
  including an exception;
- every round says what it did at info, once, including the rounds that read
  nothing -- the feature this replaces was believed broken for two evenings
  because its only evidence was at debug.
"""

import asyncio
import logging
import time
from contextlib import ExitStack
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy import update
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import backend.app.models  # noqa: F401 - populate Base.metadata
from backend.app.core.database import Base
from backend.app.models.print_queue import PrintQueueItem
from backend.app.models.printer import Printer
from backend.app.models.settings import Settings
from backend.app.services.bambu_mqtt import BambuMQTTClient
from backend.app.services.print_scheduler import _RFID_REREAD_HOLD_MARKER, PrintScheduler
from backend.tests.unit.test_scheduler_rfid_preread import _add_item, _Harness, _item, _printer_state, _tray

SETTING = "ams_read_unidentified_after_print"


@pytest.fixture
async def ctx():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    try:
        yield SimpleNamespace(session_maker=async_sessionmaker(engine, expire_on_commit=False))
    finally:
        await engine.dispose()


@pytest.fixture
async def qctx(ctx):
    """``ctx`` with the printer row a real scheduler pass needs to name."""
    async with ctx.session_maker() as db:
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
    return ctx


async def _enable(ctx, value="true"):
    async with ctx.session_maker() as db:
        db.add(Settings(key=SETTING, value=value))
        await db.commit()


def _finished(*, state="FINISH", tray_now=255, unread=(3,), exist="f", extruder_slots=None):
    """A printer that has just come off a print, with *unread* slots occupied."""
    report = _printer_state(tray_now=tray_now, unread=unread, exist=exist)
    report.state = state
    report.connected = True
    report.extruder_slots = extruder_slots or {}
    return report


def _transport(state) -> BambuMQTTClient:
    """A real MQTT client reporting *state*, with its socket mocked out.

    Real on purpose. ``ams_refresh_tray`` has a gate of its own and it is the
    one the round has to agree with; a stub that always accepts would let a
    round that cannot send a single command pass for one that reads.
    """
    client = BambuMQTTClient(ip_address="10.0.0.1", serial_number="X1C0001", access_code="x", model="X1C")
    client._client = MagicMock()
    client.state = state
    return client


class _Round:
    """One after-print read round against a fake printer."""

    def __init__(self, scheduler, state, *, refresh=None, connected=True, client=True):
        self.scheduler = scheduler
        self.state = state
        self.connected = connected
        self.transport = _transport(state) if client else None
        self.client = MagicMock() if client else None
        if self.client is not None:
            self.client.ams_refresh_tray = MagicMock(side_effect=self._refresh(refresh))
        self.tasks: list[asyncio.Task] = []
        self.pa_applied = AsyncMock()

    def _refresh(self, hook):
        """The real gate first; *hook* is what the printer does once it is past.

        Every ``refresh=`` a test passes describes an accepted command -- the
        tag arriving, a print starting, the session dying. None of them gets to
        decide whether the command was accepted in the first place.
        """

        def call(ams_id, slot_id):
            ok, message = self.transport.ams_refresh_tray(ams_id, slot_id)
            if ok and hook is not None:
                return hook(ams_id, slot_id)
            return ok, message

        return call

    def _spawn(self, coro, *, name=None):
        task = asyncio.ensure_future(coro)
        self.tasks.append(task)
        return task

    @property
    def refreshed(self):
        """The (ams_id, slot_id) pairs an ``ams_get_rfid`` went out for."""
        if self.client is None:
            return []
        return [c.args for c in self.client.ams_refresh_tray.call_args_list]

    def patched(self, ctx, *, slot_timeout=0.05):
        stack = ExitStack()
        for p in _patches(ctx, self, slot_timeout):
            stack.enter_context(p)
        return stack

    async def run(self, ctx, printer_id=1, *, slot_timeout=0.05):
        with self.patched(ctx, slot_timeout=slot_timeout):
            await self.scheduler.read_unidentified_slots_after_print(printer_id)
            for task in self.tasks:
                await task
        return self


def _patches(ctx, round_: _Round, slot_timeout: float):
    return [
        patch("backend.app.services.print_scheduler.async_session", ctx.session_maker),
        patch(
            "backend.app.services.print_scheduler.printer_manager.is_connected",
            MagicMock(return_value=round_.connected),
        ),
        patch(
            "backend.app.services.print_scheduler.printer_manager.get_status",
            MagicMock(return_value=round_.state),
        ),
        patch(
            "backend.app.services.print_scheduler.printer_manager.get_client",
            MagicMock(return_value=round_.client),
        ),
        # Consulted by nothing on this path, and that is the point: see
        # TestThePlateClearGate. True is the state of a farm nobody has walked
        # round yet.
        patch(
            "backend.app.services.print_scheduler.printer_manager.is_awaiting_plate_clear",
            MagicMock(return_value=True),
        ),
        patch("backend.app.services.print_scheduler.spawn_background_task", round_._spawn),
        patch("backend.app.api.routes.printers._apply_pa_after_refresh", round_.pa_applied),
        patch("backend.app.services.print_scheduler._RFID_REREAD_POLL_INTERVAL", 0.01),
        patch("backend.app.services.print_scheduler._RFID_REREAD_SLOT_TIMEOUT", slot_timeout),
    ]


def _lines(caplog):
    return [r.getMessage() for r in caplog.records if r.name == "backend.app.services.print_scheduler"]


class TestSettingOff:
    @pytest.mark.asyncio
    async def test_a_finished_print_asks_nothing(self, ctx):
        r = await _Round(PrintScheduler(), _finished()).run(ctx)

        assert r.refreshed == []
        assert r.scheduler._dispatch_holds == {}
        assert r.scheduler._rfid_rereads == {}

    @pytest.mark.asyncio
    async def test_a_stored_false_is_still_off(self, ctx):
        await _enable(ctx, "false")

        r = await _Round(PrintScheduler(), _finished()).run(ctx)

        assert r.refreshed == []


class TestAPrintThatEnded:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("state", ["FINISH", "FAILED", "IDLE"])
    async def test_the_unidentified_slots_are_read(self, ctx, state):
        """Failed prints count. They leave the machine just as idle, and the
        spool somebody pushed in during the print is just as unread."""
        await _enable(ctx)

        r = await _Round(PrintScheduler(), _finished(state=state, unread=(1, 3))).run(ctx)

        assert r.refreshed == [(0, 1), (0, 3)]

    @pytest.mark.asyncio
    async def test_a_completed_read_gets_the_k_profile_re_applied(self, ctx):
        await _enable(ctx)
        state = _finished(unread=(3,))

        def refresh(ams_id, slot_id):
            state.raw_data["ams"][0]["tray"][slot_id] = _tray(slot_id)
            return True, "Refreshing"

        r = await _Round(PrintScheduler(), state, refresh=MagicMock(side_effect=refresh)).run(ctx)

        r.pa_applied.assert_awaited_once_with(1, 0, 3)

    @pytest.mark.asyncio
    async def test_the_ceiling_is_the_same_eight_slots(self, ctx):
        await _enable(ctx)
        state = _finished(unread=(0, 1, 2, 3))
        state.raw_data["ams"] = [
            {"id": str(unit), "tray": [_tray(i, read=False) for i in range(4)]} for unit in range(3)
        ]
        state.tray_exist_bits = "fff"
        state.tray_read_done_bits = "0"

        r = await _Round(PrintScheduler(), state).run(ctx)

        assert len(r.refreshed) == 8


class TestTheLoadedTray:
    """The transport refuses per printer, not per slot, so a round stands down.

    ``BambuMQTTClient.ams_refresh_tray`` reads one value -- ``state.tray_now``
    -- and refuses every slot on the machine while it says anything is loaded.
    A round that dropped only the slot that value names and kept the rest would
    reserve the printer, hold the queue off it, gate auto-off, and then collect
    one refusal per slot: a feature that reads as working and identifies
    nothing. The printer this is for has retracted by the time it reports
    FINISH; the ones that have not are read after the next print that does.
    """

    @pytest.mark.asyncio
    @pytest.mark.parametrize("tray_now", [0, 1, 5, 128, 254])
    async def test_a_round_stands_down_while_anything_is_loaded(self, ctx, tray_now):
        """128 is an AMS-HT dry box and 254 the external spool. Neither is an
        ``ams_id * 4 + slot_id`` tray, so neither can be dropped slot-wise --
        and the transport refuses on both just the same."""
        await _enable(ctx)
        scheduler = PrintScheduler()

        r = await _Round(scheduler, _finished(tray_now=tray_now, unread=(1, 3))).run(ctx)

        assert r.refreshed == []
        assert scheduler._dispatch_holds == {}
        assert scheduler._rfid_rereads == {}

    @pytest.mark.asyncio
    async def test_the_stand_down_says_how_much_it_did_not_read(self, ctx, caplog):
        await _enable(ctx)

        with caplog.at_level(logging.INFO, logger="backend.app.services.print_scheduler"):
            await _Round(PrintScheduler(), _finished(tray_now=1, unread=(1, 3))).run(ctx)

        assert any("2 unread slot(s), skipped: filament loaded" in line for line in _lines(caplog))

    @pytest.mark.parametrize("tray_now", [0, 1, 5, 128, 254])
    def test_the_stand_down_is_the_rule_the_transport_itself_enforces(self, tray_now):
        """Pinned against the real client. The round stands down because
        ``ams_refresh_tray`` refuses for the whole printer; if that ever became
        a per-slot gate, this is the test that says the round may read the
        slots the loaded one does not name."""
        transport = _transport(_finished(tray_now=tray_now))

        for ams_id, slot_id in ((0, 0), (0, 3), (1, 2)):
            ok, message = transport.ams_refresh_tray(ams_id, slot_id)
            assert not ok
            assert "unload filament first" in message
        assert transport._client.publish.call_count == 0

    def test_nothing_loaded_is_what_lets_a_round_read_at_all(self):
        transport = _transport(_finished(tray_now=255))

        ok, _message = transport.ams_refresh_tray(0, 3)

        assert ok
        assert transport._client.publish.call_count == 1


class TestTheSecondHotend:
    """What ``tray_now`` cannot say, and ``extruder_slots`` can.

    One global tray id names one slot for the whole printer, so on a dual-nozzle
    machine holding two spools it can only ever cover one of them. The other is
    dropped here, because the transport's gate will not catch it.
    """

    @pytest.mark.asyncio
    async def test_the_slot_tray_now_cannot_name_is_left_out(self, ctx):
        await _enable(ctx)
        state = _finished(
            unread=(1, 2, 3),
            extruder_slots={
                0: SimpleNamespace(ams_id=0, slot_id=1, has_filament=True),
                1: SimpleNamespace(ams_id=0, slot_id=2, has_filament=True),
            },
        )

        r = await _Round(PrintScheduler(), state).run(ctx)

        assert r.refreshed == [(0, 3)]

    @pytest.mark.asyncio
    async def test_an_extruder_holding_nothing_does_not_hide_its_slot(self, ctx):
        await _enable(ctx)
        state = _finished(
            unread=(3,),
            extruder_slots={0: SimpleNamespace(ams_id=0, slot_id=3, has_filament=False)},
        )

        r = await _Round(PrintScheduler(), state).run(ctx)

        assert r.refreshed == [(0, 3)]

    @pytest.mark.asyncio
    async def test_a_round_with_nothing_left_to_read_says_which_slots_were_loaded(self, ctx, caplog):
        await _enable(ctx)
        state = _finished(
            unread=(3,),
            extruder_slots={0: SimpleNamespace(ams_id=0, slot_id=3, has_filament=True)},
        )

        with caplog.at_level(logging.INFO, logger="backend.app.services.print_scheduler"):
            r = await _Round(PrintScheduler(), state).run(ctx)

        assert r.refreshed == []
        assert any("0 unread slot(s) (1 skipped: loaded (AMS0-T3))" in line for line in _lines(caplog))


class TestThePlateClearGate:
    @pytest.mark.asyncio
    async def test_it_does_not_stand_the_round_down(self, ctx):
        """Reading moves filament inside the AMS; it does not print. The
        confirmation gate is about starting a print, and on the farm this was
        written for every printer was sitting behind one -- which is exactly
        why the pre-dispatch read never ran. ``is_awaiting_plate_clear`` is
        True for every test in this file.
        """
        await _enable(ctx)

        r = await _Round(PrintScheduler(), _finished()).run(ctx)

        assert r.refreshed == [(0, 3)]


class TestANewPrintWins:
    @pytest.mark.asyncio
    async def test_a_printer_already_printing_again_is_not_touched(self, ctx, caplog):
        await _enable(ctx)

        with caplog.at_level(logging.INFO, logger="backend.app.services.print_scheduler"):
            r = await _Round(PrintScheduler(), _finished(state="RUNNING")).run(ctx)

        assert r.refreshed == []
        assert r.scheduler._dispatch_holds == {}
        assert any("already printing again, nothing read" in line for line in _lines(caplog))

    @pytest.mark.asyncio
    async def test_a_print_that_starts_mid_round_ends_it(self, ctx, caplog):
        await _enable(ctx)
        state = _finished(unread=(1, 3))

        def refresh(ams_id, slot_id):
            state.state = "RUNNING"
            return True, "Refreshing"

        with caplog.at_level(logging.INFO, logger="backend.app.services.print_scheduler"):
            r = await _Round(PrintScheduler(), state, refresh=MagicMock(side_effect=refresh)).run(
                ctx, slot_timeout=30.0
            )

        # The slot whose command was already out is not re-sent, and the one
        # after it is never attempted.
        assert r.refreshed == [(0, 1)]
        assert r.scheduler._dispatch_holds == {}
        assert any("printing again (state=RUNNING), not reading AMS0-T3" in line for line in _lines(caplog))

    @pytest.mark.asyncio
    async def test_a_slot_the_print_cut_short_is_not_remembered_as_unreadable(self, ctx):
        """It never got its attempt. Holding it against the next round would
        silence exactly the spool this feature exists to identify."""
        await _enable(ctx)
        state = _finished(unread=(3,))
        state.tray_read_done_bits = "f"

        def refresh(ams_id, slot_id):
            state.state = "RUNNING"
            return True, "Refreshing"

        r = await _Round(PrintScheduler(), state, refresh=MagicMock(side_effect=refresh)).run(ctx, slot_timeout=30.0)

        assert r.scheduler._rfid_unreadable == {}

    @pytest.mark.asyncio
    async def test_a_printer_reserved_by_the_queue_is_left_alone(self, ctx, caplog):
        await _enable(ctx)
        scheduler = PrintScheduler()
        scheduler._mark_printer_dispatched(1, "FINISH", None)

        with caplog.at_level(logging.INFO, logger="backend.app.services.print_scheduler"):
            r = await _Round(scheduler, _finished()).run(ctx)

        assert r.refreshed == []
        assert any("printer reserved elsewhere, nothing read" in line for line in _lines(caplog))

    @pytest.mark.asyncio
    async def test_an_upload_still_in_flight_is_a_reservation_too(self, ctx, caplog):
        """The dispatch hold is taken only once the print command has gone out.
        For the whole upload before it -- a large 3MF over FTP is the better
        part of a minute -- the queue's claim on the printer is ``_inflight``,
        and the printer still reports FINISH. A round that asked about the hold
        alone would start moving the AMS into the beginning of a print."""
        await _enable(ctx)
        scheduler = PrintScheduler()
        scheduler._inflight[7] = (MagicMock(), 1)

        with caplog.at_level(logging.INFO, logger="backend.app.services.print_scheduler"):
            r = await _Round(scheduler, _finished()).run(ctx)

        assert r.refreshed == []
        assert scheduler._dispatch_holds == {}
        assert any("printer reserved elsewhere, nothing read" in line for line in _lines(caplog))

    @pytest.mark.asyncio
    async def test_a_dispatch_that_starts_while_the_round_decides_still_wins(self, ctx, caplog):
        """Reading the reservations awaits the database, and a dispatch tick is
        synchronous: one can register itself in that gap. The claims that need
        no database read are asked again with nothing awaited since, which is
        the last moment a round can still stand down."""
        await _enable(ctx)
        scheduler = PrintScheduler()
        scheduler._queue_reserved_printers = AsyncMock(return_value=set())
        scheduler._inflight[7] = (MagicMock(), 1)

        with caplog.at_level(logging.INFO, logger="backend.app.services.print_scheduler"):
            r = await _Round(scheduler, _finished()).run(ctx)

        assert r.refreshed == []
        assert scheduler._dispatch_holds == {}
        assert any("printer taken while deciding, nothing read" in line for line in _lines(caplog))

    @pytest.mark.asyncio
    async def test_an_item_already_printing_on_it_is_a_reservation_too(self, ctx):
        """The third of the three the queue counts: the row says the job is on
        this printer even in the seconds before its state catches up."""
        await _enable(ctx)
        item_id = await _add_item(ctx)
        async with ctx.session_maker() as db:
            await db.execute(update(PrintQueueItem).where(PrintQueueItem.id == item_id).values(status="printing"))
            await db.commit()

        r = await _Round(PrintScheduler(), _finished()).run(ctx)

        assert r.refreshed == []


class TestTheReservation:
    @pytest.mark.asyncio
    async def test_the_printer_is_held_while_it_reads_and_handed_back_after(self, ctx):
        await _enable(ctx)
        scheduler = PrintScheduler()
        seen: dict[str, object] = {}

        def refresh(ams_id, slot_id):
            seen.setdefault("held", scheduler._printer_in_dispatch_hold(1))
            # None, not an item id: nothing is queued behind this round.
            seen.setdefault("attributed", 1 in scheduler._rfid_rereads and scheduler._rfid_rereads[1])
            return True, "Refreshing"

        await _Round(scheduler, _finished(), refresh=MagicMock(side_effect=refresh)).run(ctx)

        assert seen == {"held": True, "attributed": None}
        assert scheduler._dispatch_holds == {}
        assert scheduler._rfid_rereads == {}

    @pytest.mark.asyncio
    async def test_an_exception_still_hands_the_printer_back(self, ctx):
        await _enable(ctx)
        scheduler = PrintScheduler()
        refresh = MagicMock(side_effect=RuntimeError("MQTT session gone"))

        await _Round(scheduler, _finished(), refresh=refresh).run(ctx)

        assert scheduler._dispatch_holds == {}
        assert scheduler._rfid_rereads == {}

    @pytest.mark.asyncio
    async def test_a_cancelled_round_still_hands_the_printer_back(self, ctx):
        await _enable(ctx)
        scheduler = PrintScheduler()
        r = _Round(scheduler, _finished())

        with r.patched(ctx, slot_timeout=30.0):
            task = asyncio.ensure_future(scheduler.read_unidentified_slots_after_print(1))
            for _ in range(500):
                if scheduler._dispatch_holds:
                    break
                await asyncio.sleep(0.002)
            assert scheduler._dispatch_holds, "the round never reserved the printer"
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task

        assert scheduler._dispatch_holds == {}
        assert scheduler._rfid_rereads == {}

    @pytest.mark.asyncio
    async def test_the_queue_stays_off_a_printer_that_is_reading(self, qctx):
        """The reservation is the same one the pre-dispatch read takes, so
        everything that honours that hold stays off the printer -- dispatching
        into a moving AMS is what it is shared to prevent. The item is not the
        one the round belongs to, because no item ever is."""
        item_id = await _add_item(qctx)
        scheduler = PrintScheduler()
        scheduler._dispatch_holds[1] = (time.monotonic(), _RFID_REREAD_HOLD_MARKER, None)
        scheduler._rfid_rereads[1] = None

        h = await _Harness(qctx, scheduler, _printer_state()).run()

        assert h.dispatched() == []
        assert (await _item(qctx, item_id)).waiting_reason == "Busy: X1C-01"


class TestTheSlotNothingCanRead:
    @staticmethod
    def _nameless_but_done():
        state = _finished(unread=(3,))
        state.tray_read_done_bits = "f"
        return state

    @pytest.mark.asyncio
    async def test_a_slot_that_stays_nameless_is_not_asked_after_the_next_print(self, ctx, caplog):
        await _enable(ctx)
        scheduler = PrintScheduler()
        state = self._nameless_but_done()

        first = await _Round(scheduler, state).run(ctx)
        assert first.refreshed == [(0, 3)]
        assert scheduler._rfid_unreadable == {1: {(0, 3)}}

        with caplog.at_level(logging.INFO, logger="backend.app.services.print_scheduler"):
            second = await _Round(scheduler, state).run(ctx)

        assert second.refreshed == []
        assert any("0 unread slot(s) (1 skipped: already tried)" in line for line in _lines(caplog))

    @pytest.mark.asyncio
    async def test_a_slot_firmware_itself_calls_unread_is_asked_every_time(self, ctx):
        await _enable(ctx)
        scheduler = PrintScheduler()
        state = self._nameless_but_done()

        await _Round(scheduler, state).run(ctx)
        assert scheduler._rfid_unreadable == {1: {(0, 3)}}

        # A Bambu spool swapped in for the unreadable one: still nameless,
        # but firmware now says it has not read this slot.
        state.tray_read_done_bits = "7"
        second = await _Round(scheduler, state).run(ctx)

        assert second.refreshed == [(0, 3)]


class TestSayWhatHappened:
    @pytest.mark.asyncio
    async def test_the_line_names_the_printer_the_slots_and_the_rule(self, ctx, caplog):
        await _enable(ctx)

        with caplog.at_level(logging.INFO, logger="backend.app.services.print_scheduler"):
            await _Round(PrintScheduler(), _finished(unread=(1, 3))).run(ctx)

        chosen = [line for line in _lines(caplog) if "unread slot(s)" in line]
        assert chosen == [
            "RFID pre-read [printer 1, after print]: setting=on "
            "tray_now=255 exist=f read_done=5 reading=- signals=masks "
            "-> 2 unread slot(s): AMS0-T1 (read-done bit clear), AMS0-T3 (read-done bit clear) "
            "-> holding the printer while it reads"
        ]

    @pytest.mark.asyncio
    async def test_a_round_that_reads_nothing_still_says_so(self, ctx, caplog):
        await _enable(ctx)

        with caplog.at_level(logging.INFO, logger="backend.app.services.print_scheduler"):
            r = await _Round(PrintScheduler(), _finished(unread=())).run(ctx)

        assert r.refreshed == []
        assert [line for line in _lines(caplog) if "unread slot(s)" in line] == [
            "RFID pre-read [printer 1, after print]: setting=on "
            "tray_now=255 exist=f read_done=f reading=- signals=masks -> 0 unread slot(s)"
        ]

    @pytest.mark.asyncio
    async def test_the_same_verdict_is_repeated_for_every_print(self, ctx, caplog):
        """The pre-dispatch read deduplicates because it re-evaluates every 30
        s. A round runs once per finished print, and each one is its own
        event: silence after the first is how this feature reads as broken."""
        await _enable(ctx)
        scheduler = PrintScheduler()

        with caplog.at_level(logging.INFO, logger="backend.app.services.print_scheduler"):
            await _Round(scheduler, _finished(unread=())).run(ctx)
            await _Round(scheduler, _finished(unread=())).run(ctx)

        assert len([line for line in _lines(caplog) if "0 unread slot(s)" in line]) == 2

    @pytest.mark.asyncio
    async def test_a_printer_that_is_not_reporting_says_so_at_info(self, ctx, caplog):
        """The path the pre-dispatch read leaves at debug. A round has to
        leave a trace whatever it found, including nothing to work with."""
        await _enable(ctx)

        with caplog.at_level(logging.INFO, logger="backend.app.services.print_scheduler"):
            r = await _Round(PrintScheduler(), _finished(), connected=False).run(ctx)

        assert r.refreshed == []
        assert any("printer not reporting, nothing read" in line for line in _lines(caplog))

    @pytest.mark.asyncio
    async def test_a_printer_without_a_client_says_so_too(self, ctx, caplog):
        await _enable(ctx)

        with caplog.at_level(logging.INFO, logger="backend.app.services.print_scheduler"):
            r = await _Round(PrintScheduler(), _finished(), client=False).run(ctx)

        assert r.refreshed == []
        assert any("printer not reporting, nothing read" in line for line in _lines(caplog))

    @pytest.mark.asyncio
    async def test_the_closing_line_does_not_promise_a_dispatch(self, ctx, caplog):
        await _enable(ctx)

        with caplog.at_level(logging.INFO, logger="backend.app.services.print_scheduler"):
            await _Round(PrintScheduler(), _finished()).run(ctx)

        finished = [line for line in _lines(caplog) if ": finished —" in line]
        assert finished == [
            "RFID pre-read [printer 1, after print]: finished — read 0, refused 0, no read 1, "
            "not attempted 0; the printer is back with the queue"
        ]
