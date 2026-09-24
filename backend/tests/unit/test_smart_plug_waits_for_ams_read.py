"""Auto-off does not cut mains power while the AMS is reading a spool.

``ams_read_unidentified_after_print`` has the AMS feeding filament to its RFID
reader for a few seconds after a print ends -- which is exactly the window a
smart plug is configured to switch in. Power lost half way leaves the filament
stranded in the feed path and the AMS needing clearing by hand.

``on_print_complete`` in main.py holds ``[AUTO-OFF-BG]`` back on an event, but
that covers one of the ways an off gets scheduled. The per-job
``auto_off_after`` toggle reaches :meth:`schedule_off_after_queue_job` from
three call sites -- one of them a thousand lines earlier in that same callback,
long before the event exists -- and ``off_delay_minutes`` of 0 is a legal
setting, as is a temperature threshold a nozzle is already below after an early
failure. So the wait lives at the point power is actually switched, and these
tests pin it there:

- a pending off waits while a round holds the printer, whichever mode it is in;
- it goes straight through when no round does;
- it gives up rather than leave a printer powered for ever;
- the queue's own per-job toggle goes through the same wait;
- it waits for a round announced but not yet started, and keeps waiting past
  the ordinary dispatch-hold timeout -- the two ways the wait ended early and
  silently, with the plug switching mid-retraction and nothing in the log.
"""

import asyncio
import logging
import time
from contextlib import ExitStack
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from backend.app.services.print_scheduler import PrintScheduler
from backend.app.services.smart_plug_manager import SmartPlugManager

PLUG_LOGGER = "backend.app.services.smart_plug_manager"


def _plug(**over):
    fields = {
        "id": 1,
        "name": "Printer 1 plug",
        "enabled": True,
        "plug_type": "tasmota",
        "ip_address": "10.0.0.2",
        "ha_entity_id": None,
        "username": None,
        "password": None,
        "controls_printer_power": True,
        "off_delay_mode": "time",
        "off_delay_minutes": 0,
        "off_temp_threshold": 50,
        "rest_off_url": None,
        "rest_off_body": None,
        "rest_method": None,
        "rest_headers": None,
    }
    fields.update(over)
    return SimpleNamespace(**fields)


class _Rig:
    """A plug that switches through a mock, against a real scheduler.

    The scheduler is real because the thing under test is the agreement between
    two services: the round takes the reservation, the plug reads it. A stub in
    the middle would pin only that the plug asks something.
    """

    def __init__(self, **plug_kw):
        self.manager = SmartPlugManager()
        self.manager._mark_auto_off_pending = AsyncMock()
        self.manager._mark_auto_off_executed = AsyncMock()
        self.service = MagicMock(turn_off=AsyncMock(return_value=True))
        self.manager.get_service_for_plug = AsyncMock(return_value=self.service)
        self.plug = _plug(**plug_kw)
        self.manager._get_plugs_for_printer = AsyncMock(return_value=[self.plug])
        self.scheduler = PrintScheduler()

    @property
    def switched_off(self) -> bool:
        return self.service.turn_off.await_count > 0

    def reading(self, printer_id=1):
        """Put the printer under an after-print read round's reservation."""
        self.scheduler._reserve_for_rfid_reread(printer_id, None)

    def reading_since(self, seconds: float, printer_id=1):
        """The same reservation, taken *seconds* ago."""
        self.reading(printer_id)
        started, marker, subtask = self.scheduler._dispatch_holds[printer_id]
        self.scheduler._dispatch_holds[printer_id] = (started - seconds, marker, subtask)

    async def announced(self, printer_id=1):
        """A round the print-complete callback has promised but not spawned."""
        row = SimpleNamespace(key="ams_read_unidentified_after_print", value="true")
        session = AsyncMock()
        session.__aenter__ = AsyncMock(return_value=session)
        session.__aexit__ = AsyncMock()
        session.execute = AsyncMock(return_value=MagicMock(scalar_one_or_none=MagicMock(return_value=row)))
        with patch("backend.app.services.print_scheduler.async_session", MagicMock(return_value=session)):
            await self.scheduler.expect_after_print_read(printer_id)

    def done_reading(self, printer_id=1):
        self.scheduler._release_rfid_reread_hold(printer_id)

    def patched(self, stack, *, nozzle=20.0):
        pm = MagicMock()
        pm.is_print_active.return_value = False
        pm.get_status.return_value = SimpleNamespace(state="FINISH", temperatures={"nozzle": nozzle})
        for p in (
            patch("backend.app.services.smart_plug_manager.printer_manager", pm),
            patch(
                "backend.app.services.smart_plug_manager.spawn_background_task",
                lambda coro, *, name=None: asyncio.ensure_future(coro),
            ),
            patch("backend.app.services.smart_plug_manager.AMS_READ_WAIT_POLL_INTERVAL", 0.01),
            patch("backend.app.services.print_scheduler.scheduler", self.scheduler),
        ):
            stack.enter_context(p)
        return self

    def delayed_off(self, printer_id=1, delay_seconds=0):
        return asyncio.ensure_future(
            self.manager._delayed_off(
                self.plug.id, self.plug.plug_type, self.plug.ip_address, None, None, None, printer_id, delay_seconds
            )
        )

    def temp_off(self, printer_id=1):
        return asyncio.ensure_future(
            self.manager._temp_based_off(
                self.plug.id,
                self.plug.plug_type,
                self.plug.ip_address,
                None,
                None,
                None,
                printer_id,
                self.plug.off_temp_threshold,
            )
        )


async def _settle(rounds=20):
    """Give a pending off every chance to fire, without waiting on a clock."""
    for _ in range(rounds):
        await asyncio.sleep(0.01)


class TestAPendingOffWaits:
    @pytest.mark.asyncio
    async def test_a_zero_delay_off_does_not_switch_while_the_ams_is_reading(self):
        """``off_delay_minutes`` is ``ge=0``, so "off the moment it finishes" is
        a setting somebody has. It is also the one that lands inside the round."""
        rig = _Rig(off_delay_minutes=0)
        rig.reading()

        with ExitStack() as stack:
            rig.patched(stack)
            task = rig.delayed_off(delay_seconds=0)
            await _settle()

            assert not rig.switched_off, "power was cut with the AMS still moving filament"

            rig.done_reading()
            await asyncio.wait_for(task, timeout=5)

        assert rig.switched_off

    @pytest.mark.asyncio
    async def test_a_temperature_based_off_waits_the_same_way(self):
        """A print that failed during heat-up leaves the nozzle below any
        threshold, so this branch fires on its first poll -- inside the round."""
        rig = _Rig(off_delay_mode="temperature")
        rig.reading()

        with ExitStack() as stack:
            rig.patched(stack, nozzle=20.0)
            task = rig.temp_off()
            await _settle()

            assert not rig.switched_off

            rig.done_reading()
            await asyncio.wait_for(task, timeout=5)

        assert rig.switched_off

    @pytest.mark.asyncio
    async def test_no_round_means_no_wait(self):
        rig = _Rig()

        with ExitStack() as stack:
            rig.patched(stack)
            await asyncio.wait_for(rig.delayed_off(), timeout=5)

        assert rig.switched_off

    @pytest.mark.asyncio
    async def test_a_hold_that_is_not_a_read_is_not_waited_for(self):
        """The post-dispatch hold means a print is starting, which the #1890
        ``is_print_active`` guard answers. Waiting on it here would park a
        pending off behind an unrelated reservation."""
        rig = _Rig()
        rig.scheduler._mark_printer_dispatched(1, "FINISH", None)

        with ExitStack() as stack:
            rig.patched(stack)
            await asyncio.wait_for(rig.delayed_off(), timeout=5)

        assert rig.switched_off


class TestTheWaitCoversTheWholeRound:
    """Two ways this wait used to end early, both of them silently.

    It ends when ``rfid_read_in_flight`` goes false, and that is read off the
    round's reservation. A reservation that expires while the round is still
    retracting therefore switches the plug with no deadline passed and no
    "powering down anyway" line -- the log does not even record that power was
    cut early. And a reservation that has not been taken yet, because the off
    was scheduled a thousand lines before the round was spawned, is not there
    to be read at all.
    """

    @pytest.mark.asyncio
    async def test_a_round_past_the_dispatch_timeout_still_holds_the_plug(self):
        """An unload phase is minutes long: a dual-nozzle round that spends it
        is past ``_dispatch_max_hold`` while filament is still moving. The
        reservation has its own, longer ceiling for exactly this."""
        rig = _Rig(off_delay_minutes=0)
        rig.reading_since(rig.scheduler._dispatch_max_hold + 1.0)

        with ExitStack() as stack:
            rig.patched(stack)
            task = rig.delayed_off(delay_seconds=0)
            await _settle()

            assert not rig.switched_off, "power was cut with the AMS still moving filament"

            rig.done_reading()
            await asyncio.wait_for(task, timeout=5)

        assert rig.switched_off

    @pytest.mark.asyncio
    async def test_an_announced_round_holds_the_plug_before_it_starts(self):
        """``on_print_complete`` schedules this off long before it spawns the
        round, so at this moment nothing has been reserved. Without the
        announcement the off sails through and the round starts unloading into
        a printer whose mains is already going."""
        rig = _Rig(off_delay_minutes=0)
        await rig.announced()

        with ExitStack() as stack:
            rig.patched(stack)
            task = rig.delayed_off(delay_seconds=0)
            await _settle()

            assert not rig.switched_off

            # The round starts for real, takes the reservation, and drops the
            # announcement the way `read_unidentified_slots_after_print` does.
            rig.reading()
            rig.scheduler._rfid_read_pending.pop(1, None)
            await _settle()

            assert not rig.switched_off

            rig.done_reading()
            await asyncio.wait_for(task, timeout=5)

        assert rig.switched_off

    @pytest.mark.asyncio
    async def test_an_announcement_nobody_answers_does_not_keep_a_printer_powered(self):
        """A round that is never spawned at all. The marker is a grace window,
        not a latch."""
        rig = _Rig(off_delay_minutes=0)
        await rig.announced()
        rig.scheduler._rfid_read_pending[1] = time.monotonic() - 1.0

        with ExitStack() as stack:
            rig.patched(stack)
            await asyncio.wait_for(rig.delayed_off(), timeout=5)

        assert rig.switched_off


class TestTheWaitIsBounded:
    @pytest.mark.asyncio
    async def test_a_reservation_nobody_releases_does_not_keep_a_printer_powered(self, caplog):
        """A round killed before its ``finally`` leaves the hold behind. A
        printer nobody ever powers down is a worse failure than one powered
        down a few seconds early, so the wait gives up and says so."""
        rig = _Rig()
        rig.reading()

        with ExitStack() as stack:
            rig.patched(stack)
            stack.enter_context(patch("backend.app.services.print_scheduler.RFID_AFTER_PRINT_MAX_WAIT", 0.05))
            with caplog.at_level(logging.WARNING, logger=PLUG_LOGGER):
                await asyncio.wait_for(rig.delayed_off(), timeout=5)

        assert rig.switched_off
        assert any("powering down anyway" in r.getMessage() for r in caplog.records if r.name == PLUG_LOGGER)


class TestThePerJobToggle:
    @pytest.mark.asyncio
    async def test_the_queue_s_own_auto_off_goes_through_the_same_wait(self):
        """The path the print-complete callback schedules a thousand lines
        before it creates its gate, and the one that does not check
        ``plug.auto_off`` either. It reaches the same switch, so it meets the
        same wait."""
        rig = _Rig(off_delay_minutes=0)
        rig.reading()

        with ExitStack() as stack:
            rig.patched(stack)
            await rig.manager.schedule_off_after_queue_job(1, MagicMock())
            await _settle()

            assert not rig.switched_off

            rig.done_reading()
            await asyncio.wait_for(rig.manager._pending_off[rig.plug.id], timeout=5)

        assert rig.switched_off
