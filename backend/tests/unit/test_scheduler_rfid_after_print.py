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
  name, with the same rules, the same cap and the same per-slot cooldown as
  the pre-dispatch read;
- a loaded ``tray_now`` stands the whole round down, because that is the rule
  the transport enforces: ``ams_refresh_tray`` refuses every slot on the
  printer while that one value says anything is loaded. A printer that has
  finished normally has retracted and reports 255; one that has not is read
  after the next print that does -- unless
  ``ams_unload_before_after_print_read`` says to retract it first, which is
  what ``TestUnloadBeforeReading`` covers;
- nothing loaded means no unload command is sent at all, ever. Neither does a
  round with nothing to read once it has unloaded, nor one whose hotend is fed
  from a unit no unload command can address. That is the owner's own rule and
  the common case on his machines: an unload sent to a hotend that has already
  retracted, or for a spool nobody needs identified, is the defect;
- the second hotend of a dual-nozzle machine, which ``tray_now`` cannot
  describe, is found through ``extruder_slots``: it hides its own slot from a
  read, and gets its own addressed unload when the round is allowed to
  retract. It does not stand the round down -- the transport takes every other
  slot while ``tray_now`` reports 255;
- the two signals are refreshed at different rates, so when they disagree the
  printer is asked to push a full report before any filament moves, and before
  a landed unload is written off as having failed;
- the plate-clear gate is NOT consulted: a finished plate nobody has released
  is exactly the window this exists for;
- a print that takes the printer back ends the round at once, and a slot it
  cut short earns no cooldown -- nor does one the task ceiling cut short, and
  the slots that ceiling never reached are where the next round begins;
- the printer is reserved for the round and handed back on every exit,
  including an exception -- and the reservation outlasts the longest round it
  is ever taken for, because everything that keeps a printer safe while its AMS
  moves is read off that one hold;
- every round says what it did at info, once, including the rounds that read
  nothing -- the feature this replaces was believed broken for two evenings
  because its only evidence was at debug.
"""

import asyncio
import json
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
from backend.tests.unit.test_scheduler_rfid_preread import (
    _add_item,
    _cooling,
    _Harness,
    _item,
    _printer_state,
    _tray,
)

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
        patch("backend.app.services.print_scheduler._RFID_UNLOAD_SETTLE", 0.05),
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
    therefore asks both signals: the block is what finds the second hotend to
    retract when the round is allowed to retract.

    What it does *not* do is stand a round down on the block alone.
    ``ams_refresh_tray`` reads one value, ``tray_now``, so a printer reporting
    255 accepts every slot however loaded the block says its hotends are. A
    hotend that block knows about hides its own slot and nothing else.
    """

    @pytest.mark.asyncio
    async def test_a_hotend_only_the_extruder_block_knows_about_hides_its_own_slot(self, ctx, caplog):
        """``tray_now`` is 255 and the transport will take every slot, so the
        round reads the ones no hotend is holding -- which is what it did
        before this setting existed, and what leaving the setting off has to
        keep doing."""
        await _enable(ctx)
        state = _finished(unread=(1, 2, 3), extruder_slots={0: _hotend(0, 1), 1: _hotend(0, 2)})

        with caplog.at_level(logging.INFO, logger="backend.app.services.print_scheduler"):
            r = await _Round(PrintScheduler(), state).run(ctx)

        assert r.refreshed == [(0, 3)]
        assert r.unloaded == []
        assert any(
            "filament loaded (AMS0-T1, AMS0-T2), unload before reading is off, "
            "reading the slots it does not hold" in line
            for line in _lines(caplog)
        )

    @pytest.mark.asyncio
    async def test_the_same_hotends_are_retracted_when_the_round_may_retract(self, ctx):
        """The block's other job. With the setting on there is no reason to
        settle for the slots the hotends do not hold: retract both and read the
        lot."""
        await _enable(ctx, unload="true")
        state = _finished(unread=(1, 2, 3), extruder_slots={0: _hotend(0, 1), 1: _hotend(0, 2)})

        r = await _Round(PrintScheduler(), state).run(ctx)

        assert r.unloaded == [1, 2]
        assert r.refreshed == [(0, 1), (0, 2), (0, 3)]

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
    async def test_a_hotend_fed_from_a_unit_no_command_can_address_gets_none(self, ctx, caplog):
        """An AMS-HT dry box shares its unit id (128-135) and does not divide
        by four, and neither form of the command reaches one -- see
        ``test_neither_unload_form_reaches_an_ams_ht`` below for why the
        unaddressed one does not either. So nothing is sent: a command at a
        unit that does not exist would be followed by the full unload timeout
        spent waiting for a hotend nobody asked to retract."""
        await _enable(ctx, unload="true")
        scheduler = PrintScheduler()
        state = _finished(tray_now=128, extruder_slots={0: _hotend(128, 0)})

        with caplog.at_level(logging.INFO, logger="backend.app.services.print_scheduler"):
            r = await _Round(scheduler, state).run(ctx)

        r.client.ams_unload_filament.assert_not_called()
        assert r.refreshed == []
        assert scheduler._dispatch_holds == {}
        assert any(
            "AMS128-T0 fed from a unit no unload command can address, nothing read" in line for line in _lines(caplog)
        )

    def test_neither_unload_form_reaches_an_ams_ht(self):
        """Pinned against the real transport, because this is the whole reason
        the hotend above is left alone. The unaddressed form is not a fallback:
        it re-derives its unit from ``tray_now``, which for an AMS-HT-fed
        hotend IS the 128-135 id, so both forms divide it by four and publish
        at unit 32."""
        for tray_id in (None, 128):
            transport = BambuMQTTClient(ip_address="10.0.0.1", serial_number="X1C0001", access_code="x", model="X1C")
            transport._client = MagicMock()
            transport.state.connected = True
            transport.state.tray_now = 128

            assert transport.ams_unload_filament(tray_id=tray_id) is True
            sent = json.loads(transport._client.publish.call_args.args[1])["print"]
            assert sent["command"] == "ams_change_filament"
            assert sent["ams_id"] == 32

    @pytest.mark.asyncio
    async def test_a_tray_now_no_command_can_address_is_not_unloaded_either(self, ctx, caplog):
        """The same unit reached through the other signal: a printer with no
        extruder block reporting ``tray_now`` 128. The unaddressed command is
        all there is there and it goes to unit 32 just the same."""
        await _enable(ctx, unload="true")

        with caplog.at_level(logging.INFO, logger="backend.app.services.print_scheduler"):
            r = await _Round(PrintScheduler(), _finished(tray_now=128)).run(ctx)

        r.client.ams_unload_filament.assert_not_called()
        assert r.refreshed == []
        assert any(
            "tray 128 fed from a unit no unload command can address, nothing read" in line for line in _lines(caplog)
        )

    @pytest.mark.asyncio
    async def test_a_strand_no_ams_is_feeding_is_not_unloaded_either(self, ctx, caplog):
        """``has_filament`` with no ``snow``: filament in the hotend that no
        AMS slot is behind. There is nothing for an addressed command to name
        and nothing for the unaddressed one to resolve -- ``tray_now`` says
        255 -- and an AMS-side unload could not pull a hotend-side strand back
        anyway. So no command, and the slots are read as they were before this
        setting existed."""
        await _enable(ctx, unload="true")
        state = _finished(unread=(3,), extruder_slots={0: _hotend(None, None)})

        with caplog.at_level(logging.INFO, logger="backend.app.services.print_scheduler"):
            r = await _Round(PrintScheduler(), state).run(ctx)

        r.client.ams_unload_filament.assert_not_called()
        assert r.refreshed == [(0, 3)]
        assert any("extruder 0 fed from a unit no unload command can address" in line for line in _lines(caplog))

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
    async def test_nothing_to_read_means_nothing_is_unloaded(self, ctx, caplog):
        """The owner's rule pointed the other way. A printer that ends every
        print loaded is the only kind this setting ever acts on, and once its
        spools have all been identified once -- or are parked as unreadable --
        there is nothing left to read. Retracting anyway would nose-heat, pull
        the strand back and make the next job purge again, after every print,
        for ever."""
        await _enable(ctx, unload="true")
        scheduler = PrintScheduler()

        with caplog.at_level(logging.INFO, logger="backend.app.services.print_scheduler"):
            r = await _Round(scheduler, _finished(tray_now=1, unread=())).run(ctx)

        r.client.ams_unload_filament.assert_not_called()
        assert r.refreshed == []
        assert scheduler._dispatch_holds == {}
        assert any("filament loaded (tray 1), nothing to read, not unloading" in line for line in _lines(caplog))

    @pytest.mark.asyncio
    async def test_the_slot_in_the_hotend_counts_as_something_to_read(self, ctx):
        """The evaluation that decides whether to retract has to ask about the
        slot the hotend is holding too -- that slot is precisely what the
        retraction makes readable. Dropping it the way a read of a loaded
        printer does would make the one spool worth unloading for look like
        nothing to do."""
        await _enable(ctx, unload="true")
        state = _finished(tray_now=1, unread=(1,), extruder_slots={0: _hotend(0, 1)})

        r = await _Round(PrintScheduler(), state).run(ctx)

        assert r.unloaded == [1]
        assert r.refreshed == [(0, 1)]

    @pytest.mark.asyncio
    async def test_a_round_that_unloads_and_finds_nothing_left_still_says_so(self, ctx, caplog):
        """The slot was worth retracting for when the round decided, and the
        AMS named it by itself while the filament was moving. Rare, and it
        still has to leave a line behind."""
        await _enable(ctx, unload="true")
        state = _finished(tray_now=1, unread=(3,))

        def unload(tray_id=None):
            _retract(state, tray_id)
            state.tray_read_done_bits = "f"
            state.raw_data["ams"][0]["tray"][3] = _tray(3)
            return True

        with caplog.at_level(logging.INFO, logger="backend.app.services.print_scheduler"):
            r = await _Round(PrintScheduler(), state, unload=MagicMock(side_effect=unload)).run(ctx)

        assert r.unloaded == [None]
        assert r.refreshed == []
        assert any("unloaded 1 hotend(s), nothing left to read" in line for line in _lines(caplog))


class TestTheTwoSignalsDisagreeing:
    """``extruder_slots`` and ``tray_now`` are not refreshed together.

    ``_parse_extruder_slots`` deliberately keeps its previous answer whenever a
    payload omits ``device.extruder.info`` -- a frame carrying only
    temperatures must not read as "both hotends are now empty" -- while
    ``tray_now`` rides every ``print.ams`` delta, and nothing asks for a full
    push when a print ends. So either signal can be the stale one, in either
    direction, and a round that believed the wrong one would either move
    filament on an empty machine or throw away a retraction that worked.

    The round's answer is neither: it asks the printer to push a full report
    and gives the two a moment to meet. What survives that is believed --
    a dual-nozzle machine loaded only on its idle hotend reports the same
    shape honestly.
    """

    @pytest.mark.asyncio
    async def test_a_stale_extruder_block_does_not_buy_an_unload(self, ctx):
        """The end-of-print retract arrives as a ``print.ams`` delta with no
        ``device`` block in it: ``tray_now`` goes to 255 and the extruder entry
        still says loaded. Believing the entry means heating a nozzle and
        unloading a printer that has already retracted."""
        await _enable(ctx, unload="true")
        state = _finished(unread=(3,), extruder_slots={0: _hotend(0, 1)})
        pushed: list[bool] = []

        def push():
            pushed.append(True)
            state.extruder_slots[0].has_filament = False
            return True

        r = _Round(PrintScheduler(), state)
        r.client.request_status_update = MagicMock(side_effect=push)
        await r.run(ctx)

        assert pushed, "the round never asked the printer for a fresh report"
        r.client.ams_unload_filament.assert_not_called()
        assert r.refreshed == [(0, 3)]

    @pytest.mark.asyncio
    async def test_a_block_that_keeps_saying_loaded_is_believed(self, ctx):
        """The honest dual-nozzle shape: the idle hotend holds a spool that
        ``tray_now``, which follows the active one, cannot name. A fresh push
        says the same thing, so the round retracts it."""
        await _enable(ctx, unload="true")
        state = _finished(unread=(1, 3), extruder_slots={0: _hotend(0, 1)})

        r = await _Round(PrintScheduler(), state).run(ctx)

        assert r.unloaded == [1]
        assert r.refreshed == [(0, 1), (0, 3)]

    @pytest.mark.asyncio
    async def test_a_tray_now_lagging_behind_a_landed_unload_does_not_lose_the_round(self, ctx, caplog):
        """The other direction, after the filament has already moved. The
        extruder block clears first, so the unload wait is satisfied, and
        ``tray_now`` is still reporting the old tray on that same tick. Giving
        up there costs the retraction *and* the read, on every print."""
        await _enable(ctx, unload="true")
        state = _finished(tray_now=1, unread=(3,), extruder_slots={0: _hotend(0, 1)})

        def unload(tray_id=None):
            # Only the block clears; `tray_now` has not caught up yet.
            state.extruder_slots[0].has_filament = False
            return True

        r = _Round(PrintScheduler(), state, unload=MagicMock(side_effect=unload))
        r.client.request_status_update = MagicMock(side_effect=lambda: setattr(state, "tray_now", 255))

        with caplog.at_level(logging.INFO, logger="backend.app.services.print_scheduler"):
            await r.run(ctx)

        assert r.unloaded == [1]
        assert r.refreshed == [(0, 3)]
        assert not any("still loaded after" in line for line in _lines(caplog))


class TestTheReservationOutlastsTheRound:
    """The hold has to survive the longest round it is taken for.

    Everything that keeps a printer safe while its AMS moves -- the queue's own
    reservation set, ``rfid_read_in_flight``, and through it both auto-off
    waits -- is read off ``_dispatch_holds``. A hold that expires while the
    round is still running does not fail loudly: the queue simply starts
    dispatching again and the plug simply switches, with nothing timed out and
    nothing logged.
    """

    def test_the_ceiling_is_derived_from_the_budget_it_has_to_cover(self):
        """The regression this replaces was exactly these numbers drifting
        apart: the unload phase lengthened the round past a hold ceiling
        written out by hand somewhere else."""
        from backend.app.services.print_scheduler import (
            _RFID_REREAD_MAX_HOLD,
            _RFID_REREAD_ROUND_BUDGET,
            RFID_AFTER_PRINT_MAX_WAIT,
        )

        assert _RFID_REREAD_MAX_HOLD > _RFID_REREAD_ROUND_BUDGET
        # And the auto-off deadline outlasts the hold, so a wait that ends
        # early ends on its own warning rather than on a hold quietly expiring.
        assert RFID_AFTER_PRINT_MAX_WAIT > _RFID_REREAD_MAX_HOLD

    def test_a_round_still_holds_its_printer_past_the_dispatch_timeout(self):
        """A dual-nozzle round that spends its whole unload budget is past
        ``_dispatch_max_hold`` while it is still retracting."""
        scheduler = PrintScheduler()
        scheduler._reserve_for_rfid_reread(1, None)
        started, marker, subtask = scheduler._dispatch_holds[1]
        scheduler._dispatch_holds[1] = (started - (scheduler._dispatch_max_hold + 1.0), marker, subtask)

        assert scheduler.rfid_read_in_flight(1) is True
        assert scheduler._printer_in_dispatch_hold(1) is True
        assert scheduler._rfid_rereads == {1: None}

    def test_a_reservation_nobody_drops_still_expires(self):
        """It is a net for a task that died before its ``finally``, not a
        promise: past the round's own ceiling the printer goes back."""
        from backend.app.services.print_scheduler import _RFID_REREAD_MAX_HOLD

        scheduler = PrintScheduler()
        scheduler._reserve_for_rfid_reread(1, None)
        started, marker, subtask = scheduler._dispatch_holds[1]
        scheduler._dispatch_holds[1] = (started - (_RFID_REREAD_MAX_HOLD + 1.0), marker, subtask)

        assert scheduler.rfid_read_in_flight(1) is False
        assert scheduler._dispatch_holds == {}
        assert scheduler._rfid_rereads == {}

    def test_a_dispatch_hold_keeps_the_shorter_timeout(self):
        """The longer ceiling is for filament moving, not for a printer
        digesting a project_file."""
        scheduler = PrintScheduler()
        scheduler._mark_printer_dispatched(1, "FINISH", None)
        started, pre_state, subtask = scheduler._dispatch_holds[1]
        scheduler._dispatch_holds[1] = (started - (scheduler._dispatch_max_hold + 1.0), pre_state, subtask)

        assert scheduler._printer_in_dispatch_hold(1) is False


class TestTheRoundIsAnnouncedBeforeItExists:
    """``on_print_complete`` schedules the queue's per-job auto-off about a
    thousand lines before it spawns the round, with awaited database and
    archive work in between. ``off_delay_minutes`` of 0 is legal, so that off
    reaches its gate while no round has reserved anything yet.
    """

    @pytest.mark.asyncio
    async def test_the_announcement_holds_the_gate_before_the_round_starts(self, ctx):
        scheduler = PrintScheduler()
        await _enable(ctx)

        with patch("backend.app.services.print_scheduler.async_session", ctx.session_maker):
            await scheduler.expect_after_print_read(1)

        assert scheduler.rfid_read_in_flight(1) is True
        # And the queue is not told: nothing has been reserved yet, and a
        # printer this round may never touch has to stay dispatchable.
        assert scheduler._dispatch_holds == {}

    @pytest.mark.asyncio
    async def test_an_install_that_does_not_read_is_not_delayed(self, ctx):
        scheduler = PrintScheduler()

        with patch("backend.app.services.print_scheduler.async_session", ctx.session_maker):
            await scheduler.expect_after_print_read(1)

        assert scheduler.rfid_read_in_flight(1) is False

    @pytest.mark.asyncio
    async def test_a_round_that_declines_drops_the_announcement(self, ctx):
        """The gate must not outlive a round that decided it had nothing to
        do -- here a printer already printing again."""
        await _enable(ctx)
        scheduler = PrintScheduler()

        with patch("backend.app.services.print_scheduler.async_session", ctx.session_maker):
            await scheduler.expect_after_print_read(1)
        await _Round(scheduler, _finished(state="RUNNING")).run(ctx)

        assert scheduler.rfid_read_in_flight(1) is False

    @pytest.mark.asyncio
    async def test_an_announcement_nobody_answers_expires(self, ctx):
        """A round that is never spawned at all -- the callback blew up on the
        way to it -- must not leave a printer powered for ever."""
        from backend.app.services.print_scheduler import _RFID_READ_PENDING_GRACE

        await _enable(ctx)
        scheduler = PrintScheduler()

        with patch("backend.app.services.print_scheduler.async_session", ctx.session_maker):
            await scheduler.expect_after_print_read(1)
        scheduler._rfid_read_pending[1] = time.monotonic() - 1.0

        assert _RFID_READ_PENDING_GRACE > 0
        assert scheduler.rfid_read_in_flight(1) is False
        assert scheduler._rfid_read_pending == {}


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
    async def test_a_slot_the_print_cut_short_earns_no_cooldown(self, ctx):
        """It never got its attempt. Resting it now would silence exactly the
        spool this feature exists to identify, for the next half hour."""
        await _enable(ctx)
        state = _finished(unread=(3,))
        state.tray_read_done_bits = "f"

        def refresh(ams_id, slot_id):
            state.state = "RUNNING"
            return True, "Refreshing"

        r = await _Round(PrintScheduler(), state, refresh=MagicMock(side_effect=refresh)).run(ctx, slot_timeout=30.0)

        assert _cooling(r.scheduler) == {}

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


class TestTheSlotThatReadNothing:
    """A round that reads nothing out of a slot rests it, and the next round
    is told how long the rest has left.

    The owner reported three times that unidentified spools were never
    identified automatically while the manual button identified the very same
    spool every time it was pressed. The slot had been written off for the
    life of the process on one read that ran out of budget -- and it was an
    original Bambu spool with a perfectly good tag in it.
    """

    @staticmethod
    def _nameless_but_done():
        state = _finished(unread=(3,))
        state.tray_read_done_bits = "f"
        return state

    @pytest.mark.asyncio
    async def test_a_slot_that_stays_nameless_rests_over_the_next_print(self, ctx, caplog):
        await _enable(ctx)
        scheduler = PrintScheduler()
        state = self._nameless_but_done()

        first = await _Round(scheduler, state).run(ctx)
        assert first.refreshed == [(0, 3)]
        assert _cooling(scheduler) == {1: {(0, 3)}}

        with caplog.at_level(logging.INFO, logger="backend.app.services.print_scheduler"):
            second = await _Round(scheduler, state).run(ctx)

        assert second.refreshed == []
        assert any(
            "0 unread slot(s) (1 skipped: on cooldown (AMS0-T3 (30 min left)))" in line for line in _lines(caplog)
        )

    @pytest.mark.asyncio
    async def test_the_rest_ends_and_the_print_after_it_asks_again(self, ctx):
        """The whole point of a cooldown over a verdict: the spool that merely
        read slowly gets another turn the same afternoon."""
        await _enable(ctx)
        scheduler = PrintScheduler()
        state = self._nameless_but_done()

        with patch("backend.app.services.print_scheduler._RFID_SLOT_COOLDOWN", 0.05):
            first = await _Round(scheduler, state).run(ctx)
        assert first.refreshed == [(0, 3)]
        assert _cooling(scheduler) == {1: {(0, 3)}}

        await asyncio.sleep(0.06)
        second = await _Round(scheduler, state).run(ctx)

        assert second.refreshed == [(0, 3)]

    @pytest.mark.asyncio
    async def test_pulling_the_spool_ends_the_rest_at_once(self, ctx):
        """Without the half hour being waited out: the next roll in that slot
        is a different roll, and it has its own tag."""
        await _enable(ctx)
        scheduler = PrintScheduler()
        state = self._nameless_but_done()

        await _Round(scheduler, state).run(ctx)
        assert _cooling(scheduler) == {1: {(0, 3)}}

        state.tray_exist_bits = "7"  # roll pulled
        with _Round(scheduler, state).patched(ctx):
            scheduler._prune_rfid_cooldowns()
        assert _cooling(scheduler) == {}

        state.tray_exist_bits = "f"  # and another one goes in
        second = await _Round(scheduler, state).run(ctx)
        assert second.refreshed == [(0, 3)]

    @pytest.mark.asyncio
    async def test_a_slot_firmware_itself_calls_unread_is_asked_every_time(self, ctx):
        await _enable(ctx)
        scheduler = PrintScheduler()
        state = self._nameless_but_done()

        await _Round(scheduler, state).run(ctx)
        assert _cooling(scheduler) == {1: {(0, 3)}}

        # A Bambu spool swapped in for the one that read nothing: still
        # nameless, but firmware now says it has not read this slot.
        state.tray_read_done_bits = "7"
        second = await _Round(scheduler, state).run(ctx)

        assert second.refreshed == [(0, 3)]

    @pytest.mark.asyncio
    async def test_the_round_after_a_ceiling_starts_where_it_stopped(self, ctx):
        """`_RFID_REREAD_TASK_TIMEOUT` bounds the round here as it does before a
        dispatch, and both rounds share the one cursor: on a farm whose every
        AMS slot is nameless, the slots the ceiling never reached are the next
        round's rather than nobody's. The slot it cut short earns no rest
        either -- it did not get the attempt a rest is meant to follow."""
        await _enable(ctx)
        scheduler = PrintScheduler()
        state = _finished(unread=(0, 1, 2, 3))
        state.tray_read_done_bits = "f"

        # A ceiling shorter than one slot budget: the first slot is asked with
        # a fraction of it, and the three behind it are never attempted.
        with patch("backend.app.services.print_scheduler._RFID_REREAD_TASK_TIMEOUT", 0.05):
            first = await _Round(scheduler, state).run(ctx, slot_timeout=10.0)
        assert first.refreshed == [(0, 0)]
        assert _cooling(scheduler) == {}

        with patch("backend.app.services.print_scheduler._RFID_REREAD_TASK_TIMEOUT", 0.05):
            second = await _Round(scheduler, state).run(ctx, slot_timeout=10.0)
        assert second.refreshed == [(0, 1)]


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
