"""Tests for PrintScheduler maintenance calibration runs (#3127).

Same shape as the scheduled-drying tests: rows in the test database, the
printer manager mocked, ``_check_maintenance_runs`` driven directly.
"""

from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import pytest
from sqlalchemy import select

from backend.app.models.maintenance import MaintenanceHistory, MaintenanceRun, MaintenanceType, PrinterMaintenance
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


async def _make_item(db_session, printer_factory, *, action="calibration", **kwargs):
    printer = await printer_factory(model=kwargs.pop("model", "X1C"))
    maint_type = MaintenanceType(
        name="Printer Calibration",
        description="",
        default_interval_hours=100.0,
        interval_type="hours",
        is_system=True,
        action=action,
    )
    db_session.add(maint_type)
    await db_session.flush()
    defaults = {"printer_id": printer.id, "maintenance_type_id": maint_type.id, "enabled": True}
    defaults.update(kwargs)
    item = PrinterMaintenance(**defaults)
    db_session.add(item)
    await db_session.commit()
    await db_session.refresh(item)
    return item


async def _make_run(db_session, item, **kwargs):
    defaults = {
        "printer_maintenance_id": item.id,
        "printer_id": item.printer_id,
        "status": "pending",
        "source": "manual",
        "options": maintenance_actions.normalize_calibration_options(item.action_options),
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
