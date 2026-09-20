"""Tests for PrintScheduler maintenance calibration runs (#3127).

Same shape as the scheduled-drying tests: rows in the test database, the
printer manager mocked, ``_check_maintenance_runs`` driven directly.
"""

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy import select

from backend.app.models.maintenance import MaintenanceHistory, MaintenanceRun, MaintenanceType, PrinterMaintenance
from backend.app.models.print_queue import PrintQueueItem
from backend.app.models.printer import Printer
from backend.app.services import maintenance_actions
from backend.app.services.print_scheduler import PrintScheduler

pytestmark = pytest.mark.unit


def _utcnow_naive() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _mock_state(state="IDLE", connected=True):
    mock = MagicMock()
    mock.state = state
    mock.connected = connected
    return mock


async def _make_item(db_session, printer_factory, *, action="calibration", printer_id=None, **kwargs):
    if printer_id is None:
        printer = await printer_factory(model=kwargs.pop("model", "X1C"))
        printer_id = printer.id
    maint_type = MaintenanceType(
        name="Printer Calibration" if action == "calibration" else "Vision Encoder Calibration",
        description="",
        default_interval_hours=100.0,
        interval_type="hours",
        is_system=True,
        action=action,
    )
    db_session.add(maint_type)
    await db_session.flush()
    defaults = {"printer_id": printer_id, "maintenance_type_id": maint_type.id, "enabled": True}
    defaults.update(kwargs)
    item = PrinterMaintenance(**defaults)
    db_session.add(item)
    await db_session.commit()
    await db_session.refresh(item)
    return item


async def _make_run(db_session, item, **kwargs):
    await db_session.refresh(item, ["maintenance_type"])
    defaults = {
        "printer_maintenance_id": item.id,
        "printer_id": item.printer_id,
        "status": "pending",
        "source": "manual",
        "options": (
            maintenance_actions.normalize_calibration_options(item.action_options)
            if item.maintenance_type.action == "calibration"
            else None
        ),
    }
    defaults.update(kwargs)
    run = MaintenanceRun(**defaults)
    db_session.add(run)
    await db_session.commit()
    await db_session.refresh(run)
    return run


@pytest.fixture
def scheduler():
    return PrintScheduler()


# ============== Dispatch ==============


@pytest.mark.asyncio
async def test_future_start_after_not_dispatched(scheduler, db_session, printer_factory):
    item = await _make_item(db_session, printer_factory)
    run = await _make_run(db_session, item, start_after=_utcnow_naive() + timedelta(hours=2))
    with patch("backend.app.services.print_scheduler.printer_manager") as mock_pm:
        mock_pm.get_status.return_value = _mock_state()
        await scheduler._check_maintenance_runs(db_session, False)
    mock_pm.start_calibration.assert_not_called()
    await db_session.refresh(run)
    assert run.status == "pending"
    assert run.waiting_reason is None


@pytest.mark.asyncio
async def test_offline_printer_waits_with_reason(scheduler, db_session, printer_factory):
    item = await _make_item(db_session, printer_factory)
    run = await _make_run(db_session, item)
    with patch("backend.app.services.print_scheduler.printer_manager") as mock_pm:
        mock_pm.get_status.return_value = None
        await scheduler._check_maintenance_runs(db_session, False)
    mock_pm.start_calibration.assert_not_called()
    await db_session.refresh(run)
    assert run.status == "pending"
    assert run.waiting_reason == "printer_offline"


@pytest.mark.asyncio
async def test_busy_printer_waits_with_reason(scheduler, db_session, printer_factory):
    item = await _make_item(db_session, printer_factory)
    run = await _make_run(db_session, item)
    with (
        patch("backend.app.services.print_scheduler.printer_manager") as mock_pm,
        patch.object(scheduler, "_is_printer_idle", return_value=False),
    ):
        mock_pm.get_status.return_value = _mock_state("RUNNING")
        mock_pm.is_awaiting_plate_clear.return_value = False
        await scheduler._check_maintenance_runs(db_session, True)
    mock_pm.start_calibration.assert_not_called()
    await db_session.refresh(run)
    assert run.waiting_reason == "printer_busy"


async def _run_pass_with_queue_reservations(scheduler, db_session, require_plate_clear=False):
    """Drive the maintenance pass the way check_queue does: seed first."""
    reserved = await scheduler._queue_reserved_printers(db_session)
    await scheduler._check_maintenance_runs(db_session, require_plate_clear, reserved)


@pytest.mark.asyncio
async def test_queue_item_already_printing_holds_the_calibration(scheduler, db_session, printer_factory):
    """The queue sent project_file seconds ago; the printer still says IDLE."""
    item = await _make_item(db_session, printer_factory)
    run = await _make_run(db_session, item)
    db_session.add(PrintQueueItem(printer_id=item.printer_id, status="printing"))
    await db_session.commit()
    with patch("backend.app.services.print_scheduler.printer_manager") as mock_pm:
        mock_pm.get_status.return_value = _mock_state("IDLE")
        mock_pm.is_connected.return_value = True
        await _run_pass_with_queue_reservations(scheduler, db_session)
    mock_pm.start_calibration.assert_not_called()
    await db_session.refresh(run)
    assert run.status == "pending"
    assert run.waiting_reason == "printer_busy"


@pytest.mark.asyncio
async def test_upload_in_flight_holds_the_calibration(scheduler, db_session, printer_factory):
    """The queue row is still pending while its 3MF uploads; the printer is IDLE."""
    item = await _make_item(db_session, printer_factory)
    run = await _make_run(db_session, item)
    scheduler._inflight[999] = (MagicMock(), item.printer_id)
    with patch("backend.app.services.print_scheduler.printer_manager") as mock_pm:
        mock_pm.get_status.return_value = _mock_state("IDLE")
        mock_pm.is_connected.return_value = True
        await _run_pass_with_queue_reservations(scheduler, db_session)
    mock_pm.start_calibration.assert_not_called()
    await db_session.refresh(run)
    assert run.status == "pending"
    assert run.waiting_reason == "printer_busy"


@pytest.mark.asyncio
async def test_post_dispatch_hold_holds_the_calibration(scheduler, db_session, printer_factory):
    item = await _make_item(db_session, printer_factory)
    run = await _make_run(db_session, item)
    scheduler._mark_printer_dispatched(item.printer_id, "IDLE", None)
    with patch("backend.app.services.print_scheduler.printer_manager") as mock_pm:
        mock_pm.get_status.return_value = _mock_state("IDLE")
        mock_pm.is_connected.return_value = True
        await _run_pass_with_queue_reservations(scheduler, db_session)
    mock_pm.start_calibration.assert_not_called()
    await db_session.refresh(run)
    assert run.status == "pending"
    assert run.waiting_reason == "printer_busy"


@pytest.mark.asyncio
async def test_queue_reservations_on_another_printer_do_not_hold_it(scheduler, db_session, printer_factory):
    item = await _make_item(db_session, printer_factory)
    other = await printer_factory(name="Other")
    run = await _make_run(db_session, item)
    db_session.add(PrintQueueItem(printer_id=other.id, status="printing"))
    await db_session.commit()
    scheduler._inflight[998] = (MagicMock(), other.id)
    with patch("backend.app.services.print_scheduler.printer_manager") as mock_pm:
        mock_pm.get_status.return_value = _mock_state("IDLE")
        mock_pm.is_connected.return_value = True
        mock_pm.start_calibration.return_value = True
        await _run_pass_with_queue_reservations(scheduler, db_session)
    mock_pm.start_calibration.assert_called_once()
    await db_session.refresh(run)
    assert run.status == "running"


@pytest.mark.asyncio
async def test_plate_not_released_waits_with_its_own_reason(scheduler, db_session, printer_factory):
    """The gate is the whole point of "as soon as the plate is released": it
    must be named on the card rather than bucketed into "busy"."""
    item = await _make_item(db_session, printer_factory)
    run = await _make_run(db_session, item)
    with patch("backend.app.services.print_scheduler.printer_manager") as mock_pm:
        mock_pm.get_status.return_value = _mock_state("FINISH")
        mock_pm.is_connected.return_value = True
        mock_pm.is_awaiting_plate_clear.return_value = True
        await scheduler._check_maintenance_runs(db_session, True)
    mock_pm.start_calibration.assert_not_called()
    await db_session.refresh(run)
    assert run.status == "pending"
    assert run.waiting_reason == "awaiting_plate_clear"


@pytest.mark.asyncio
async def test_plate_gate_off_dispatches_on_finish_printer(scheduler, db_session, printer_factory):
    item = await _make_item(db_session, printer_factory)
    run = await _make_run(db_session, item)
    with patch("backend.app.services.print_scheduler.printer_manager") as mock_pm:
        mock_pm.get_status.return_value = _mock_state("FINISH")
        mock_pm.is_connected.return_value = True
        mock_pm.is_awaiting_plate_clear.return_value = True  # ignored: the setting is off
        mock_pm.start_calibration.return_value = True
        await scheduler._check_maintenance_runs(db_session, False)
    mock_pm.start_calibration.assert_called_once()
    await db_session.refresh(run)
    assert run.status == "running"
    assert run.started_at is not None
    assert run.waiting_reason is None
    assert scheduler._calibrating_printer_ids == {item.printer_id}


@pytest.mark.asyncio
async def test_dispatch_sends_the_runs_options_including_bits_0_and_6(scheduler, db_session, printer_factory):
    item = await _make_item(db_session, printer_factory)
    run = await _make_run(
        db_session,
        item,
        options={"bed_leveling": True, "micro_lidar": True, "nozzle_clumping": True},
    )
    with (
        patch("backend.app.services.print_scheduler.printer_manager") as mock_pm,
        patch.object(scheduler, "_is_printer_idle", return_value=True),
    ):
        mock_pm.get_status.return_value = _mock_state()
        mock_pm.start_calibration.return_value = True
        await scheduler._check_maintenance_runs(db_session, True)
    mock_pm.start_calibration.assert_called_once_with(
        item.printer_id,
        micro_lidar=True,
        bed_leveling=True,
        vibration=False,
        motor_noise=False,
        nozzle_offset=False,
        high_temp_heatbed=False,
        nozzle_clumping=True,
    )
    await db_session.refresh(run)
    assert run.status == "running"


@pytest.mark.asyncio
async def test_bitmask_reaches_the_wire_with_bits_0_and_6():
    """The client's bitmask: bit 0 Micro Lidar, bits 1-3 the defaults, bit 6 nozzle clumping."""
    import json

    from backend.app.services.bambu_mqtt import BambuMQTTClient

    client = BambuMQTTClient.__new__(BambuMQTTClient)
    client._client = MagicMock()
    client.state = MagicMock(connected=True)
    client._sequence_id = 0
    client.serial_number = "TEST"

    assert client.start_calibration(
        bed_leveling=True, vibration=True, motor_noise=True, micro_lidar=True, nozzle_clumping=True
    )
    payload = json.loads(client._client.publish.call_args.args[1])
    assert payload["print"]["command"] == "calibration"
    assert payload["print"]["option"] == (1 << 0) | (1 << 1) | (1 << 2) | (1 << 3) | (1 << 6)


@pytest.mark.asyncio
async def test_publish_failure_keeps_pending_as_offline(scheduler, db_session, printer_factory):
    item = await _make_item(db_session, printer_factory)
    run = await _make_run(db_session, item)
    with (
        patch("backend.app.services.print_scheduler.printer_manager") as mock_pm,
        patch.object(scheduler, "_is_printer_idle", return_value=True),
    ):
        mock_pm.get_status.return_value = _mock_state()
        mock_pm.start_calibration.return_value = False
        await scheduler._check_maintenance_runs(db_session, True)
    await db_session.refresh(run)
    assert run.status == "pending"
    assert run.waiting_reason == "printer_offline"


@pytest.mark.asyncio
async def test_drying_in_progress_defers(scheduler, db_session, printer_factory):
    item = await _make_item(db_session, printer_factory)
    run = await _make_run(db_session, item)
    scheduler._drying_in_progress[item.printer_id] = 1.0
    with (
        patch("backend.app.services.print_scheduler.printer_manager") as mock_pm,
        patch.object(scheduler, "_is_printer_idle", return_value=True),
    ):
        mock_pm.get_status.return_value = _mock_state()
        await scheduler._check_maintenance_runs(db_session, True)
    mock_pm.start_calibration.assert_not_called()
    await db_session.refresh(run)
    assert run.waiting_reason == "already_drying"


@pytest.mark.asyncio
async def test_a_running_run_blocks_a_second_one_on_the_same_printer(scheduler, db_session, printer_factory):
    item = await _make_item(db_session, printer_factory)
    await _make_run(db_session, item, status="running", started_at=_utcnow_naive())
    other_type = MaintenanceType(name="Other", default_interval_hours=10, is_system=False, action="calibration")
    db_session.add(other_type)
    await db_session.flush()
    other_item = PrinterMaintenance(printer_id=item.printer_id, maintenance_type_id=other_type.id, enabled=True)
    db_session.add(other_item)
    await db_session.commit()
    second = await _make_run(db_session, other_item)
    with (
        patch("backend.app.services.print_scheduler.printer_manager") as mock_pm,
        patch.object(scheduler, "_is_printer_idle", return_value=True),
    ):
        mock_pm.get_status.return_value = _mock_state()
        await scheduler._check_maintenance_runs(db_session, True)
    mock_pm.start_calibration.assert_not_called()
    await db_session.refresh(second)
    assert second.waiting_reason == "printer_busy"


@pytest.mark.asyncio
async def test_stale_running_run_is_failed(scheduler, db_session, printer_factory):
    item = await _make_item(db_session, printer_factory)
    run = await _make_run(db_session, item, status="running", started_at=_utcnow_naive() - timedelta(hours=3))
    with patch("backend.app.services.print_scheduler.printer_manager") as mock_pm:
        mock_pm.get_status.return_value = _mock_state()
        await scheduler._check_maintenance_runs(db_session, True)
    await db_session.refresh(run)
    assert run.status == "failed"
    assert run.completed_at is not None
    assert "Lost track" in run.error_message


@pytest.mark.asyncio
async def test_recently_dispatched_printer_is_busy_only_briefly(scheduler, db_session, printer_factory):
    item = await _make_item(db_session, printer_factory)
    await _make_run(db_session, item, status="running", started_at=_utcnow_naive() - timedelta(minutes=10))
    with patch("backend.app.services.print_scheduler.printer_manager") as mock_pm:
        mock_pm.get_status.return_value = _mock_state()
        await scheduler._check_maintenance_runs(db_session, True)
    assert scheduler._calibrating_printer_ids == set()


# ============== Triggers ==============


@pytest.mark.asyncio
async def test_when_due_creates_exactly_one_run(db_session, printer_factory):
    item = await _make_item(
        db_session,
        printer_factory,
        trigger_mode="when_due",
        last_performed_hours=0.0,
    )
    # 150 h of runtime against a 100 h interval: due.
    printer = await db_session.get(Printer, item.printer_id)
    printer.runtime_seconds = 150 * 3600
    await db_session.commit()

    created = await maintenance_actions.queue_triggered_runs(db_session)
    await db_session.commit()
    assert len(created) == 1
    assert created[0].source == "due"
    assert maintenance_actions.selected_calibration_flags(created[0].options) == [
        "bed_leveling",
        "vibration",
        "motor_noise",
    ]

    # A second pass finds the pending run and adds nothing.
    assert await maintenance_actions.queue_triggered_runs(db_session) == []
    runs = (await db_session.execute(select(MaintenanceRun))).scalars().all()
    assert len(runs) == 1


@pytest.mark.asyncio
async def test_when_due_does_not_retry_a_failed_run_of_the_same_due_period(db_session, printer_factory):
    item = await _make_item(db_session, printer_factory, trigger_mode="when_due", last_performed_hours=0.0)
    printer = await db_session.get(Printer, item.printer_id)
    printer.runtime_seconds = 150 * 3600
    await db_session.commit()
    await _make_run(db_session, item, status="failed", completed_at=_utcnow_naive())

    assert await maintenance_actions.queue_triggered_runs(db_session) == []


@pytest.mark.asyncio
async def test_when_due_not_due_creates_nothing(db_session, printer_factory):
    item = await _make_item(db_session, printer_factory, trigger_mode="when_due", last_performed_hours=0.0)
    printer = await db_session.get(Printer, item.printer_id)
    printer.runtime_seconds = 10 * 3600
    await db_session.commit()
    assert await maintenance_actions.queue_triggered_runs(db_session) == []


@pytest.mark.asyncio
async def test_disabled_and_manual_items_are_ignored(db_session, printer_factory):
    await _make_item(db_session, printer_factory, trigger_mode="when_due", enabled=False)
    item = await _make_item(db_session, printer_factory, trigger_mode="manual")
    printer = await db_session.get(Printer, item.printer_id)
    printer.runtime_seconds = 999 * 3600
    await db_session.commit()
    assert await maintenance_actions.queue_triggered_runs(db_session) == []


@pytest.mark.asyncio
async def test_schedule_creates_a_run_and_advances_next_at(db_session, printer_factory, monkeypatch):
    monkeypatch.setenv("TZ", "UTC")
    past = _utcnow_naive() - timedelta(minutes=5)
    item = await _make_item(
        db_session,
        printer_factory,
        trigger_mode="schedule",
        schedule_days=[0, 1, 2, 3, 4, 5, 6],
        schedule_time="06:00",
        schedule_next_at=past,
    )
    created = await maintenance_actions.queue_triggered_runs(db_session)
    await db_session.commit()
    assert len(created) == 1
    assert created[0].source == "schedule"
    assert created[0].start_after == past
    await db_session.refresh(item)
    assert item.schedule_next_at is not None
    assert item.schedule_next_at > _utcnow_naive()
    assert (item.schedule_next_at.hour, item.schedule_next_at.minute) == (6, 0)


@pytest.mark.asyncio
async def test_schedule_not_yet_due_creates_nothing(db_session, printer_factory):
    await _make_item(
        db_session,
        printer_factory,
        trigger_mode="schedule",
        schedule_days=[5],
        schedule_time="06:00",
        schedule_next_at=_utcnow_naive() + timedelta(days=1),
    )
    assert await maintenance_actions.queue_triggered_runs(db_session) == []


@pytest.mark.asyncio
async def test_schedule_without_next_at_is_seeded_not_fired(db_session, printer_factory):
    """A row saved before the scheduler saw it gets its next occurrence and waits for it."""
    item = await _make_item(
        db_session, printer_factory, trigger_mode="schedule", schedule_days=[0, 1, 2, 3, 4, 5, 6], schedule_time="06:00"
    )
    assert await maintenance_actions.queue_triggered_runs(db_session) == []
    await db_session.commit()
    await db_session.refresh(item)
    assert item.schedule_next_at is not None


@pytest.mark.asyncio
async def test_trigger_with_no_flag_selected_creates_nothing(db_session, printer_factory):
    item = await _make_item(db_session, printer_factory, trigger_mode="when_due", action_options={})
    printer = await db_session.get(Printer, item.printer_id)
    printer.runtime_seconds = 999 * 3600
    await db_session.commit()
    assert await maintenance_actions.queue_triggered_runs(db_session) == []


# ============== Completion ==============


@pytest.fixture
def completion_session(test_engine):
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    maker = async_sessionmaker(test_engine, class_=AsyncSession, expire_on_commit=False)
    with patch("backend.app.services.maintenance_actions.async_session", maker):
        yield maker


@pytest.mark.asyncio
async def test_finish_completes_the_run_and_performs_the_item(db_session, printer_factory, completion_session):
    item = await _make_item(db_session, printer_factory, last_performed_hours=0.0)
    printer = await db_session.get(Printer, item.printer_id)
    printer.runtime_seconds = 120 * 3600
    await db_session.commit()
    run = await _make_run(db_session, item, status="running", started_at=_utcnow_naive())

    with patch("backend.app.services.mqtt_relay.mqtt_relay") as relay:
        relay.on_maintenance_reset = MagicMock(return_value=_coro(None))
        closed = await maintenance_actions.on_internal_job_finished(
            item.printer_id,
            "/usr/etc/print/X1C/auto_cali_for_user_param.gcode",
            "auto_cali_for_user_param.gcode",
            "completed",
            None,
        )
    assert closed is True

    await db_session.refresh(run)
    await db_session.refresh(item)
    assert run.status == "completed"
    assert run.completed_at is not None
    assert item.last_performed_hours == pytest.approx(120)
    assert item.last_performed_at is not None
    assert item.last_auto_run_at is not None
    history = (
        (
            await db_session.execute(
                select(MaintenanceHistory).where(MaintenanceHistory.printer_maintenance_id == item.id)
            )
        )
        .scalars()
        .all()
    )
    assert len(history) == 1
    assert history[0].notes == maintenance_actions.AUTO_RUN_NOTES
    assert history[0].hours_at_maintenance == pytest.approx(120)


@pytest.mark.asyncio
async def test_cancel_from_the_printer_screen_cancels_the_run(db_session, printer_factory, completion_session):
    item = await _make_item(db_session, printer_factory)
    run = await _make_run(db_session, item, status="running", started_at=_utcnow_naive())
    assert await maintenance_actions.on_internal_job_finished(
        item.printer_id, "/usr/etc/print/X1C/auto_cali_for_user_param.gcode", None, "failed", 50348044
    )
    await db_session.refresh(run)
    await db_session.refresh(item)
    assert run.status == "cancelled"
    assert item.last_performed_at is None
    history = (await db_session.execute(select(MaintenanceHistory))).scalars().all()
    assert history == []


# The H2S capture (tap-0938BJ611001133-20260920-103100.jsonl, lines 1094 and
# 1100): a cancel from the printer screen first reports FAILED with
# print_error 0 and the 0300_400C code only seconds later.
_CAPTURE_RUNNING = {
    "print": {
        "command": "push_status",
        "gcode_state": "RUNNING",
        "gcode_file": "/usr/etc/print/O1S/auto_cali_for_user_param.gcode",
        "subtask_name": "auto_cali_for_user_param.gcode",
        "print_type": "system",
        "print_error": 0,
        "mc_percent": 10,
    }
}
_CAPTURE_FAILED_NO_CODE = {"print": {**_CAPTURE_RUNNING["print"], "gcode_state": "FAILED", "mc_percent": 0}}
_CAPTURE_FAILED_CANCEL_CODE = {"print": {**_CAPTURE_FAILED_NO_CODE["print"], "print_error": 50348044}}


def _client_with_completion_capture():
    from backend.app.services.bambu_mqtt import BambuMQTTClient

    client = BambuMQTTClient(ip_address="192.168.1.100", serial_number="0938BJ611001133", access_code="12345678")
    completions: list[dict] = []
    client.on_print_start = lambda data: None
    client.on_print_complete = completions.append
    return client, completions


def test_the_capture_fires_completion_before_the_cancel_code_arrives():
    """Replays the real ordering: the completion payload carries no code, and
    the code that follows is stamped on the state rather than dropped."""
    client, completions = _client_with_completion_capture()
    client._process_message(_CAPTURE_RUNNING)
    client._process_message(_CAPTURE_FAILED_NO_CODE)

    assert len(completions) == 1
    assert completions[0]["status"] == "failed"
    assert completions[0]["raw_data"]["print_error"] == 0
    assert maintenance_actions.resolve_run_outcome("failed", None) == "failed"
    assert client.state.last_cancel_echo_at is None

    client._process_message(_CAPTURE_FAILED_CANCEL_CODE)
    assert len(completions) == 1
    assert client.state.last_cancel_echo_at is not None
    assert client.state.hms_errors == []  # still not a fault


def test_a_cancel_echo_in_the_hms_array_is_stamped_too():
    client, _ = _client_with_completion_capture()
    client._process_message({"print": {"hms": [{"attr": 0x05000000, "code": 0x0000400E}]}})
    assert client.state.last_cancel_echo_at is not None
    assert client.state.hms_errors == []


class TestCancelEchoSeen:
    def _state(self, echo_at):
        state = MagicMock()
        state.last_cancel_echo_at = echo_at
        return state

    def test_echo_after_the_edge(self):
        with patch("backend.app.services.printer_manager.printer_manager") as pm:
            pm.get_status.return_value = self._state(107.0)
            assert maintenance_actions.cancel_echo_seen(1, 100.0, now=115.0)

    def test_echo_shortly_before_the_edge(self):
        with patch("backend.app.services.printer_manager.printer_manager") as pm:
            pm.get_status.return_value = self._state(95.0)
            assert maintenance_actions.cancel_echo_seen(1, 100.0, now=115.0)

    def test_an_old_echo_from_an_earlier_print_does_not_count(self):
        with patch("backend.app.services.printer_manager.printer_manager") as pm:
            pm.get_status.return_value = self._state(100.0 - maintenance_actions.CANCEL_ECHO_LOOKBACK_SECONDS - 1)
            assert not maintenance_actions.cancel_echo_seen(1, 100.0, now=115.0)

    def test_no_echo_and_no_state(self):
        with patch("backend.app.services.printer_manager.printer_manager") as pm:
            pm.get_status.return_value = self._state(None)
            assert not maintenance_actions.cancel_echo_seen(1, 100.0, now=115.0)
            pm.get_status.return_value = None
            assert not maintenance_actions.cancel_echo_seen(1, 100.0, now=115.0)


@pytest.mark.asyncio
async def test_failed_without_a_code_is_cancelled_once_the_echo_arrives(
    db_session, printer_factory, completion_session
):
    """The capture's sequence end to end: FAILED with print_error 0 closes
    nothing yet; after the grace the stamped echo turns the run cancelled."""
    item = await _make_item(db_session, printer_factory)
    run = await _make_run(db_session, item, status="running", started_at=_utcnow_naive())
    state = MagicMock()
    state.last_cancel_echo_at = None

    async def sleep_then_echo(_seconds):
        state.last_cancel_echo_at = 100.0 + 7.0  # the code lands during the grace

    with (
        patch("backend.app.services.printer_manager.printer_manager") as pm,
        patch("backend.app.services.maintenance_actions.asyncio.sleep", sleep_then_echo),
    ):
        pm.get_status.return_value = state
        closed = await maintenance_actions.on_internal_job_failed(
            item.printer_id,
            "/usr/etc/print/O1S/auto_cali_for_user_param.gcode",
            "auto_cali_for_user_param.gcode",
            100.0,
        )
    assert closed is True
    await db_session.refresh(run)
    await db_session.refresh(item)
    assert run.status == "cancelled"
    assert run.error_message is None
    assert item.last_performed_at is None


@pytest.mark.asyncio
async def test_failed_without_a_code_and_no_echo_is_a_failure(db_session, printer_factory, completion_session):
    item = await _make_item(db_session, printer_factory)
    run = await _make_run(db_session, item, status="running", started_at=_utcnow_naive())
    state = MagicMock()
    state.last_cancel_echo_at = None

    async def no_sleep(_seconds):
        return None

    with (
        patch("backend.app.services.printer_manager.printer_manager") as pm,
        patch("backend.app.services.maintenance_actions.asyncio.sleep", no_sleep),
    ):
        pm.get_status.return_value = state
        assert await maintenance_actions.on_internal_job_failed(
            item.printer_id, "/usr/etc/print/O1S/auto_cali_for_user_param.gcode", None, 100.0
        )
    await db_session.refresh(run)
    assert run.status == "failed"
    assert run.error_message == "Calibration failed"


@pytest.mark.asyncio
async def test_failed_grace_ignores_jobs_that_are_not_the_calibration(db_session, printer_factory, completion_session):
    item = await _make_item(db_session, printer_factory)
    run = await _make_run(db_session, item, status="running", started_at=_utcnow_naive())
    with patch("backend.app.services.maintenance_actions.asyncio.sleep") as sleep:
        assert not await maintenance_actions.on_internal_job_failed(item.printer_id, "", "auto_pa_line_calib_mode", 1.0)
    sleep.assert_not_called()
    await db_session.refresh(run)
    assert run.status == "running"


@pytest.mark.asyncio
async def test_a_second_active_run_for_the_same_item_is_rejected_by_the_database(db_session, printer_factory):
    """Two "Run now" clicks racing past the route's read: the partial unique
    index lets exactly one active row exist. Finished runs pile up freely."""
    from sqlalchemy.exc import IntegrityError

    item = await _make_item(db_session, printer_factory)
    await _make_run(db_session, item, status="completed")
    await _make_run(db_session, item, status="failed")
    await _make_run(db_session, item, status="pending")
    db_session.add(MaintenanceRun(printer_maintenance_id=item.id, printer_id=item.printer_id, status="running"))
    with pytest.raises(IntegrityError):
        await db_session.flush()


@pytest.mark.asyncio
async def test_other_failure_fails_the_run_with_the_error(db_session, printer_factory, completion_session):
    item = await _make_item(db_session, printer_factory)
    run = await _make_run(db_session, item, status="running", started_at=_utcnow_naive())
    assert await maintenance_actions.on_internal_job_finished(
        item.printer_id, "/usr/etc/print/X1C/auto_cali_for_user_param.gcode", None, "failed", 83886081
    )
    await db_session.refresh(run)
    assert run.status == "failed"
    assert "83886081" in run.error_message


@pytest.mark.asyncio
async def test_pressure_advance_line_does_not_close_a_run(db_session, printer_factory, completion_session):
    item = await _make_item(db_session, printer_factory)
    run = await _make_run(db_session, item, status="running", started_at=_utcnow_naive())
    assert not await maintenance_actions.on_internal_job_finished(
        item.printer_id, "", "auto_pa_line_calib_mode", "completed", None
    )
    await db_session.refresh(run)
    assert run.status == "running"


@pytest.mark.asyncio
async def test_a_manual_calibration_with_nothing_waiting_is_ignored(db_session, printer_factory, completion_session):
    printer = await printer_factory()
    assert not await maintenance_actions.on_internal_job_finished(
        printer.id, "/usr/etc/print/X1C/auto_cali_for_user_param.gcode", None, "completed", None
    )


async def _coro(value):
    return value


# ============== Vision encoder (motion precision) runs ==============


_MOTION_GCODE = "/usr/etc/print/O1S/calibrate_motion_precision.gcode"
_LEVELLING_GCODE = "/usr/etc/print/O1S/auto_cali_for_user_param.gcode"


@pytest.mark.asyncio
async def test_motion_precision_dispatch_uses_the_learned_directory(scheduler, db_session, printer_factory):
    item = await _make_item(db_session, printer_factory, action="motion_precision", model="H2S")
    run = await _make_run(db_session, item)
    with (
        patch("backend.app.services.print_scheduler.printer_manager") as mock_pm,
        patch.object(scheduler, "_is_printer_idle", return_value=True),
    ):
        state = _mock_state()
        state.internal_gcode_dir = "O1E"
        mock_pm.get_status.return_value = state
        mock_pm.start_internal_gcode_file.return_value = True
        mock_pm.await_internal_gcode_ack = AsyncMock(return_value=(True, ""))
        await scheduler._check_maintenance_runs(db_session, True)
    mock_pm.start_internal_gcode_file.assert_called_once_with(
        item.printer_id, "/usr/etc/print/O1E/calibrate_motion_precision.gcode"
    )
    mock_pm.start_calibration.assert_not_called()
    await db_session.refresh(run)
    assert run.status == "running"
    assert run.started_at is not None
    assert run.options is None


@pytest.mark.asyncio
async def test_motion_precision_dispatch_falls_back_to_the_model_map(scheduler, db_session, printer_factory):
    item = await _make_item(db_session, printer_factory, action="motion_precision", model="H2D")
    run = await _make_run(db_session, item)
    with (
        patch("backend.app.services.print_scheduler.printer_manager") as mock_pm,
        patch.object(scheduler, "_is_printer_idle", return_value=True),
    ):
        state = _mock_state()
        state.internal_gcode_dir = None
        mock_pm.get_status.return_value = state
        mock_pm.start_internal_gcode_file.return_value = True
        mock_pm.await_internal_gcode_ack = AsyncMock(return_value=(True, ""))
        await scheduler._check_maintenance_runs(db_session, True)
    mock_pm.start_internal_gcode_file.assert_called_once_with(item.printer_id, _MOTION_GCODE.replace("O1S", "O1D"))
    await db_session.refresh(run)
    assert run.status == "running"


@pytest.mark.asyncio
async def test_motion_precision_is_refused_on_a_printer_without_a_vision_encoder(
    scheduler, db_session, printer_factory
):
    item = await _make_item(db_session, printer_factory, action="motion_precision", model="X1C")
    run = await _make_run(db_session, item)
    with (
        patch("backend.app.services.print_scheduler.printer_manager") as mock_pm,
        patch.object(scheduler, "_is_printer_idle", return_value=True),
    ):
        mock_pm.get_status.return_value = _mock_state()
        await scheduler._check_maintenance_runs(db_session, True)
    mock_pm.start_internal_gcode_file.assert_not_called()
    mock_pm.start_calibration.assert_not_called()
    await db_session.refresh(run)
    assert run.status == "failed"
    assert run.completed_at is not None
    assert "H2-series" in run.error_message
    assert "X1C" in run.error_message


@pytest.mark.asyncio
async def test_motion_precision_publish_failure_keeps_pending_as_offline(scheduler, db_session, printer_factory):
    item = await _make_item(db_session, printer_factory, action="motion_precision", model="H2S")
    run = await _make_run(db_session, item)
    with (
        patch("backend.app.services.print_scheduler.printer_manager") as mock_pm,
        patch.object(scheduler, "_is_printer_idle", return_value=True),
    ):
        mock_pm.get_status.return_value = _mock_state()
        mock_pm.start_internal_gcode_file.return_value = False
        await scheduler._check_maintenance_runs(db_session, True)
    await db_session.refresh(run)
    assert run.status == "pending"
    assert run.waiting_reason == "printer_offline"


@pytest.mark.asyncio
async def test_a_refused_gcode_file_fails_the_run_at_once(scheduler, db_session, printer_factory):
    """The printer's reply is the only immediate sign that the directory was
    guessed wrong; the run must not sit "running" until the stale sweep."""
    item = await _make_item(db_session, printer_factory, action="motion_precision", model="H2D")
    run = await _make_run(db_session, item)
    with (
        patch("backend.app.services.print_scheduler.printer_manager") as mock_pm,
        patch.object(scheduler, "_is_printer_idle", return_value=True),
    ):
        state = _mock_state()
        state.internal_gcode_dir = None
        mock_pm.get_status.return_value = state
        mock_pm.start_internal_gcode_file.return_value = True
        mock_pm.await_internal_gcode_ack = AsyncMock(return_value=(False, "err_code 1, file not found"))
        await scheduler._check_maintenance_runs(db_session, True)
    mock_pm.await_internal_gcode_ack.assert_awaited_once_with(item.printer_id, _MOTION_GCODE.replace("O1S", "O1D"))
    await db_session.refresh(run)
    assert run.status == "failed"
    assert run.completed_at is not None
    assert run.waiting_reason is None
    assert "/usr/etc/print/O1D/calibrate_motion_precision.gcode" in run.error_message
    assert "err_code 1, file not found" in run.error_message
    assert scheduler._calibrating_printer_ids == set()


@pytest.mark.asyncio
async def test_silence_after_the_gcode_file_command_leaves_the_run_running(scheduler, db_session, printer_factory):
    item = await _make_item(db_session, printer_factory, action="motion_precision", model="H2S")
    run = await _make_run(db_session, item)
    with (
        patch("backend.app.services.print_scheduler.printer_manager") as mock_pm,
        patch.object(scheduler, "_is_printer_idle", return_value=True),
    ):
        mock_pm.get_status.return_value = _mock_state()
        mock_pm.start_internal_gcode_file.return_value = True
        mock_pm.await_internal_gcode_ack = AsyncMock(return_value=(True, "no acknowledgement from printer"))
        await scheduler._check_maintenance_runs(db_session, True)
    await db_session.refresh(run)
    assert run.status == "running"
    assert scheduler._calibrating_printer_ids == {item.printer_id}


def _wire_client():
    from backend.app.services.bambu_mqtt import BambuMQTTClient

    client = BambuMQTTClient(ip_address="192.168.1.100", serial_number="TEST", access_code="12345678")
    client.state.connected = True
    client._client = MagicMock()
    return client


def _gcode_file_reply(path, err_code=0, result="SUCCESS", reason="SUCCESS"):
    # Shape of the H2S reply in the capture: the path comes back as param.
    return {
        "print": {
            "command": "gcode_file",
            "err_code": err_code,
            "is_from_mqtt": True,
            "param": path,
            "reason": reason,
            "result": result,
            "sequence_id": "2",
        }
    }


@pytest.mark.asyncio
async def test_the_success_reply_acknowledges_the_file():
    client = _wire_client()
    assert client.start_internal_gcode_file(_MOTION_GCODE)
    assert _MOTION_GCODE in client._pending_gcode_file_acks
    client._process_message(_gcode_file_reply(_MOTION_GCODE))
    ok, detail = await client.await_internal_gcode_ack(_MOTION_GCODE, timeout=2.0)
    assert ok is True
    assert _MOTION_GCODE not in client._pending_gcode_file_acks


@pytest.mark.asyncio
async def test_a_refusal_reply_is_reported_with_its_code():
    client = _wire_client()
    wrong = "/usr/etc/print/O1D/calibrate_motion_precision.gcode"
    assert client.start_internal_gcode_file(wrong)
    client._process_message(_gcode_file_reply(wrong, err_code=1, result="FAIL", reason="file not found"))
    ok, detail = await client.await_internal_gcode_ack(wrong, timeout=2.0)
    assert ok is False
    assert detail == "err_code 1, file not found"


@pytest.mark.asyncio
async def test_a_reply_for_another_file_does_not_resolve_this_one():
    client = _wire_client()
    assert client.start_internal_gcode_file(_MOTION_GCODE)
    client._process_message(_gcode_file_reply(_LEVELLING_GCODE, err_code=1, result="FAIL", reason="file not found"))
    ok, detail = await client.await_internal_gcode_ack(_MOTION_GCODE, timeout=0.3)
    assert ok is True
    assert "no acknowledgement" in detail
    assert _MOTION_GCODE not in client._pending_gcode_file_acks


@pytest.mark.asyncio
async def test_no_client_means_no_verdict():
    from backend.app.services.printer_manager import PrinterManager

    manager = PrinterManager()
    assert await manager.await_internal_gcode_ack(999, _MOTION_GCODE) == (True, "no acknowledgement from printer")


def test_the_gcode_file_command_reaches_the_wire():
    """The command as captured on the H2S: print.command gcode_file, param = path."""
    import json

    from backend.app.services.bambu_mqtt import BambuMQTTClient

    client = BambuMQTTClient.__new__(BambuMQTTClient)
    client._client = MagicMock()
    client.state = MagicMock(connected=True)
    client._sequence_id = 0
    client.serial_number = "TEST"
    client._pending_gcode_file_acks = {}

    assert client.start_internal_gcode_file(_MOTION_GCODE)
    payload = json.loads(client._client.publish.call_args.args[1])
    assert payload["print"]["command"] == "gcode_file"
    assert payload["print"]["param"] == _MOTION_GCODE
    assert payload["print"]["sequence_id"] == "1"


def test_only_system_paths_can_be_started_this_way():
    from backend.app.services.bambu_mqtt import BambuMQTTClient

    client = BambuMQTTClient.__new__(BambuMQTTClient)
    client._client = MagicMock()
    client.state = MagicMock(connected=True)
    client._sequence_id = 0
    client.serial_number = "TEST"

    assert not client.start_internal_gcode_file("/data/Metadata/plate_1.gcode")
    assert not client.start_internal_gcode_file("usr/etc/print/O1S/calibrate_motion_precision.gcode")
    client._client.publish.assert_not_called()


def test_the_internal_gcode_directory_is_learned_from_push_status():
    from backend.app.services.bambu_mqtt import BambuMQTTClient, _internal_gcode_dir

    assert _internal_gcode_dir(_LEVELLING_GCODE) == "O1S"
    assert _internal_gcode_dir("/usr/etc/print/O1D/calibrate_motion_precision.gcode") == "O1D"
    assert _internal_gcode_dir("/data/Metadata/plate_1.gcode") is None
    assert _internal_gcode_dir("/usr/etc/print/x.gcode") is None
    assert _internal_gcode_dir("/usr/etc/print/O1S/sub/x.gcode") is None
    assert _internal_gcode_dir(None) is None

    client = BambuMQTTClient(ip_address="192.168.1.100", serial_number="TEST123", access_code="12345678")
    assert client.state.internal_gcode_dir is None
    client._update_state({"gcode_file": "/data/Metadata/plate_1.gcode"})
    assert client.state.internal_gcode_dir is None
    client._update_state({"gcode_file": _LEVELLING_GCODE})
    assert client.state.internal_gcode_dir == "O1S"
    # A user's print afterwards does not forget it
    client._update_state({"gcode_file": "Benchy.gcode.3mf"})
    assert client.state.internal_gcode_dir == "O1S"


@pytest.mark.asyncio
async def test_completion_closes_only_the_run_of_the_matching_action(db_session, printer_factory, completion_session):
    """Two running runs on one printer -- one per action, which cannot happen
    on real hardware but pins the matching: the vision encoder job closes the
    motion_precision run and leaves the calibration run alone."""
    levelling = await _make_item(db_session, printer_factory, model="H2S")
    motion = await _make_item(db_session, printer_factory, action="motion_precision", printer_id=levelling.printer_id)
    levelling_run = await _make_run(db_session, levelling, status="running", started_at=_utcnow_naive())
    motion_run = await _make_run(
        db_session, motion, status="running", started_at=_utcnow_naive() + timedelta(seconds=5)
    )

    with patch("backend.app.services.mqtt_relay.mqtt_relay") as relay:
        relay.on_maintenance_reset = MagicMock(return_value=_coro(None))
        assert await maintenance_actions.on_internal_job_finished(
            levelling.printer_id, _MOTION_GCODE, "calibrate_motion_precision.gcode", "completed", None
        )
    await db_session.refresh(levelling_run)
    await db_session.refresh(motion_run)
    await db_session.refresh(motion)
    await db_session.refresh(levelling)
    assert motion_run.status == "completed"
    assert levelling_run.status == "running"
    assert motion.last_performed_at is not None
    assert levelling.last_performed_at is None

    with patch("backend.app.services.mqtt_relay.mqtt_relay") as relay:
        relay.on_maintenance_reset = MagicMock(return_value=_coro(None))
        assert await maintenance_actions.on_internal_job_finished(
            levelling.printer_id, _LEVELLING_GCODE, "auto_cali_for_user_param.gcode", "completed", None
        )
    await db_session.refresh(levelling_run)
    assert levelling_run.status == "completed"


@pytest.mark.asyncio
async def test_the_vision_encoder_job_does_not_close_a_calibration_run(db_session, printer_factory, completion_session):
    item = await _make_item(db_session, printer_factory, model="H2S")
    run = await _make_run(db_session, item, status="running", started_at=_utcnow_naive())
    assert not await maintenance_actions.on_internal_job_finished(
        item.printer_id, _MOTION_GCODE, "calibrate_motion_precision.gcode", "completed", None
    )
    await db_session.refresh(run)
    assert run.status == "running"


@pytest.mark.asyncio
async def test_a_screen_cancel_of_the_vision_encoder_run_is_cancelled(db_session, printer_factory, completion_session):
    item = await _make_item(db_session, printer_factory, action="motion_precision", model="H2S")
    run = await _make_run(db_session, item, status="running", started_at=_utcnow_naive())
    assert await maintenance_actions.on_internal_job_finished(
        item.printer_id, _MOTION_GCODE, "calibrate_motion_precision.gcode", "failed", 50348044
    )
    await db_session.refresh(run)
    assert run.status == "cancelled"
