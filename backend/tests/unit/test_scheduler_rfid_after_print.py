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
  next print that does -- unless ``ams_unload_before_after_print_read`` says to
  retract it first, which is what ``TestUnloadBeforeReading`` covers;
- nothing loaded means no unload command is sent at all, ever. That is the
  owner's own requirement and the common case on his machines: an unload sent
  to a hotend that has already retracted is the defect, not a detail;
- the second hotend of a dual-nozzle machine, which ``tray_now`` cannot
  describe, is found through ``extruder_slots`` -- it stands the round down
  like any other loaded hotend, and gets its own addressed unload when the
  round is allowed to retract;
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
UNLOAD_SETTING = "ams_unload_before_after_print_read"


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


async def _enable(ctx, value="true", *, unload=None):
    async with ctx.session_maker() as db:
        db.add(Settings(key=SETTING, value=value))
        if unload is not None:
            db.add(Settings(key=UNLOAD_SETTING, value=unload))
        await db.commit()


def _finished(*, state="FINISH", tray_now=255, unread=(3,), exist="f", extruder_slots=None):
    """A printer that has just come off a print, with *unread* slots occupied."""
    report = _printer_state(tray_now=tray_now, unread=unread, exist=exist)
    report.state = state
    report.connected = True
    report.extruder_slots = extruder_slots or {}
    return report


def _hotend(ams_id, slot_id, *, loaded=True):
    """One ``extruder_slots`` entry, in the shape ``_parse_extruder_slots`` builds."""
    return SimpleNamespace(ams_id=ams_id, slot_id=slot_id, has_filament=loaded)


def _global_tray(slot):
    """The id ``ams_unload_filament`` addresses a slot by, as the AMS UI computes it."""
    if slot.ams_id is None or slot.slot_id is None:
        return None
    return slot.ams_id * 4 + slot.slot_id


def _retract(state, tray_id=None):
    """Make *state* report what a printer looks like once an unload has landed.

    An addressed unload frees the hotend fed from that slot; the unaddressed
    one frees everything, which on a printer without a per-extruder block is
    the single hotend ``tray_now`` names. ``tray_now`` then reports whatever is
    left, or 255 once nothing is.
    """
    slots = state.extruder_slots or {}
    for slot in slots.values():
        if tray_id is None or _global_tray(slot) == tray_id:
            slot.has_filament = False
    still_loaded = [slot for slot in slots.values() if slot.has_filament]
    if not still_loaded:
        state.tray_now = 255
        return
    remaining = _global_tray(still_loaded[0])
    state.tray_now = 254 if remaining is None else remaining


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

    def __init__(self, scheduler, state, *, refresh=None, unload=None, connected=True, client=True):
        self.scheduler = scheduler
        self.state = state
        self.connected = connected
        self.transport = _transport(state) if client else None
        self.client = MagicMock() if client else None
        if self.client is not None:
            self.client.ams_refresh_tray = MagicMock(side_effect=self._refresh(refresh))
            # Default: the printer accepts the unload and retracts at once.
            self.client.ams_unload_filament = MagicMock(side_effect=unload or self._retract)
        self.tasks: list[asyncio.Task] = []
        self.pa_applied = AsyncMock()

    def _retract(self, tray_id=None):
        """What a printer that obeys an unload reports afterwards."""
        _retract(self.state, tray_id)
        return True

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

    @property
    def unloaded(self):
        """The tray ids an ``ams_change_filament`` unload went out for, in order.

        None is the unaddressed command, which unloads whatever ``tray_now``
        names -- the only form a printer that reports no per-extruder block
        understands.
        """
        if self.client is None:
            return []
        return [c.kwargs.get("tray_id") for c in self.client.ams_unload_filament.call_args_list]

    def patched(self, ctx, *, slot_timeout=0.05, unload_timeout=0.05):
        stack = ExitStack()
        for p in _patches(ctx, self, slot_timeout, unload_timeout):
            stack.enter_context(p)
        return stack

    async def run(self, ctx, printer_id=1, *, slot_timeout=0.05, unload_timeout=0.05):
        with self.patched(ctx, slot_timeout=slot_timeout, unload_timeout=unload_timeout):
            await self.scheduler.read_unidentified_slots_after_print(printer_id)
            for task in self.tasks:
                await task
        return self


def _patches(ctx, round_: _Round, slot_timeout: float, unload_timeout: float = 0.05):
    return [
        patch("backend.app.services.print_scheduler._RFID_UNLOAD_TIMEOUT", unload_timeout),
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
        assert r.unloaded == []
        assert scheduler._dispatch_holds == {}
        assert scheduler._rfid_rereads == {}

    @pytest.mark.asyncio
    async def test_the_stand_down_names_what_is_in_the_way(self, ctx, caplog):
        """A line that says "loaded" and nothing else reads as the feature
        being dead. It names the hotend and the setting that would fix it."""
        await _enable(ctx)

        with caplog.at_level(logging.INFO, logger="backend.app.services.print_scheduler"):
            await _Round(PrintScheduler(), _finished(tray_now=1, unread=(1, 3))).run(ctx)

        assert any(
            "filament loaded (tray 1), unload before reading is off, nothing read" in line for line in _lines(caplog)
        )

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
    machine holding two spools it can only ever cover one of them -- and a
    ``tray_now`` of 255 there does not mean the machine is empty. The round
    therefore asks both signals, and a hotend only ``extruder_slots`` knows
    about stands it down exactly as a loaded ``tray_now`` does.
    """

    @pytest.mark.asyncio
    async def test_a_hotend_only_the_extruder_block_knows_about_stands_the_round_down(self, ctx, caplog):
        await _enable(ctx)
        state = _finished(unread=(1, 2, 3), extruder_slots={0: _hotend(0, 1), 1: _hotend(0, 2)})

        with caplog.at_level(logging.INFO, logger="backend.app.services.print_scheduler"):
            r = await _Round(PrintScheduler(), state).run(ctx)

        assert r.refreshed == []
        assert r.unloaded == []
        assert any("filament loaded (AMS0-T1, AMS0-T2)" in line for line in _lines(caplog))

    @pytest.mark.asyncio
    async def test_an_extruder_holding_nothing_does_not_hide_its_slot(self, ctx):
        await _enable(ctx)
        state = _finished(unread=(3,), extruder_slots={0: _hotend(0, 3, loaded=False)})

        r = await _Round(PrintScheduler(), state).run(ctx)

        assert r.refreshed == [(0, 3)]

    def test_the_pre_dispatch_read_still_only_drops_the_slot(self, ctx):
        """The two rounds part company here on purpose. The pre-dispatch read
        has a job waiting on it and cannot unload anything, so it keeps its
        per-slot drop; the after-print round stands down instead, because it
        can be told to retract and then read everything."""
        state = _finished(unread=(1, 3), extruder_slots={0: _hotend(0, 1)})

        with ExitStack() as stack:
            for p in _patches(ctx, _Round(PrintScheduler(), state), 0.05):
                stack.enter_context(p)
            slots = PrintScheduler()._slots_to_reread(1, item_id=7)

        assert slots == [(0, 3)]


class TestUnloadBeforeReading:
    """``ams_unload_before_after_print_read``: the round may retract first.

    The AMS has to move filament to reach a tag, so a printer that still holds
    any is refused every slot -- the second reason the read never fired on a
    busy farm, after the idle gate. With this on the round retracts into the
    AMS first and reads afterwards; with it off it stands down and says so.

    The rule that outranks everything else here is the owner's: do not trigger
    an unload twice when the material is already unloaded. His H2S retracts by
    itself when a print ends, so the ordinary case is that there is nothing to
    unload at all -- and a command sent to an empty hotend, or a second one
    sent to a slot already freed, is the defect this shape exists to prevent,
    not a detail. Every path therefore re-reads the printer's own report
    immediately before it sends anything.
    """

    @pytest.mark.asyncio
    async def test_nothing_loaded_sends_no_unload_at_all(self, ctx):
        """The owner's rule and his common case. 255 with no hotend holding a
        slot is a printer that has already retracted; it is read exactly as it
        was before this setting existed and never sent a command."""
        await _enable(ctx, unload="true")

        r = await _Round(PrintScheduler(), _finished()).run(ctx)

        r.client.ams_unload_filament.assert_not_called()
        assert r.refreshed == [(0, 3)]

    @pytest.mark.asyncio
    async def test_a_hotend_block_reporting_nothing_loaded_is_not_something_loaded(self, ctx):
        """``has_filament`` false on every extruder is the same "nothing
        loaded" as no block at all: still no unload, still a straight read."""
        await _enable(ctx, unload="true")
        state = _finished(extruder_slots={0: _hotend(0, 1, loaded=False), 1: _hotend(None, None, loaded=False)})

        r = await _Round(PrintScheduler(), state).run(ctx)

        r.client.ams_unload_filament.assert_not_called()
        assert r.refreshed == [(0, 3)]

    @pytest.mark.asyncio
    async def test_loaded_with_the_setting_off_neither_unloads_nor_reads(self, ctx, caplog):
        await _enable(ctx)
        scheduler = PrintScheduler()

        with caplog.at_level(logging.INFO, logger="backend.app.services.print_scheduler"):
            r = await _Round(scheduler, _finished(tray_now=1, unread=(1, 3))).run(ctx)

        assert r.unloaded == []
        assert r.refreshed == []
        assert scheduler._dispatch_holds == {}
        assert any(
            "filament loaded (tray 1), unload before reading is off, nothing read" in line for line in _lines(caplog)
        )

    @pytest.mark.asyncio
    async def test_a_stored_false_is_still_off(self, ctx):
        await _enable(ctx, unload="false")

        r = await _Round(PrintScheduler(), _finished(tray_now=1)).run(ctx)

        assert r.unloaded == []
        assert r.refreshed == []

    @pytest.mark.asyncio
    async def test_it_does_nothing_while_the_after_print_read_itself_is_off(self, ctx):
        """It only qualifies the other setting. On its own it is not a licence
        to move filament on a machine nobody asked us to read."""
        await _enable(ctx, "false", unload="true")

        r = await _Round(PrintScheduler(), _finished(tray_now=1)).run(ctx)

        assert r.unloaded == []
        assert r.refreshed == []

    @pytest.mark.asyncio
    async def test_loaded_with_the_setting_on_unloads_then_reads(self, ctx):
        await _enable(ctx, unload="true")
        state = _finished(tray_now=1, unread=(1, 3))
        order: list[str] = []

        def unload(tray_id=None):
            order.append(f"unload {tray_id}")
            _retract(state, tray_id)
            return True

        def refresh(ams_id, slot_id):
            order.append(f"read AMS{ams_id}-T{slot_id}")
            return True, "Refreshing"

        await _Round(
            PrintScheduler(),
            state,
            refresh=MagicMock(side_effect=refresh),
            unload=MagicMock(side_effect=unload),
        ).run(ctx)

        assert order == ["unload None", "read AMS0-T1", "read AMS0-T3"]

    @pytest.mark.asyncio
    async def test_a_printer_with_no_extruder_block_takes_the_unaddressed_unload(self, ctx):
        """The single-nozzle shape. ``tray_now`` already names the one loaded
        slot exactly, and the unaddressed command -- which reads it itself --
        is the only form those machines are known to accept."""
        await _enable(ctx, unload="true")

        r = await _Round(PrintScheduler(), _finished(tray_now=5)).run(ctx)

        assert r.unloaded == [None]

    @pytest.mark.asyncio
    async def test_a_hotend_fed_from_a_unit_the_command_cannot_address_falls_back(self, ctx):
        """An AMS-HT dry box shares its unit id (128-135) and does not divide
        by four, so there is no ``ams_id * 4 + slot`` for it: ``ams_unload_filament``
        would decode 128 as unit 32 and send the command at nothing. It takes
        the unaddressed form, which the printer resolves itself."""
        await _enable(ctx, unload="true")
        state = _finished(tray_now=128, extruder_slots={0: _hotend(128, 0)})

        r = await _Round(PrintScheduler(), state).run(ctx)

        assert r.unloaded == [None]

    @pytest.mark.asyncio
    async def test_both_hotends_get_one_addressed_unload_each(self, ctx):
        """``tray_now`` is one value for the whole printer, so it names one of
        these two. An unaddressed unload would retract that one and leave the
        other loaded -- and the read would still be refused."""
        await _enable(ctx, unload="true")
        state = _finished(tray_now=1, unread=(0, 3), extruder_slots={0: _hotend(0, 1), 1: _hotend(0, 2)})

        r = await _Round(PrintScheduler(), state).run(ctx)

        assert r.unloaded == [1, 2]
        assert r.refreshed == [(0, 0), (0, 3)]

    @pytest.mark.asyncio
    async def test_a_hotend_that_came_free_on_its_own_gets_no_command(self, ctx, caplog):
        """The owner's rule at its sharpest: this printer retracts both hotends
        on the first command, so the second one's unload is never sent."""
        await _enable(ctx, unload="true")
        state = _finished(tray_now=1, unread=(3,), extruder_slots={0: _hotend(0, 1), 1: _hotend(0, 2)})

        def unload(tray_id=None):
            _retract(state, None)
            return True

        with caplog.at_level(logging.INFO, logger="backend.app.services.print_scheduler"):
            r = await _Round(PrintScheduler(), state, unload=MagicMock(side_effect=unload)).run(ctx)

        assert r.unloaded == [1]
        assert any("AMS0-T2 came free on its own, no unload sent" in line for line in _lines(caplog))
        assert r.refreshed == [(0, 3)]

    @pytest.mark.asyncio
    async def test_a_hotend_that_never_reports_free_is_not_re_sent(self, ctx, caplog):
        """One unload the printer swallowed is a machine for somebody to look
        at, not one to send a second at. The round gives up and lets go."""
        await _enable(ctx, unload="true")
        scheduler = PrintScheduler()

        with caplog.at_level(logging.INFO, logger="backend.app.services.print_scheduler"):
            r = await _Round(scheduler, _finished(tray_now=1), unload=MagicMock(return_value=True)).run(ctx)

        assert r.unloaded == [None]
        assert r.refreshed == []
        assert scheduler._dispatch_holds == {}
        assert scheduler._rfid_rereads == {}
        assert any("not re-sent, giving up" in line for line in _lines(caplog))

    @pytest.mark.asyncio
    async def test_an_unload_the_printer_refuses_ends_the_round(self, ctx, caplog):
        await _enable(ctx, unload="true")
        scheduler = PrintScheduler()

        with caplog.at_level(logging.INFO, logger="backend.app.services.print_scheduler"):
            r = await _Round(scheduler, _finished(tray_now=1), unload=MagicMock(return_value=False)).run(ctx)

        assert r.unloaded == [None]
        assert r.refreshed == []
        assert scheduler._dispatch_holds == {}
        assert any("unload refused by the printer, giving up" in line for line in _lines(caplog))

    @pytest.mark.asyncio
    async def test_a_print_starting_during_the_unload_wait_ends_the_round(self, ctx, caplog):
        await _enable(ctx, unload="true")
        scheduler = PrintScheduler()
        state = _finished(tray_now=1)

        def unload(tray_id=None):
            state.state = "RUNNING"
            return True

        with caplog.at_level(logging.INFO, logger="backend.app.services.print_scheduler"):
            r = await _Round(scheduler, state, unload=MagicMock(side_effect=unload)).run(ctx, unload_timeout=30.0)

        assert r.unloaded == [None]
        assert r.refreshed == []
        assert scheduler._dispatch_holds == {}
        assert any("printing again, tray 1 left loaded, nothing read" in line for line in _lines(caplog))

    @pytest.mark.asyncio
    async def test_a_print_that_starts_between_two_unloads_stops_the_second(self, ctx, caplog):
        await _enable(ctx, unload="true")
        scheduler = PrintScheduler()
        state = _finished(tray_now=1, extruder_slots={0: _hotend(0, 1), 1: _hotend(0, 2)})

        def unload(tray_id=None):
            _retract(state, tray_id)
            state.state = "RUNNING"
            return True

        with caplog.at_level(logging.INFO, logger="backend.app.services.print_scheduler"):
            r = await _Round(scheduler, state, unload=MagicMock(side_effect=unload)).run(ctx, unload_timeout=30.0)

        assert r.unloaded == [1]
        assert r.refreshed == []
        assert scheduler._dispatch_holds == {}
        assert any("printing again (state=RUNNING), AMS0-T2 not unloaded" in line for line in _lines(caplog))

    @pytest.mark.asyncio
    async def test_the_hold_covers_the_unload_and_is_released_after_it(self, ctx):
        """Auto-off asks ``rfid_read_in_flight`` before it switches a plug, and
        an unload takes longer than a tag read -- power cut mid-retraction
        strands the filament in the tube."""
        await _enable(ctx, unload="true")
        scheduler = PrintScheduler()
        state = _finished(tray_now=1)
        seen: dict[str, object] = {}

        def unload(tray_id=None):
            seen.setdefault("in_flight", scheduler.rfid_read_in_flight(1))
            _retract(state, tray_id)
            return True

        await _Round(scheduler, state, unload=MagicMock(side_effect=unload)).run(ctx)

        assert seen == {"in_flight": True}
        assert scheduler.rfid_read_in_flight(1) is False
        assert scheduler._dispatch_holds == {}
        assert scheduler._rfid_rereads == {}

    @pytest.mark.asyncio
    async def test_an_exception_on_the_unload_path_still_hands_the_printer_back(self, ctx):
        await _enable(ctx, unload="true")
        scheduler = PrintScheduler()

        r = await _Round(
            scheduler, _finished(tray_now=1), unload=MagicMock(side_effect=RuntimeError("MQTT session gone"))
        ).run(ctx)

        assert r.refreshed == []
        assert scheduler._dispatch_holds == {}
        assert scheduler._rfid_rereads == {}

    @pytest.mark.asyncio
    async def test_nothing_is_loaded_back_afterwards(self, ctx):
        """Left unloaded on purpose: the next print loads what it needs, and a
        reload here would spend a second purge for nothing."""
        await _enable(ctx, unload="true")
        state = _finished(tray_now=1)

        r = await _Round(PrintScheduler(), state).run(ctx)

        r.client.ams_load_filament.assert_not_called()
        assert state.tray_now == 255

    @pytest.mark.asyncio
    async def test_the_round_says_what_it_unloaded_before_it_read(self, ctx, caplog):
        await _enable(ctx, unload="true")
        state = _finished(tray_now=1, unread=(3,), extruder_slots={0: _hotend(0, 1), 1: _hotend(0, 2)})

        with caplog.at_level(logging.INFO, logger="backend.app.services.print_scheduler"):
            await _Round(PrintScheduler(), state).run(ctx)

        lines = _lines(caplog)
        assert any(
            "filament loaded (AMS0-T1, AMS0-T2) -> unloading 2 hotend(s) before reading" in line for line in lines
        )
        assert any("unloaded 2 hotend(s), reading now" in line for line in lines)

    @pytest.mark.asyncio
    async def test_a_round_that_unloads_and_finds_nothing_to_read_says_so(self, ctx, caplog):
        await _enable(ctx, unload="true")

        with caplog.at_level(logging.INFO, logger="backend.app.services.print_scheduler"):
            r = await _Round(PrintScheduler(), _finished(tray_now=1, unread=())).run(ctx)

        assert r.unloaded == [None]
        assert r.refreshed == []
        assert any("unloaded 1 hotend(s), nothing left to read" in line for line in _lines(caplog))


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
