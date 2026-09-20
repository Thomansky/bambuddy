"""The printer's own calibration run must not be handled as a print (#3081, #3127).

Pins for ``on_print_start`` / ``on_print_complete`` / ``on_finish_photo_moment``
in ``backend.app.main``: an internal job raises no plate-clear gate, captures
and books no filament usage, creates no archive, sends no notification, and
hands its result to the maintenance executor instead. Driven the way
``backend/tests/integration/test_print_lifecycle.py`` drives the callbacks.
"""

import asyncio
from contextlib import ExitStack
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

pytestmark = pytest.mark.unit

CALIBRATION = {
    "filename": "/usr/etc/print/H2S/auto_cali_for_user_param.gcode",
    "subtask_name": "auto_cali_for_user_param.gcode",
}


def _complete_mocks(stack: ExitStack) -> dict:
    mocks = {
        "session": stack.enter_context(patch("backend.app.main.async_session")),
        "notif": stack.enter_context(patch("backend.app.main.notification_service")),
        "plug": stack.enter_context(patch("backend.app.main.smart_plug_manager")),
        "ws": stack.enter_context(patch("backend.app.main.ws_manager")),
        "relay": stack.enter_context(patch("backend.app.main.mqtt_relay")),
        "pm": stack.enter_context(patch("backend.app.main.printer_manager")),
        "spawn": stack.enter_context(patch("backend.app.main.spawn_background_task")),
        "cache": stack.enter_context(patch("backend.app.main.clear_3mf_cache")),
        "usage": stack.enter_context(
            patch("backend.app.services.usage_tracker.on_print_complete", new_callable=AsyncMock)
        ),
        "finished": stack.enter_context(
            patch("backend.app.main.maintenance_actions.on_internal_job_finished", new_callable=AsyncMock)
        ),
    }
    mocks["notif"].on_print_complete = AsyncMock()
    mocks["ws"].send_print_complete = AsyncMock()
    mocks["ws"].broadcast = AsyncMock()
    mocks["relay"].on_print_complete = AsyncMock()
    mocks["pm"].get_printer.return_value = None
    mocks["pm"].set_awaiting_plate_clear = MagicMock()
    mocks["finished"].return_value = True
    return mocks


async def _cancel_new_tasks(before):
    for task in asyncio.all_tasks() - before:
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):
            pass


class TestPrintCompleteForAnInternalJob:
    @pytest.mark.asyncio
    async def test_completed_calibration_touches_nothing_but_the_executor(self):
        tasks_before = set(asyncio.all_tasks())
        with ExitStack() as stack:
            m = _complete_mocks(stack)
            from backend.app.main import on_print_complete

            await on_print_complete(1, {**CALIBRATION, "status": "completed", "raw_data": {"print_error": 0}})
            await _cancel_new_tasks(tasks_before)

        # No plate-clear gate: a calibration leaves nothing on the plate.
        gate_calls = [c for c in m["pm"].set_awaiting_plate_clear.call_args_list if c.args[1] is True]
        assert gate_calls == []
        # No usage booking (#3081), no archive lookup, no queue reconciliation.
        m["usage"].assert_not_called()
        m["session"].assert_not_called()
        m["cache"].assert_not_called()
        # No notification of any kind.
        m["notif"].on_print_complete.assert_not_called()
        m["relay"].on_print_complete.assert_not_called()
        m["ws"].send_print_complete.assert_not_called()
        assert not [c for c in m["spawn"].call_args_list if "notify" in str(c)]
        # The executor is told, with the printer's own verdict.
        m["finished"].assert_awaited_once_with(
            1, CALIBRATION["filename"], CALIBRATION["subtask_name"], "completed", None
        )

    @pytest.mark.asyncio
    async def test_cancelled_calibration_passes_the_print_error_on(self):
        tasks_before = set(asyncio.all_tasks())
        with ExitStack() as stack:
            m = _complete_mocks(stack)
            from backend.app.main import on_print_complete

            await on_print_complete(1, {**CALIBRATION, "status": "failed", "raw_data": {"print_error": 50348044}})
            await _cancel_new_tasks(tasks_before)

        m["finished"].assert_awaited_once_with(
            1, CALIBRATION["filename"], CALIBRATION["subtask_name"], "failed", 50348044
        )
        gate_calls = [c for c in m["pm"].set_awaiting_plate_clear.call_args_list if c.args[1] is True]
        assert gate_calls == []

    @pytest.mark.asyncio
    async def test_a_stop_during_the_calibration_does_not_leak_into_the_next_print(self):
        from backend.app import main as main_module

        tasks_before = set(asyncio.all_tasks())
        main_module._user_stopped_printers.add(1)
        with ExitStack() as stack:
            _complete_mocks(stack)
            await main_module.on_print_complete(1, {**CALIBRATION, "status": "failed"})
            await _cancel_new_tasks(tasks_before)
        assert 1 not in main_module._user_stopped_printers

    @pytest.mark.asyncio
    async def test_an_executor_error_does_not_break_the_callback(self):
        tasks_before = set(asyncio.all_tasks())
        with ExitStack() as stack:
            m = _complete_mocks(stack)
            m["finished"].side_effect = RuntimeError("db down")
            from backend.app.main import on_print_complete

            await on_print_complete(1, {**CALIBRATION, "status": "completed"})
            await _cancel_new_tasks(tasks_before)
        m["finished"].assert_awaited_once()

    @pytest.mark.asyncio
    async def test_a_real_print_still_raises_the_gate(self):
        """The guard must not swallow ordinary completions."""
        tasks_before = set(asyncio.all_tasks())
        with ExitStack() as stack:
            m = _complete_mocks(stack)
            mock_session = AsyncMock()
            mock_session.__aenter__ = AsyncMock(return_value=mock_session)
            mock_session.__aexit__ = AsyncMock()
            mock_session.execute = AsyncMock(
                return_value=MagicMock(scalar_one_or_none=MagicMock(return_value=None), scalars=MagicMock())
            )
            m["session"].return_value = mock_session
            from backend.app.main import on_print_complete

            await on_print_complete(
                1, {"filename": "/data/Metadata/plate_1.gcode", "subtask_name": "Benchy", "status": "completed"}
            )
            await _cancel_new_tasks(tasks_before)
        m["pm"].set_awaiting_plate_clear.assert_any_call(1, True)
        m["finished"].assert_not_called()


class TestPrintStartForAnInternalJob:
    @pytest.mark.asyncio
    async def test_no_usage_session_no_relay_no_smart_plug(self, capture_logs):
        with ExitStack() as stack:
            session = stack.enter_context(patch("backend.app.main.async_session"))
            notif = stack.enter_context(patch("backend.app.main.notification_service"))
            plug = stack.enter_context(patch("backend.app.main.smart_plug_manager"))
            ws = stack.enter_context(patch("backend.app.main.ws_manager"))
            stack.enter_context(patch("backend.app.main.printer_manager"))
            relay = stack.enter_context(patch("backend.app.main.mqtt_relay"))
            usage = stack.enter_context(
                patch("backend.app.services.usage_tracker.on_print_start", new_callable=AsyncMock)
            )
            notify = stack.enter_context(
                patch("backend.app.main._send_print_start_notification", new_callable=AsyncMock)
            )
            notif.on_print_start = AsyncMock()
            plug.on_print_start = AsyncMock()
            ws.send_print_start = AsyncMock()
            relay.on_print_start = AsyncMock()

            from backend.app.main import on_print_start

            await on_print_start(1, dict(CALIBRATION))

            # Per-printer bookkeeping still runs...
            ws.send_print_start.assert_awaited_once()
            # ...but nothing that describes a user's print.
            usage.assert_not_called()
            session.assert_not_called()
            plug.on_print_start.assert_not_called()
            relay.on_print_start.assert_not_called()
            notify.assert_not_called()

        assert [r for r in capture_logs.records if "internal printer job" in str(r.message)]


class TestFinishPhotoForAnInternalJob:
    @pytest.mark.asyncio
    async def test_no_capture_and_no_plate_move(self):
        with ExitStack() as stack:
            session = stack.enter_context(patch("backend.app.main.async_session"))
            restore = stack.enter_context(
                patch("backend.app.main._restore_plate_for_finish_photo", new_callable=AsyncMock)
            )
            from backend.app.main import on_finish_photo_moment

            await on_finish_photo_moment(1, {**CALIBRATION, "trigger": "finish_state", "timelapse_was_active": False})
        session.assert_not_called()
        restore.assert_not_called()
