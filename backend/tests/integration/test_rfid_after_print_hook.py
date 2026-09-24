"""Where the after-print AMS read hangs off the print-complete callback.

``ams_read_unidentified_after_print`` only ever gets to do anything if two
orderings hold in ``on_print_complete``:

* every print that ends schedules a round, completed or failed alike -- both
  leave the machine idle with the same unread spool in it;
* ``[AUTO-OFF-BG]`` does not cut the printer's power while the round has the
  AMS moving filament. A round that loses its printer half way through is the
  quietest possible failure, and the one the owner already lived through with
  this feature's predecessor.

The round itself is pinned in
``backend/tests/unit/test_scheduler_rfid_after_print.py``; this file is only
about the wiring around it.
"""

import asyncio
import logging
from contextlib import ExitStack
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


def _setup(stack, *, read_after_print=None):
    """``on_print_complete`` with everything but the two tasks under test stubbed.

    The archive has to be found: the no-archive branch returns before the block
    that spawns the post-print tasks, so a callback that cannot place the print
    never reaches the code under test (and never reaches auto-off either).
    """
    stack.enter_context(patch.dict("backend.app.main._active_prints", {(1, "Test"): 42}, clear=True))
    stack.enter_context(patch("backend.app.main.ArchiveService")).return_value = MagicMock(
        update_status=AsyncMock(),
        attach_timelapse=AsyncMock(),
    )
    mock_session_maker = stack.enter_context(patch("backend.app.main.async_session"))
    stack.enter_context(patch("backend.app.main.notification_service")).on_print_complete = AsyncMock()
    mock_plug = stack.enter_context(patch("backend.app.main.smart_plug_manager"))
    mock_plug.on_print_complete = AsyncMock()
    mock_plug.schedule_off_after_queue_job = AsyncMock()
    mock_ws = stack.enter_context(patch("backend.app.main.ws_manager"))
    mock_ws.send_print_complete = AsyncMock()
    mock_ws.broadcast = AsyncMock()
    stack.enter_context(patch("backend.app.main.mqtt_relay")).on_print_complete = AsyncMock()
    mock_pm = stack.enter_context(patch("backend.app.main.printer_manager"))
    mock_pm.get_printer.return_value = None
    mock_scheduler = stack.enter_context(patch("backend.app.main.print_scheduler"))
    mock_scheduler.read_unidentified_slots_after_print = read_after_print or AsyncMock()

    mock_session = AsyncMock()
    mock_session.__aenter__ = AsyncMock(return_value=mock_session)
    mock_session.__aexit__ = AsyncMock()
    mock_session.execute = AsyncMock(return_value=MagicMock(scalar_one_or_none=MagicMock(return_value=None)))
    mock_session_maker.return_value = mock_session
    return mock_scheduler, mock_plug


async def _fire(status="completed"):
    from backend.app.main import on_print_complete

    await on_print_complete(
        1,
        {
            "status": status,
            "filename": "/data/Metadata/test.gcode",
            "subtask_name": "Test",
            "timelapse_was_active": False,
        },
    )


async def _drain(tasks_before, *, wait=1.0):
    """Let the callback's background tasks finish, then cancel what is left.

    They must not outlive the patches: a survivor would run against the real
    session maker and the real printers.
    """
    spawned = asyncio.all_tasks() - tasks_before
    if spawned:
        await asyncio.wait(spawned, timeout=wait)
    for task in asyncio.all_tasks() - tasks_before:
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):
            pass


class TestEveryPrintThatEndsSchedulesARound:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("status", ["completed", "failed", "aborted", "cancelled"])
    async def test_the_round_is_scheduled_whatever_ended_the_print(self, status):
        """A failed, aborted or cancelled print leaves the machine just as idle
        and the spool just as unread; the setting itself is read inside the
        round, so this stage does not second-guess it."""
        tasks_before = set(asyncio.all_tasks())
        with ExitStack() as stack:
            scheduler, _plug = _setup(stack)
            await _fire(status)
            await _drain(tasks_before)

        scheduler.read_unidentified_slots_after_print.assert_awaited_once_with(1)


class TestAutoOffWaitsForTheRound:
    @pytest.mark.asyncio
    async def test_the_power_is_not_cut_while_the_ams_is_moving_filament(self):
        reading = asyncio.Event()
        release = asyncio.Event()

        async def slow_round(printer_id):
            reading.set()
            await release.wait()

        tasks_before = set(asyncio.all_tasks())
        with ExitStack() as stack:
            _scheduler, plug = _setup(stack, read_after_print=AsyncMock(side_effect=slow_round))
            await _fire()
            await asyncio.wait_for(reading.wait(), timeout=5)

            # Mid-round: auto-off has not even asked the smart plugs yet.
            plug.on_print_complete.assert_not_awaited()

            release.set()
            await _drain(tasks_before)

        plug.on_print_complete.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_a_round_that_raises_does_not_strand_the_auto_off(self):
        """The gate is released in a ``finally``: a printer left powered
        because a read blew up would be a worse bug than the one this fixes."""
        tasks_before = set(asyncio.all_tasks())
        with ExitStack() as stack:
            _scheduler, plug = _setup(stack, read_after_print=AsyncMock(side_effect=RuntimeError("MQTT gone")))
            await _fire()
            await _drain(tasks_before)

        plug.on_print_complete.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_a_round_that_never_ends_is_given_up_on(self, caplog):
        """A task killed before its ``finally`` -- shutdown, a cancelled task --
        never sets the gate. The gate is a ceiling and not a promise: a printer
        left powered for ever because nobody released a flag is a worse bug
        than one powered down a little early."""

        async def never_ends(printer_id):
            await asyncio.Event().wait()

        tasks_before = set(asyncio.all_tasks())
        with ExitStack() as stack:
            _scheduler, plug = _setup(stack, read_after_print=AsyncMock(side_effect=never_ends))
            stack.enter_context(patch("backend.app.main.RFID_AFTER_PRINT_MAX_WAIT", 0.05))
            with caplog.at_level(logging.WARNING, logger="backend.app.main"):
                await _fire()
                await _drain(tasks_before, wait=5)

        plug.on_print_complete.assert_awaited_once()
        assert any("still running after" in r.getMessage() for r in caplog.records)

    def test_the_ceiling_outlasts_a_round_that_runs_its_full_budget(self):
        """The ceiling only has a job if it is longer than the round it waits
        for; set below ``_RFID_REREAD_TASK_TIMEOUT`` it would cut every slow
        round short instead of catching the ones that died."""
        from backend.app.main import RFID_AFTER_PRINT_MAX_WAIT
        from backend.app.services.print_scheduler import _RFID_REREAD_TASK_TIMEOUT

        assert RFID_AFTER_PRINT_MAX_WAIT > _RFID_REREAD_TASK_TIMEOUT
