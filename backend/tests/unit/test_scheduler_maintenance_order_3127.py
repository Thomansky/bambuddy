"""Order of maintenance runs on one printer, and the wording the print queue
borrows from them (#3127).

Two runs become pending at the same second on one printer every Sunday: the
levelling calibration and the vision encoder calibration. Which goes first
used to be an accident of row ids, and the loser reported a plain
``printer_busy``. Now the line is fixed -- action priority, ``start_after``,
id -- and everything behind the head of it says whose turn it is.
"""

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from backend.app.models.maintenance import MaintenanceRun, MaintenanceType, PrinterMaintenance
from backend.app.services import maintenance_actions
from backend.app.services.print_scheduler import PrintScheduler

pytestmark = pytest.mark.unit


def _utcnow_naive() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _mock_state(state="IDLE", connected=True, bed=20.0):
    mock = MagicMock()
    mock.state = state
    mock.connected = connected
    mock.temperatures = {"bed": bed}
    return mock


async def _make_type(db_session, name, action):
    maint_type = MaintenanceType(
        name=name, description="", default_interval_hours=100.0, interval_type="hours", is_system=True, action=action
    )
    db_session.add(maint_type)
    await db_session.flush()
    return maint_type


async def _make_item(db_session, printer_id, maint_type, **kwargs):
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
        "source": "schedule",
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


async def _sunday_pair(db_session, printer_factory, *, model="H2S", start_after=None):
    """The user's Sunday: both actions pending on one H2S at the same instant.

    The vision encoder run gets the lower id on purpose, so id order alone
    would put it first.
    """
    printer = await printer_factory(model=model)
    calibration_type = await _make_type(db_session, "Printer Calibration", "calibration")
    motion_type = await _make_type(db_session, "Vision Encoder Calibration", "motion_precision")
    calibration = await _make_item(db_session, printer.id, calibration_type)
    motion = await _make_item(db_session, printer.id, motion_type)
    motion_run = await _make_run(db_session, motion, start_after=start_after)
    calibration_run = await _make_run(db_session, calibration, start_after=start_after)
    assert motion_run.id < calibration_run.id
    return SimpleNamespace(printer=printer, calibration_run=calibration_run, motion_run=motion_run)


@pytest.fixture
def scheduler():
    return PrintScheduler()


class TestTheLineOnOnePrinter:
    @pytest.mark.asyncio
    async def test_the_calibration_goes_first_and_the_vision_encoder_run_waits_behind_it(
        self, scheduler, db_session, printer_factory
    ):
        pair = await _sunday_pair(db_session, printer_factory)
        with patch("backend.app.services.print_scheduler.printer_manager") as mock_pm:
            mock_pm.get_status.return_value = _mock_state()
            mock_pm.is_connected.return_value = True
            mock_pm.start_calibration.return_value = True
            await scheduler._check_maintenance_runs(db_session, False)
        mock_pm.start_calibration.assert_called_once()
        mock_pm.start_internal_gcode_file.assert_not_called()
        await db_session.refresh(pair.calibration_run)
        await db_session.refresh(pair.motion_run)
        assert pair.calibration_run.status == "running"
        assert pair.motion_run.status == "pending"
        assert pair.motion_run.waiting_reason == "after_other_run"
        assert pair.motion_run.waiting_detail == {"item": "Printer Calibration"}

    @pytest.mark.asyncio
    async def test_the_second_run_waits_by_name_while_the_first_is_held_too(
        self, scheduler, db_session, printer_factory
    ):
        """Only the head of the line is a candidate at all: with the printer
        offline the calibration waits for it, and the vision encoder run
        waits for the calibration, not for the printer."""
        pair = await _sunday_pair(db_session, printer_factory)
        with patch("backend.app.services.print_scheduler.printer_manager") as mock_pm:
            mock_pm.get_status.return_value = None
            await scheduler._check_maintenance_runs(db_session, False)
        await db_session.refresh(pair.calibration_run)
        await db_session.refresh(pair.motion_run)
        assert pair.calibration_run.waiting_reason == "printer_offline"
        assert pair.motion_run.waiting_reason == "after_other_run"
        assert pair.motion_run.waiting_detail == {"item": "Printer Calibration"}

    @pytest.mark.asyncio
    async def test_a_running_vision_encoder_run_heads_the_line_whatever_the_priority(
        self, scheduler, db_session, printer_factory
    ):
        pair = await _sunday_pair(db_session, printer_factory)
        pair.motion_run.status = "running"
        pair.motion_run.started_at = _utcnow_naive()
        await db_session.commit()
        with patch("backend.app.services.print_scheduler.printer_manager") as mock_pm:
            mock_pm.get_status.return_value = _mock_state()
            await scheduler._check_maintenance_runs(db_session, False)
        mock_pm.start_calibration.assert_not_called()
        await db_session.refresh(pair.calibration_run)
        assert pair.calibration_run.waiting_reason == "after_other_run"
        assert pair.calibration_run.waiting_detail == {"item": "Vision Encoder Calibration"}

    @pytest.mark.asyncio
    async def test_the_bed_condition_holds_the_vision_encoder_run_after_the_calibration(
        self, scheduler, db_session, printer_factory
    ):
        """The calibration has finished and heated the bed; the vision encoder
        run, now at the head, waits for the bed and not for anybody."""
        pair = await _sunday_pair(db_session, printer_factory)
        pair.calibration_run.status = "completed"
        pair.calibration_run.completed_at = _utcnow_naive()
        item = (await db_session.get(PrinterMaintenance, pair.motion_run.printer_maintenance_id)) or None
        assert item is not None
        item.action_options = {"bed_temp_below": 30}
        await db_session.commit()
        with patch("backend.app.services.print_scheduler.printer_manager") as mock_pm:
            mock_pm.get_status.return_value = _mock_state(bed=48.0)
            mock_pm.is_connected.return_value = True
            await scheduler._check_maintenance_runs(db_session, False)
        mock_pm.start_internal_gcode_file.assert_not_called()
        await db_session.refresh(pair.motion_run)
        assert pair.motion_run.waiting_reason == "bed_too_warm"
        assert pair.motion_run.waiting_detail == {"bed_temp": 48.0, "threshold": 30.0}

    @pytest.mark.asyncio
    async def test_a_run_whose_start_is_still_ahead_heads_nothing(self, scheduler, db_session, printer_factory):
        pair = await _sunday_pair(db_session, printer_factory)
        pair.calibration_run.start_after = _utcnow_naive() + timedelta(hours=1)
        await db_session.commit()
        with patch("backend.app.services.print_scheduler.printer_manager") as mock_pm:
            mock_pm.get_status.return_value = _mock_state()
            mock_pm.is_connected.return_value = True
            mock_pm.start_internal_gcode_file.return_value = True
            mock_pm.await_internal_gcode_ack = AsyncMock(return_value=(True, ""))
            await scheduler._check_maintenance_runs(db_session, False)
        mock_pm.start_internal_gcode_file.assert_called_once()
        await db_session.refresh(pair.motion_run)
        await db_session.refresh(pair.calibration_run)
        assert pair.motion_run.status == "running"
        assert pair.calibration_run.status == "pending"
        assert pair.calibration_run.waiting_reason is None

    @pytest.mark.asyncio
    async def test_runs_on_different_printers_do_not_queue_behind_each_other(
        self, scheduler, db_session, printer_factory
    ):
        first = await printer_factory(model="H2S", name="A")
        second = await printer_factory(model="H2S", name="B")
        calibration_type = await _make_type(db_session, "Printer Calibration", "calibration")
        run_a = await _make_run(db_session, await _make_item(db_session, first.id, calibration_type))
        run_b = await _make_run(db_session, await _make_item(db_session, second.id, calibration_type))
        with patch("backend.app.services.print_scheduler.printer_manager") as mock_pm:
            mock_pm.get_status.return_value = _mock_state()
            mock_pm.is_connected.return_value = True
            mock_pm.start_calibration.return_value = True
            await scheduler._check_maintenance_runs(db_session, False)
        assert mock_pm.start_calibration.call_count == 2
        await db_session.refresh(run_a)
        await db_session.refresh(run_b)
        assert (run_a.status, run_b.status) == ("running", "running")


class TestRunsNobodyOwnsAnyMore:
    """A pending run whose item was switched off, or whose type was hidden (#3127).

    Its card -- the only place with a Cancel button -- has left the
    maintenance page, so the run must not go on holding the print queue and
    must never reach the printer. The routes cancel it as it happens; this
    is the scheduler's own sweep, which also catches rows an older version
    left behind.
    """

    async def _pending_run(self, db_session, printer_factory, *, enabled=True, hidden=False, name="A"):
        printer = await printer_factory(model="X1C", name=name)
        maint_type = await _make_type(db_session, "Printer Calibration", "calibration")
        maint_type.is_deleted = hidden
        item = await _make_item(db_session, printer.id, maint_type, enabled=enabled)
        return printer, await _make_run(db_session, item)

    @pytest.mark.asyncio
    async def test_the_pass_cancels_the_run_of_a_switched_off_item(self, scheduler, db_session, printer_factory):
        _printer, run = await self._pending_run(db_session, printer_factory, enabled=False)
        with patch("backend.app.services.print_scheduler.printer_manager") as mock_pm:
            mock_pm.get_status.return_value = _mock_state()
            mock_pm.is_connected.return_value = True
            mock_pm.start_calibration.return_value = True
            await scheduler._check_maintenance_runs(db_session, False)
        mock_pm.start_calibration.assert_not_called()
        await db_session.refresh(run)
        assert run.status == "cancelled"
        assert run.waiting_reason is None
        assert run.completed_at is not None

    @pytest.mark.asyncio
    async def test_the_pass_cancels_the_run_of_a_hidden_type(self, scheduler, db_session, printer_factory):
        _printer, run = await self._pending_run(db_session, printer_factory, hidden=True)
        with patch("backend.app.services.print_scheduler.printer_manager") as mock_pm:
            mock_pm.get_status.return_value = _mock_state()
            mock_pm.is_connected.return_value = True
            mock_pm.start_calibration.return_value = True
            await scheduler._check_maintenance_runs(db_session, False)
        mock_pm.start_calibration.assert_not_called()
        await db_session.refresh(run)
        assert run.status == "cancelled"

    @pytest.mark.asyncio
    async def test_it_reserves_no_printer_while_it_is_still_pending(self, scheduler, db_session, printer_factory):
        printer, _run = await self._pending_run(db_session, printer_factory, enabled=False)
        assert await scheduler._maintenance_reserved_printers(db_session) == {}

        # The same run on an item that is on does reserve it.
        other, run = await self._pending_run(db_session, printer_factory, name="B")
        reserved = await scheduler._maintenance_reserved_printers(db_session)
        assert list(reserved) == [other.id]
        assert reserved[other.id].id == run.id
        assert printer.id not in reserved

    @pytest.mark.asyncio
    async def test_a_run_already_on_the_printer_still_reserves_it(self, scheduler, db_session, printer_factory):
        printer, run = await self._pending_run(db_session, printer_factory, enabled=False)
        run.status = "running"
        run.started_at = _utcnow_naive()
        await db_session.commit()

        # The calibration is on the machine whatever the item now says.
        assert list(await scheduler._maintenance_reserved_printers(db_session)) == [printer.id]


class TestHeadRuns:
    """The pure ordering rule, on plain objects."""

    @staticmethod
    def _run(run_id, action, status="pending", start_after=None, printer_id=1):
        return SimpleNamespace(
            id=run_id,
            printer_id=printer_id,
            status=status,
            start_after=start_after,
            printer_maintenance=SimpleNamespace(maintenance_type=SimpleNamespace(action=action, name=action)),
        )

    def test_action_priority_beats_id(self):
        now = _utcnow_naive()
        motion = self._run(1, "motion_precision")
        calibration = self._run(2, "calibration")
        assert maintenance_actions.head_runs([motion, calibration], now) == {1: calibration}

    def test_start_after_beats_id_within_one_action(self):
        now = _utcnow_naive()
        later = self._run(1, "calibration", start_after=now - timedelta(minutes=1))
        earlier = self._run(2, "calibration", start_after=now - timedelta(hours=1))
        assert maintenance_actions.head_runs([later, earlier], now) == {1: earlier}

    def test_no_start_after_sorts_before_any(self):
        now = _utcnow_naive()
        scheduled = self._run(1, "calibration", start_after=now - timedelta(hours=1))
        manual = self._run(2, "calibration")
        assert maintenance_actions.head_runs([scheduled, manual], now) == {1: manual}

    def test_a_running_run_heads_regardless_of_priority(self):
        now = _utcnow_naive()
        running = self._run(5, "motion_precision", status="running")
        pending = self._run(1, "calibration")
        assert maintenance_actions.head_runs([pending, running], now) == {1: running}

    def test_future_runs_never_head(self):
        now = _utcnow_naive()
        future = self._run(1, "calibration", start_after=now + timedelta(hours=1))
        assert maintenance_actions.head_runs([future], now) == {}

    def test_one_head_per_printer(self):
        now = _utcnow_naive()
        a = self._run(1, "calibration", printer_id=1)
        b = self._run(2, "calibration", printer_id=2)
        assert maintenance_actions.head_runs([a, b], now) == {1: a, 2: b}


def _hold_run(status="pending", reason=None, detail=None, name="Printer Calibration"):
    return SimpleNamespace(
        status=status,
        waiting_reason=reason,
        waiting_detail=detail,
        printer_maintenance=SimpleNamespace(maintenance_type=SimpleNamespace(name=name, action="calibration")),
    )


class TestQueueHoldWording:
    """The sentence a queue row shows for a printer a run reserves."""

    @pytest.mark.parametrize(
        "run, expected",
        [
            (_hold_run(), "Maintenance run pending: Printer Calibration (queued)"),
            (_hold_run(status="running"), "Maintenance run pending: Printer Calibration (running)"),
            (_hold_run(reason="printer_busy"), "Maintenance run pending: Printer Calibration (printer busy)"),
            (
                _hold_run(reason="awaiting_plate_clear"),
                "Maintenance run pending: Printer Calibration (plate not released yet)",
            ),
            (
                _hold_run(reason="bed_too_warm", detail={"bed_temp": 45.0, "threshold": 30.0}),
                "Maintenance run pending: Printer Calibration (bed still warm, 45 °C)",
            ),
            (
                _hold_run(
                    reason="bed_too_warm",
                    detail={"bed_temp": 34.2, "threshold": 30.0},
                    name="Vision Encoder Calibration",
                ),
                "Maintenance run pending: Vision Encoder Calibration (bed still warm, 34.2 °C)",
            ),
            (
                _hold_run(reason="bed_temp_unknown"),
                "Maintenance run pending: Printer Calibration (bed temperature unknown)",
            ),
        ],
    )
    def test_run_hold(self, run, expected):
        assert maintenance_actions.queue_hold_for_run(run) == expected

    def test_schedule_hold_names_the_slot_on_the_local_clock(self, monkeypatch):
        monkeypatch.setenv("TZ", "UTC")
        next_at = datetime(2026, 9, 27, 10, 0)  # a Sunday
        assert (
            maintenance_actions.queue_hold_for_schedule(next_at, 3 * 3600 + 20 * 60)
            == "Scheduled maintenance at Sunday 10:00 — this job would run into it (estimated 3h 20m)"
        )
        assert (
            maintenance_actions.queue_hold_for_schedule(next_at, None)
            == "Scheduled maintenance at Sunday 10:00 — this job would run into it (duration unknown)"
        )
        assert maintenance_actions.queue_hold_for_schedule(next_at, 45 * 60).endswith("(estimated 45m)")

    def test_schedule_hold_uses_the_configured_zone(self, monkeypatch):
        monkeypatch.setenv("TZ", "Europe/Berlin")
        next_at = datetime(2026, 9, 27, 10, 0)  # 12:00 in Berlin
        assert maintenance_actions.queue_hold_for_schedule(next_at, 600).startswith(
            "Scheduled maintenance at Sunday 12:00"
        )

    @pytest.mark.parametrize(
        "clause, busy_only",
        [
            ("Maintenance run pending: Printer Calibration (queued)", True),
            ("Scheduled maintenance at Sunday 12:00 — this job would run into it (estimated 3h 20m)", True),
            ("Busy: X1C-01 | Maintenance run pending: Printer Calibration (running)", True),
            ("Waiting for plate confirmation: X1C-01", False),
            ("Maintenance run pending: Printer Calibration (queued) | Offline: X1C-02", False),
        ],
    )
    def test_both_holds_count_as_self_resolving(self, clause, busy_only):
        assert PrintScheduler._is_busy_only(clause) is busy_only


class TestScheduleLookAhead:
    """When a job of a given length is allowed to start before the slot."""

    now = datetime(2026, 9, 27, 8, 0)
    slot = datetime(2026, 9, 27, 12, 0)

    def test_a_job_that_ends_before_the_margin_fits(self):
        assert maintenance_actions.schedule_hold_blocks(self.slot, self.now, 3 * 3600 + 45 * 60) is False

    def test_a_job_that_ends_inside_the_margin_is_held(self):
        assert maintenance_actions.schedule_hold_blocks(self.slot, self.now, 3 * 3600 + 46 * 60) is True

    def test_a_job_longer_than_the_time_left_is_held(self):
        assert maintenance_actions.schedule_hold_blocks(self.slot, self.now, 6 * 3600) is True

    def test_unknown_duration_is_held_from_two_hours_before(self):
        assert maintenance_actions.schedule_hold_blocks(self.slot, self.now, None) is False
        assert maintenance_actions.schedule_hold_blocks(self.slot, self.slot - timedelta(hours=2), None) is True
        assert (
            maintenance_actions.schedule_hold_blocks(self.slot, self.slot - timedelta(hours=2, minutes=1), 0) is False
        )

    def test_a_slot_that_has_passed_holds_everything(self):
        assert maintenance_actions.schedule_hold_blocks(self.slot, self.slot + timedelta(minutes=1), 60) is True


class TestMigration:
    @pytest.mark.asyncio
    async def test_reserve_before_schedule_is_added_once_with_a_true_default(self):
        from backend.app.core.database import _safe_execute

        sql = "ALTER TABLE printer_maintenance ADD COLUMN reserve_before_schedule BOOLEAN NOT NULL DEFAULT TRUE"
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        async with engine.begin() as conn:
            await conn.execute(text("CREATE TABLE printer_maintenance (id INTEGER PRIMARY KEY, printer_id INTEGER)"))
            await _safe_execute(conn, sql)
            await _safe_execute(conn, sql)  # a second start-up: swallowed, not raised
            await conn.execute(text("INSERT INTO printer_maintenance (id, printer_id) VALUES (1, 1)"))
            row = (await conn.execute(text("SELECT reserve_before_schedule FROM printer_maintenance"))).fetchone()
        await engine.dispose()
        assert row[0] == 1

    @pytest.mark.asyncio
    async def test_user_started_is_added_once_with_a_false_default(self):
        from backend.app.core.database import _safe_execute

        sql = "ALTER TABLE print_queue ADD COLUMN user_started BOOLEAN DEFAULT FALSE NOT NULL"
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        async with engine.begin() as conn:
            await conn.execute(text("CREATE TABLE print_queue (id INTEGER PRIMARY KEY, status TEXT)"))
            await _safe_execute(conn, sql)
            await _safe_execute(conn, sql)
            await conn.execute(text("INSERT INTO print_queue (id, status) VALUES (1, 'pending')"))
            row = (await conn.execute(text("SELECT user_started FROM print_queue"))).fetchone()
        await engine.dispose()
        assert row[0] == 0
