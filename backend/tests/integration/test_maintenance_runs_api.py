"""Integration tests for actionable maintenance: runs, triggers, overview fields (#3127)."""

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from httpx import AsyncClient
from sqlalchemy import select

from backend.app.models.maintenance import MaintenanceRun, MaintenanceType, PrinterMaintenance

pytestmark = [pytest.mark.asyncio, pytest.mark.integration]


async def _calibration_item(async_client: AsyncClient, printer_id: int) -> dict:
    """The overview auto-creates the item for the seeded system type."""
    response = await async_client.get(f"/api/v1/maintenance/printers/{printer_id}")
    assert response.status_code == 200, response.text
    items = [i for i in response.json()["maintenance_items"] if i["action"] == "calibration"]
    assert len(items) == 1
    return items[0]


class TestSeededType:
    async def test_printer_calibration_is_a_system_type_with_an_action(self, async_client):
        response = await async_client.get("/api/v1/maintenance/types")
        assert response.status_code == 200
        calibration = [t for t in response.json() if t["name"] == "Printer Calibration"]
        assert len(calibration) == 1
        assert calibration[0]["is_system"] is True
        assert calibration[0]["action"] == "calibration"
        assert calibration[0]["default_interval_hours"] == 100.0

    async def test_custom_types_carry_no_action(self, async_client):
        response = await async_client.post(
            "/api/v1/maintenance/types", json={"name": "Wipe Lens", "default_interval_hours": 10}
        )
        assert response.status_code == 200
        assert response.json()["action"] is None

    async def test_overview_exposes_the_action_fields(self, async_client, printer_factory):
        printer = await printer_factory(model="X1C")
        item = await _calibration_item(async_client, printer.id)
        assert item["trigger_mode"] == "manual"
        assert item["action_options"] == {
            "micro_lidar": False,
            "bed_leveling": True,
            "vibration": True,
            "motor_noise": True,
            "nozzle_offset": False,
            "high_temp_heatbed": False,
            "nozzle_clumping": False,
        }
        assert "nozzle_offset" not in item["action_available_options"]
        assert item["current_run"] is None
        assert item["last_run"] is None
        assert item["schedule_next_at"] is None

    async def test_dual_nozzle_printers_are_offered_nozzle_offset(self, async_client, printer_factory):
        printer = await printer_factory(model="H2D")
        item = await _calibration_item(async_client, printer.id)
        assert "nozzle_offset" in item["action_available_options"]

    async def test_reminder_only_items_have_no_action(self, async_client, printer_factory):
        printer = await printer_factory()
        response = await async_client.get(f"/api/v1/maintenance/printers/{printer.id}")
        others = [i for i in response.json()["maintenance_items"] if i["action"] is None]
        assert others
        assert all(i["action_options"] is None and i["current_run"] is None for i in others)


class TestRunNow:
    async def test_run_now_queues_a_pending_run(self, async_client, printer_factory):
        printer = await printer_factory()
        item = await _calibration_item(async_client, printer.id)

        response = await async_client.post(f"/api/v1/maintenance/items/{item['id']}/run")
        assert response.status_code == 200, response.text
        run = response.json()
        assert run["status"] == "pending"
        assert run["source"] == "manual"
        assert run["printer_id"] == printer.id
        assert run["options"]["bed_leveling"] is True
        assert run["start_after"] is None

        item = await _calibration_item(async_client, printer.id)
        assert item["current_run"]["id"] == run["id"]
        assert item["current_run"]["status"] == "pending"

    async def test_second_run_now_is_a_409(self, async_client, printer_factory):
        printer = await printer_factory()
        item = await _calibration_item(async_client, printer.id)
        assert (await async_client.post(f"/api/v1/maintenance/items/{item['id']}/run")).status_code == 200
        response = await async_client.post(f"/api/v1/maintenance/items/{item['id']}/run")
        assert response.status_code == 409

    async def test_run_now_on_a_reminder_type_is_a_400(self, async_client, printer_factory):
        printer = await printer_factory()
        response = await async_client.get(f"/api/v1/maintenance/printers/{printer.id}")
        other = next(i for i in response.json()["maintenance_items"] if i["action"] is None)
        response = await async_client.post(f"/api/v1/maintenance/items/{other['id']}/run")
        assert response.status_code == 400

    async def test_run_now_with_no_flag_selected_is_a_400(self, async_client, printer_factory, db_session):
        printer = await printer_factory()
        item = await _calibration_item(async_client, printer.id)
        row = await db_session.get(PrinterMaintenance, item["id"])
        row.action_options = {}
        await db_session.commit()
        response = await async_client.post(f"/api/v1/maintenance/items/{item['id']}/run")
        assert response.status_code == 400

    async def test_unknown_item_is_a_404(self, async_client):
        assert (await async_client.post("/api/v1/maintenance/items/9999/run")).status_code == 404


class TestCancel:
    async def test_cancel_pending(self, async_client, printer_factory):
        printer = await printer_factory()
        item = await _calibration_item(async_client, printer.id)
        run = (await async_client.post(f"/api/v1/maintenance/items/{item['id']}/run")).json()

        with patch("backend.app.api.routes.maintenance.printer_manager") as mock_pm:
            response = await async_client.delete(f"/api/v1/maintenance/runs/{run['id']}")
        assert response.status_code == 200
        assert response.json() == {"status": "cancelled", "id": run["id"]}
        mock_pm.stop_print.assert_not_called()

        item = await _calibration_item(async_client, printer.id)
        assert item["current_run"] is None
        assert item["last_run"]["status"] == "cancelled"

    async def _running_run(self, async_client, printer_factory, db_session):
        printer = await printer_factory()
        item = await _calibration_item(async_client, printer.id)
        run = MaintenanceRun(
            printer_maintenance_id=item["id"],
            printer_id=printer.id,
            status="running",
            source="manual",
            options={"bed_leveling": True},
            started_at=datetime.now(timezone.utc).replace(tzinfo=None),
        )
        db_session.add(run)
        await db_session.commit()
        return printer, run

    async def test_cancel_running_sends_stop(self, async_client, printer_factory, db_session):
        printer, run = await self._running_run(async_client, printer_factory, db_session)

        with patch("backend.app.api.routes.maintenance.printer_manager") as mock_pm:
            mock_pm.get_status.return_value = SimpleNamespace(
                state="RUNNING",
                gcode_file="/usr/etc/print/H2S/auto_cali_for_user_param.gcode",
                current_print=None,
                subtask_name="auto_cali_for_user_param.gcode",
            )
            mock_pm.stop_print.return_value = True
            response = await async_client.delete(f"/api/v1/maintenance/runs/{run.id}")
        assert response.status_code == 200
        mock_pm.stop_print.assert_called_once_with(printer.id)
        await db_session.refresh(run)
        assert run.status == "cancelled"
        assert run.completed_at is not None

    async def test_cancel_running_never_stops_somebody_elses_print(self, async_client, printer_factory, db_session):
        """A row can outlive its calibration (missed completion across a
        restart); by the time Cancel is clicked the printer may be hours into
        a real print. The row is closed, the printer is left alone."""
        printer, run = await self._running_run(async_client, printer_factory, db_session)

        with patch("backend.app.api.routes.maintenance.printer_manager") as mock_pm:
            mock_pm.get_status.return_value = SimpleNamespace(
                state="RUNNING",
                gcode_file="/data/Metadata/plate_1.gcode",
                current_print="Benchy.gcode.3mf",
                subtask_name="Benchy",
            )
            response = await async_client.delete(f"/api/v1/maintenance/runs/{run.id}")
        assert response.status_code == 200
        mock_pm.stop_print.assert_not_called()
        await db_session.refresh(run)
        assert run.status == "cancelled"
        assert run.completed_at is not None

    async def test_cancel_running_on_an_idle_or_offline_printer_just_closes_the_row(
        self, async_client, printer_factory, db_session
    ):
        _printer, run = await self._running_run(async_client, printer_factory, db_session)

        with patch("backend.app.api.routes.maintenance.printer_manager") as mock_pm:
            mock_pm.get_status.return_value = None
            response = await async_client.delete(f"/api/v1/maintenance/runs/{run.id}")
        assert response.status_code == 200
        mock_pm.stop_print.assert_not_called()
        await db_session.refresh(run)
        assert run.status == "cancelled"

    async def test_cancel_finished_run_is_a_400(self, async_client, printer_factory, db_session):
        printer = await printer_factory()
        item = await _calibration_item(async_client, printer.id)
        run = MaintenanceRun(
            printer_maintenance_id=item["id"], printer_id=printer.id, status="completed", source="manual"
        )
        db_session.add(run)
        await db_session.commit()
        assert (await async_client.delete(f"/api/v1/maintenance/runs/{run.id}")).status_code == 400

    async def test_cancel_unknown_run_is_a_404(self, async_client):
        assert (await async_client.delete("/api/v1/maintenance/runs/9999")).status_code == 404


class TestRunsList:
    async def test_lists_newest_first_with_limit(self, async_client, printer_factory, db_session):
        printer = await printer_factory()
        item = await _calibration_item(async_client, printer.id)
        for status in ("completed", "failed", "cancelled"):
            db_session.add(
                MaintenanceRun(printer_maintenance_id=item["id"], printer_id=printer.id, status=status, source="due")
            )
        await db_session.commit()

        response = await async_client.get(f"/api/v1/maintenance/items/{item['id']}/runs")
        assert response.status_code == 200
        assert [r["status"] for r in response.json()] == ["cancelled", "failed", "completed"]

        response = await async_client.get(f"/api/v1/maintenance/items/{item['id']}/runs?limit=2")
        assert len(response.json()) == 2


class TestSettings:
    async def test_patch_options_and_trigger(self, async_client, printer_factory):
        printer = await printer_factory()
        item = await _calibration_item(async_client, printer.id)
        response = await async_client.patch(
            f"/api/v1/maintenance/items/{item['id']}",
            json={"action_options": {"bed_leveling": True, "nozzle_clumping": True}, "trigger_mode": "when_due"},
        )
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["trigger_mode"] == "when_due"
        assert body["action_options"]["nozzle_clumping"] is True
        assert body["action_options"]["vibration"] is False
        assert body["schedule_next_at"] is None

    @pytest.mark.parametrize(("value", "expected"), [("false", False), ("true", True), (0, False), (1, True)])
    async def test_flags_take_string_and_numeric_booleans(self, async_client, printer_factory, value, expected):
        printer = await printer_factory()
        item = await _calibration_item(async_client, printer.id)
        response = await async_client.patch(
            f"/api/v1/maintenance/items/{item['id']}",
            json={"action_options": {"bed_leveling": value, "vibration": True}},
        )
        assert response.status_code == 200, response.text
        assert response.json()["action_options"]["bed_leveling"] is expected

    @pytest.mark.parametrize("value", ["abc", None, 2, [True], {"on": True}])
    async def test_non_boolean_flags_are_a_422(self, async_client, printer_factory, value):
        printer = await printer_factory()
        item = await _calibration_item(async_client, printer.id)
        response = await async_client.patch(
            f"/api/v1/maintenance/items/{item['id']}",
            json={"action_options": {"bed_leveling": value, "vibration": True}},
        )
        assert response.status_code == 422, response.text

    async def test_schedule_computes_next_at(self, async_client, printer_factory, monkeypatch):
        monkeypatch.setenv("TZ", "UTC")
        printer = await printer_factory()
        item = await _calibration_item(async_client, printer.id)
        response = await async_client.patch(
            f"/api/v1/maintenance/items/{item['id']}",
            json={"trigger_mode": "schedule", "schedule_days": [5, 6], "schedule_time": "6:30"},
        )
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["schedule_days"] == [5, 6]
        assert body["schedule_time"] == "06:30"
        next_at = datetime.fromisoformat(body["schedule_next_at"].replace("Z", "+00:00"))
        assert next_at > datetime.now(timezone.utc)
        assert next_at.weekday() in (5, 6)
        assert (next_at.hour, next_at.minute) == (6, 30)

        # Overview carries the same
        item = await _calibration_item(async_client, printer.id)
        assert item["schedule_next_at"] is not None
        assert item["trigger_mode"] == "schedule"

    async def test_switching_back_to_manual_clears_next_at(self, async_client, printer_factory):
        printer = await printer_factory()
        item = await _calibration_item(async_client, printer.id)
        await async_client.patch(
            f"/api/v1/maintenance/items/{item['id']}",
            json={"trigger_mode": "schedule", "schedule_days": [0], "schedule_time": "06:00"},
        )
        response = await async_client.patch(f"/api/v1/maintenance/items/{item['id']}", json={"trigger_mode": "manual"})
        assert response.status_code == 200
        assert response.json()["schedule_next_at"] is None

    async def test_schedule_without_days_or_time_is_a_400(self, async_client, printer_factory):
        printer = await printer_factory()
        item = await _calibration_item(async_client, printer.id)
        response = await async_client.patch(
            f"/api/v1/maintenance/items/{item['id']}", json={"trigger_mode": "schedule"}
        )
        assert response.status_code == 400
        response = await async_client.patch(
            f"/api/v1/maintenance/items/{item['id']}", json={"trigger_mode": "schedule", "schedule_days": [1]}
        )
        assert response.status_code == 400

    @pytest.mark.parametrize(
        "payload",
        [
            {"schedule_days": [7]},
            {"schedule_days": [1, 1]},
            {"schedule_time": "25:00"},
            {"schedule_time": "six"},
            {"trigger_mode": "sometimes"},
            {"action_options": {"vision_encoder": True}},
        ],
    )
    async def test_malformed_settings_are_a_422(self, async_client, printer_factory, payload):
        printer = await printer_factory()
        item = await _calibration_item(async_client, printer.id)
        response = await async_client.patch(f"/api/v1/maintenance/items/{item['id']}", json=payload)
        assert response.status_code == 422, response.text

    async def test_non_manual_trigger_needs_a_flag(self, async_client, printer_factory):
        printer = await printer_factory()
        item = await _calibration_item(async_client, printer.id)
        response = await async_client.patch(
            f"/api/v1/maintenance/items/{item['id']}", json={"trigger_mode": "when_due", "action_options": {}}
        )
        assert response.status_code == 400

    async def test_action_settings_on_a_reminder_type_are_a_400(self, async_client, printer_factory):
        printer = await printer_factory()
        response = await async_client.get(f"/api/v1/maintenance/printers/{printer.id}")
        other = next(i for i in response.json()["maintenance_items"] if i["action"] is None)
        response = await async_client.patch(
            f"/api/v1/maintenance/items/{other['id']}", json={"trigger_mode": "when_due"}
        )
        assert response.status_code == 400
        # The plain fields still work there.
        response = await async_client.patch(f"/api/v1/maintenance/items/{other['id']}", json={"enabled": False})
        assert response.status_code == 200

    async def test_disabling_the_item_clears_next_at(self, async_client, printer_factory):
        printer = await printer_factory()
        item = await _calibration_item(async_client, printer.id)
        await async_client.patch(
            f"/api/v1/maintenance/items/{item['id']}",
            json={"trigger_mode": "schedule", "schedule_days": [0], "schedule_time": "06:00"},
        )
        response = await async_client.patch(f"/api/v1/maintenance/items/{item['id']}", json={"enabled": False})
        assert response.status_code == 200
        assert response.json()["schedule_next_at"] is None


class TestPerformStillWorks:
    async def test_reset_writes_history_and_resets(self, async_client, printer_factory, db_session):
        printer = await printer_factory()
        item = await _calibration_item(async_client, printer.id)
        response = await async_client.post(f"/api/v1/maintenance/items/{item['id']}/perform", json={"notes": "by hand"})
        assert response.status_code == 200, response.text
        assert response.json()["is_due"] is False
        assert response.json()["action"] == "calibration"
        history = await async_client.get(f"/api/v1/maintenance/items/{item['id']}/history")
        assert [h["notes"] for h in history.json()] == ["by hand"]


class TestEnsureDefaultsKeepsTheAction:
    async def test_action_is_restored_on_an_existing_row(self, async_client, db_session):
        await async_client.get("/api/v1/maintenance/types")
        row = (
            await db_session.execute(select(MaintenanceType).where(MaintenanceType.name == "Printer Calibration"))
        ).scalar_one()
        row.action = None
        await db_session.commit()

        response = await async_client.get("/api/v1/maintenance/types")
        calibration = next(t for t in response.json() if t["name"] == "Printer Calibration")
        assert calibration["action"] == "calibration"


class TestOverviewWaitingReason:
    async def test_waiting_reason_and_last_result_reach_the_client(self, async_client, printer_factory, db_session):
        printer = await printer_factory()
        item = await _calibration_item(async_client, printer.id)
        db_session.add(
            MaintenanceRun(
                printer_maintenance_id=item["id"],
                printer_id=printer.id,
                status="failed",
                source="schedule",
                error_message="Calibration failed (print_error 83886081)",
                completed_at=datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=1),
            )
        )
        db_session.add(
            MaintenanceRun(
                printer_maintenance_id=item["id"],
                printer_id=printer.id,
                status="pending",
                source="schedule",
                waiting_reason="awaiting_plate_clear",
            )
        )
        await db_session.commit()

        item = await _calibration_item(async_client, printer.id)
        assert item["current_run"]["status"] == "pending"
        assert item["current_run"]["source"] == "schedule"
        assert item["current_run"]["waiting_reason"] == "awaiting_plate_clear"
        assert item["last_run"] is None  # an active run takes the slot


# ============== Vision encoder calibration (second action) ==============


async def _motion_item(async_client: AsyncClient, printer_id: int) -> dict | None:
    response = await async_client.get(f"/api/v1/maintenance/printers/{printer_id}")
    assert response.status_code == 200, response.text
    items = [i for i in response.json()["maintenance_items"] if i["action"] == "motion_precision"]
    assert len(items) <= 1
    return items[0] if items else None


class TestVisionEncoderType:
    async def test_seeded_as_a_seven_day_system_type(self, async_client):
        response = await async_client.get("/api/v1/maintenance/types")
        vision = [t for t in response.json() if t["name"] == "Vision Encoder Calibration"]
        assert len(vision) == 1
        assert vision[0]["is_system"] is True
        assert vision[0]["action"] == "motion_precision"
        assert vision[0]["default_interval_hours"] == 7.0
        assert vision[0]["interval_type"] == "days"

    @pytest.mark.parametrize("model", ["H2S", "H2D", "H2D Pro", "H2C", "O1S"])
    async def test_offered_on_the_h2_series(self, async_client, printer_factory, model):
        printer = await printer_factory(model=model)
        item = await _motion_item(async_client, printer.id)
        assert item is not None
        assert item["interval_type"] == "days"
        assert item["interval_hours"] == 7.0
        assert item["is_due"] is True  # never performed
        # No option set: the job has none
        assert item["action_options"] is None
        assert item["action_available_options"] is None
        assert item["trigger_mode"] == "manual"

    @pytest.mark.parametrize("model", ["X1C", "P1S", "A1", "X2D", None])
    async def test_not_offered_on_printers_without_a_vision_encoder(self, async_client, printer_factory, model):
        printer = await printer_factory(model=model)
        assert await _motion_item(async_client, printer.id) is None
        # ...while the bed-levelling calibration still is
        assert await _calibration_item(async_client, printer.id)

    async def test_run_now_needs_no_options(self, async_client, printer_factory):
        printer = await printer_factory(model="H2S")
        item = await _motion_item(async_client, printer.id)
        response = await async_client.post(f"/api/v1/maintenance/items/{item['id']}/run")
        assert response.status_code == 200, response.text
        assert response.json()["status"] == "pending"
        assert response.json()["options"] is None
        assert (await async_client.post(f"/api/v1/maintenance/items/{item['id']}/run")).status_code == 409

    async def test_triggers_need_no_options_either(self, async_client, printer_factory, monkeypatch):
        monkeypatch.setenv("TZ", "UTC")
        printer = await printer_factory(model="H2S")
        item = await _motion_item(async_client, printer.id)
        response = await async_client.patch(
            f"/api/v1/maintenance/items/{item['id']}", json={"trigger_mode": "when_due"}
        )
        assert response.status_code == 200, response.text
        response = await async_client.patch(
            f"/api/v1/maintenance/items/{item['id']}",
            json={"trigger_mode": "schedule", "schedule_days": [5], "schedule_time": "06:00"},
        )
        assert response.status_code == 200, response.text
        assert response.json()["schedule_next_at"] is not None

    async def test_options_cannot_be_set_on_it(self, async_client, printer_factory):
        printer = await printer_factory(model="H2S")
        item = await _motion_item(async_client, printer.id)
        response = await async_client.patch(
            f"/api/v1/maintenance/items/{item['id']}", json={"action_options": {"bed_leveling": True}}
        )
        assert response.status_code == 400
        assert "no options" in response.json()["detail"]

    async def _running_motion_run(self, async_client, printer_factory, db_session):
        printer = await printer_factory(model="H2S")
        item = await _motion_item(async_client, printer.id)
        run = MaintenanceRun(
            printer_maintenance_id=item["id"],
            printer_id=printer.id,
            status="running",
            source="manual",
            started_at=datetime.now(timezone.utc).replace(tzinfo=None),
        )
        db_session.add(run)
        await db_session.commit()
        return printer, run

    async def test_cancel_stops_the_printer_only_on_the_vision_encoder_job(
        self, async_client, printer_factory, db_session
    ):
        printer, run = await self._running_motion_run(async_client, printer_factory, db_session)
        with patch("backend.app.api.routes.maintenance.printer_manager") as mock_pm:
            mock_pm.get_status.return_value = SimpleNamespace(
                state="RUNNING",
                gcode_file="/usr/etc/print/O1S/calibrate_motion_precision.gcode",
                current_print=None,
                subtask_name="calibrate_motion_precision.gcode",
            )
            mock_pm.stop_print.return_value = True
            response = await async_client.delete(f"/api/v1/maintenance/runs/{run.id}")
        assert response.status_code == 200
        mock_pm.stop_print.assert_called_once_with(printer.id)
        await db_session.refresh(run)
        assert run.status == "cancelled"

    async def test_cancel_leaves_the_other_calibration_alone(self, async_client, printer_factory, db_session):
        """The printer is on the bed-levelling run (started from the screen,
        say) while a stale motion_precision row says running: not ours to stop."""
        _printer, run = await self._running_motion_run(async_client, printer_factory, db_session)
        with patch("backend.app.api.routes.maintenance.printer_manager") as mock_pm:
            mock_pm.get_status.return_value = SimpleNamespace(
                state="RUNNING",
                gcode_file="/usr/etc/print/O1S/auto_cali_for_user_param.gcode",
                current_print=None,
                subtask_name="auto_cali_for_user_param.gcode",
            )
            response = await async_client.delete(f"/api/v1/maintenance/runs/{run.id}")
        assert response.status_code == 200
        mock_pm.stop_print.assert_not_called()
        await db_session.refresh(run)
        assert run.status == "cancelled"

    async def test_a_calibration_run_is_not_stopped_while_the_vision_encoder_runs(
        self, async_client, printer_factory, db_session
    ):
        printer = await printer_factory(model="H2S")
        item = await _calibration_item(async_client, printer.id)
        run = MaintenanceRun(
            printer_maintenance_id=item["id"],
            printer_id=printer.id,
            status="running",
            source="manual",
            options={"bed_leveling": True},
            started_at=datetime.now(timezone.utc).replace(tzinfo=None),
        )
        db_session.add(run)
        await db_session.commit()
        with patch("backend.app.api.routes.maintenance.printer_manager") as mock_pm:
            mock_pm.get_status.return_value = SimpleNamespace(
                state="RUNNING",
                gcode_file="/usr/etc/print/O1S/calibrate_motion_precision.gcode",
                current_print=None,
                subtask_name="calibrate_motion_precision.gcode",
            )
            response = await async_client.delete(f"/api/v1/maintenance/runs/{run.id}")
        assert response.status_code == 200
        mock_pm.stop_print.assert_not_called()


# ============== Bed-temperature start condition ==============


class TestBedTempBelowSetting:
    async def test_set_read_back_and_clear(self, async_client, printer_factory):
        printer = await printer_factory()
        item = await _calibration_item(async_client, printer.id)
        response = await async_client.patch(
            f"/api/v1/maintenance/items/{item['id']}",
            json={"action_options": {**item["action_options"], "bed_temp_below": 28}},
        )
        assert response.status_code == 200, response.text
        assert response.json()["action_options"]["bed_temp_below"] == 28.0
        assert response.json()["action_options"]["bed_leveling"] is True

        item = await _calibration_item(async_client, printer.id)
        assert item["action_options"]["bed_temp_below"] == 28.0

        # The run carries the flags only; the condition stays on the item.
        response = await async_client.post(f"/api/v1/maintenance/items/{item['id']}/run")
        assert response.status_code == 200, response.text
        assert "bed_temp_below" not in response.json()["options"]

        # null clears it, and so does leaving the key out
        response = await async_client.patch(
            f"/api/v1/maintenance/items/{item['id']}",
            json={"action_options": {"bed_leveling": True, "bed_temp_below": None}},
        )
        assert response.status_code == 200, response.text
        assert "bed_temp_below" not in response.json()["action_options"]
        item = await _calibration_item(async_client, printer.id)
        assert "bed_temp_below" not in item["action_options"]

    async def test_one_decimal_is_kept(self, async_client, printer_factory):
        printer = await printer_factory()
        item = await _calibration_item(async_client, printer.id)
        response = await async_client.patch(
            f"/api/v1/maintenance/items/{item['id']}",
            json={"action_options": {"bed_leveling": True, "bed_temp_below": 29.5}},
        )
        assert response.status_code == 200, response.text
        assert response.json()["action_options"]["bed_temp_below"] == 29.5

    @pytest.mark.parametrize("value", [0, -1, 120.5, 1000, "28", True, [28], 28.25])
    async def test_out_of_range_and_non_numeric_are_a_422(self, async_client, printer_factory, value):
        printer = await printer_factory()
        item = await _calibration_item(async_client, printer.id)
        response = await async_client.patch(
            f"/api/v1/maintenance/items/{item['id']}",
            json={"action_options": {"bed_leveling": True, "bed_temp_below": value}},
        )
        assert response.status_code == 422, response.text

    async def test_the_vision_encoder_item_takes_the_condition_but_no_flags(self, async_client, printer_factory):
        printer = await printer_factory(model="H2S")
        item = await _motion_item(async_client, printer.id)
        response = await async_client.patch(
            f"/api/v1/maintenance/items/{item['id']}", json={"action_options": {"bed_temp_below": 30}}
        )
        assert response.status_code == 200, response.text
        assert response.json()["action_options"] == {"bed_temp_below": 30.0}
        item = await _motion_item(async_client, printer.id)
        assert item["action_options"] == {"bed_temp_below": 30.0}
        assert item["action_available_options"] is None

        response = await async_client.patch(
            f"/api/v1/maintenance/items/{item['id']}",
            json={"action_options": {"bed_temp_below": 30, "bed_leveling": True}},
        )
        assert response.status_code == 400

        response = await async_client.patch(f"/api/v1/maintenance/items/{item['id']}", json={"action_options": {}})
        assert response.status_code == 200, response.text
        item = await _motion_item(async_client, printer.id)
        assert item["action_options"] is None

    async def test_the_waiting_temperature_reaches_the_card(self, async_client, printer_factory, db_session):
        printer = await printer_factory()
        item = await _calibration_item(async_client, printer.id)
        db_session.add(
            MaintenanceRun(
                printer_maintenance_id=item["id"],
                printer_id=printer.id,
                status="pending",
                source="due",
                waiting_reason="bed_too_warm",
                waiting_detail={"bed_temp": 34.2, "threshold": 30.0},
            )
        )
        await db_session.commit()
        item = await _calibration_item(async_client, printer.id)
        assert item["current_run"]["waiting_reason"] == "bed_too_warm"
        assert item["current_run"]["waiting_detail"] == {"bed_temp": 34.2, "threshold": 30.0}

        response = await async_client.get(f"/api/v1/maintenance/items/{item['id']}/runs")
        assert response.json()[0]["waiting_detail"] == {"bed_temp": 34.2, "threshold": 30.0}

        # Cancelling clears reason and detail together
        with patch("backend.app.api.routes.maintenance.printer_manager"):
            response = await async_client.delete(f"/api/v1/maintenance/runs/{item['current_run']['id']}")
        assert response.status_code == 200
        item = await _calibration_item(async_client, printer.id)
        assert item["last_run"]["waiting_reason"] is None
        assert item["last_run"]["waiting_detail"] is None
