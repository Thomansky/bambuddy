"""End-to-end behaviour of a flow-dynamics calibration run.

Two of these tests matter more than the rest: the ones that assert **nothing**
reached the printer. A slice that failed, or a sliced file that cannot
calibrate, must not produce an FTP connection or an MQTT publish -- that is the
whole safety story of the feature, and the only way to keep it is to assert the
mocks were never called.
"""

import io
import json
import zipfile
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.app.models.pa_calibration import PaCalibrationRun
from backend.app.services import pa_calibration as pa
from backend.app.services.slicer_api import SlicerApiUnavailableError, SliceResult
from backend.app.utils.local_time import utcnow_naive

CALIBRATING_GCODE = """
M1002 gcode_claim_action : 4
M1002 judge_flag extrude_cali_flag
M622 J0
    M983.3 F10.4167 A0.4 ; cali dynamic extrusion compensation
M623
G1 X100 Y130 F30000
"""

PLAIN_GCODE = "M1002 gcode_claim_action : 4\nG1 X100 Y130 F30000\n"

RESULT_ENTRY = {
    "ams_id": 0,
    "confidence": 0,
    "extruder_id": 0,
    "filament_id": "GFA01",
    "k_value": "0.018612",
    "n_coef": "0.750000",
    "nozzle_diameter": "0.4",
    "nozzle_id": "HS01-0.4",
    "nozzle_pos": 0,
    "nozzle_sn": "N/A",
    "setting_id": "",
    "slot_id": 0,
    "tray_id": -1,
}


def make_3mf(gcode: str) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("Metadata/project_settings.config", json.dumps({"machine_start_gcode": gcode}))
        archive.writestr("Metadata/plate_1.gcode", gcode)
    return buffer.getvalue()


class FakeKProfile:
    def __init__(self, k_value="0.020000", nozzle_id="HS00-0.4", filament_id="GFA01", slot_id=1):
        self.k_value = k_value
        self.nozzle_id = nozzle_id
        self.filament_id = filament_id
        self.slot_id = slot_id
        self.name = "Bambu PLA Matte"
        self.extruder_id = 0
        self.nozzle_diameter = "0.4"
        self.n_coef = "0.000000"
        self.ams_id = 0
        self.tray_id = 0
        self.setting_id = ""


class FakeMqttClient:
    """Records every command that would have gone on the wire."""

    def __init__(self, *, result=None, stored_k="0.020000"):
        self.state = SimpleNamespace(connected=True)
        self.published: list[tuple[str, dict]] = []
        self._result = result
        self._stored_k = stored_k
        self.stop_print_calls = 0

    async def get_kprofiles(self, nozzle_diameter="0.4", **_kwargs):
        self.published.append(("extrusion_cali_get", {"nozzle_diameter": nozzle_diameter}))
        if self._stored_k is None:
            return []
        return [FakeKProfile(k_value=self._stored_k)]

    async def get_extrusion_cali_result(self, nozzle_diameter="0.4", **_kwargs):
        self.published.append(("extrusion_cali_get_result", {"nozzle_diameter": nozzle_diameter}))
        return self._result

    def set_measured_kprofile(self, nozzle_diameter, filament):
        self.published.append(("extrusion_cali_set", {"nozzle_diameter": nozzle_diameter, "filament": filament}))
        self._stored_k = filament["k_value"]
        return "4242"

    def set_kprofile(self, **kwargs):
        self.published.append(("extrusion_cali_set_new", kwargs))
        self._stored_k = kwargs["k_value"]
        return "4243"

    def extrusion_cali_sel(self, **kwargs):
        self.published.append(("extrusion_cali_sel", kwargs))
        return True

    async def await_cali_ack(self, seq_id, timeout=6.0):
        self.published.append(("await_ack", {"seq": seq_id}))
        return (True, "")

    def stop_print(self):
        self.stop_print_calls += 1
        self.published.append(("stop", {}))
        return True

    def commands(self) -> list[str]:
        return [name for name, _ in self.published]


def make_state(*, state="IDLE", filament_id="GFA01", connected=True, subtask=None, progress=0):
    trays = [{"id": "0", "state": 10}]
    if filament_id:
        trays.append(
            {
                "id": "1",
                "tray_info_idx": filament_id,
                "tray_type": "PLA",
                "tray_sub_brands": "PLA Matte",
                "tray_color": "FFFFFFFF",
                "cali_idx": 1,
            }
        )
    else:
        trays.append({"id": "1", "state": 10})
    return SimpleNamespace(
        connected=connected,
        state=state,
        raw_data={"ams": [{"id": "0", "tray": trays}]},
        nozzles=[SimpleNamespace(nozzle_type="HS01", nozzle_diameter="0.4")],
        ams_extruder_map={},
        ams_switch_inlet={},
        dispatched_subtask=subtask,
        subtask_name=subtask,
        progress=progress,
        print_error=0,
    )


@pytest.fixture
def pa_env(monkeypatch, test_engine):
    """A supported printer, a working sidecar and a fake printer, all recorded.

    Returns a namespace whose FTP, dispatch and MQTT records every test can
    assert against -- including, crucially, asserting they were never used.
    """
    # The two long-running steps open their own session, exactly as the queue's
    # dispatch does, so that has to point at the test engine too.
    monkeypatch.setattr(
        "backend.app.core.database.async_session",
        async_sessionmaker(test_engine, class_=AsyncSession, expire_on_commit=False),
    )
    mqtt = FakeMqttClient(
        result={"result": "success", "reason": "", "nozzle_diameter": "0.4", "filaments": [RESULT_ENTRY]}
    )
    env = SimpleNamespace(
        mqtt=mqtt,
        state=make_state(),
        slice_content=make_3mf(CALIBRATING_GCODE),
        slice_error=None,
        uploads=[],
        deletes=[],
        starts=[],
        start_result=True,
        plate_clear_pending=False,
    )

    monkeypatch.setattr(pa.printer_manager, "get_status", lambda _pid: env.state)
    monkeypatch.setattr(pa.printer_manager, "get_client", lambda _pid: env.mqtt)
    monkeypatch.setattr(
        pa.printer_manager, "is_awaiting_plate_clear", lambda _pid: env.plate_clear_pending, raising=False
    )

    def _start_print(printer_id, filename, **kwargs):
        env.starts.append({"printer_id": printer_id, "filename": filename, **kwargs})
        return env.start_result

    monkeypatch.setattr(pa.printer_manager, "start_print", _start_print)

    async def _upload(ip, code, local, remote, **_kwargs):
        env.uploads.append(remote)
        return True

    async def _delete(ip, code, remote, **_kwargs):
        env.deletes.append(remote)
        return SimpleNamespace(name="DELETED")

    monkeypatch.setattr(pa, "upload_file_async", _upload)
    monkeypatch.setattr(pa, "delete_file_async", _delete)
    monkeypatch.setattr(pa, "get_ftp_retry_settings", AsyncMock(return_value=(False, 0, 0.0, 30.0)))

    class FakeSlicer:
        def __init__(self, *_args, **_kwargs):
            pass

        async def close(self):
            return None

        async def health(self):
            return {"status": "healthy"}

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_):
            return None

        async def slice_with_profiles(self, **_kwargs):
            if env.slice_error is not None:
                raise env.slice_error
            return SliceResult(
                content=env.slice_content,
                print_time_seconds=344,
                filament_used_g=0.07,
                filament_used_mm=20,
            )

    # Both bindings: the service module holds a direct reference, and the
    # preflight route imports the name at call time for its health check.
    monkeypatch.setattr(pa, "SlicerApiService", FakeSlicer)
    monkeypatch.setattr("backend.app.services.slicer_api.SlicerApiService", FakeSlicer)

    async def _resolve(_db, _user, ref, slot):
        return json.dumps({"type": slot, "inherits": ref.id})

    monkeypatch.setattr("backend.app.services.preset_resolver.resolve_preset_ref", _resolve)
    monkeypatch.setattr("backend.app.services.pa_calibration.notify", AsyncMock())
    return env


async def make_run(db_session, printer, **overrides) -> PaCalibrationRun:
    defaults = {
        "printer_id": printer.id,
        "ams_id": 0,
        "slot_id": 1,
        "tray_id": 1,
        "extruder_id": 0,
        "filament_id": "GFA01",
        "filament_name": "PLA Matte",
        "nozzle_diameter": "0.4",
        "nozzle_id": "HS01-0.4",
        "plate_type": "textured_plate",
        "plate_confirmed": True,
        "presets": {
            "printer": {"source": "standard", "id": "Bambu Lab H2S 0.4 nozzle"},
            "process": {"source": "standard", "id": "0.20mm Standard @BBL H2S"},
            "filament": {"source": "standard", "id": "Bambu PLA Matte @BBL H2S"},
        },
        "status": "queued",
        "stage": "queued",
        "k_before": 0.02,
    }
    defaults.update(overrides)
    row = PaCalibrationRun(**defaults)
    db_session.add(row)
    await db_session.commit()
    await db_session.refresh(row)
    return row


@pytest.fixture
async def h2s(printer_factory):
    return await printer_factory(model="H2S", name="H2S", serial_number="0938BJ611001133")


class TestSliceAndDispatch:
    async def test_happy_path_reaches_awaiting_confirmation(self, db_session, h2s, pa_env):
        row = await make_run(db_session, h2s, status="slicing", stage="slicing")
        await pa.run_slice_and_dispatch(row.id)
        await db_session.refresh(row)

        assert row.status == "printing"
        assert row.remote_filename == "bambuddy_pa_cali.3mf"
        assert pa_env.uploads == ["/bambuddy_pa_cali.3mf"]

        dispatched = pa_env.starts[0]
        # The two fields the whole feature turns on.
        assert dispatched["flow_cali"] == "on"
        assert dispatched["bed_levelling"] == "off"
        assert dispatched["vibration_cali"] is False
        assert dispatched["timelapse"] is False
        assert dispatched["layer_inspect"] is True
        assert dispatched["nozzle_offset_cali"] == "off"
        assert dispatched["ams_mapping"] == [1]
        assert dispatched["plate_id"] == 1

    async def test_sidecar_unreachable_never_touches_the_printer(self, db_session, h2s, pa_env):
        pa_env.slice_error = SlicerApiUnavailableError("Slicer sidecar unreachable: ConnectError")
        row = await make_run(db_session, h2s, status="slicing", stage="slicing")
        await pa.run_slice_and_dispatch(row.id)
        await db_session.refresh(row)

        assert row.status == "failed"
        assert row.stage == "slicing"
        assert "unreachable" in row.error_message
        assert pa_env.uploads == []
        assert pa_env.deletes == []
        assert pa_env.starts == []
        assert pa_env.mqtt.commands() == []

    async def test_content_guard_never_touches_the_printer(self, db_session, h2s, pa_env):
        pa_env.slice_content = make_3mf(PLAIN_GCODE)
        row = await make_run(db_session, h2s, status="slicing", stage="slicing")
        await pa.run_slice_and_dispatch(row.id)
        await db_session.refresh(row)

        assert row.status == "failed"
        assert row.stage == "slicing"
        assert "calibration step" in row.error_message
        assert pa_env.uploads == []
        assert pa_env.deletes == []
        assert pa_env.starts == []
        assert pa_env.mqtt.commands() == []

    async def test_a_filament_swap_between_upload_and_dispatch_stops_it(self, db_session, h2s, pa_env):
        row = await make_run(db_session, h2s, status="slicing", stage="slicing")

        original_upload = pa.upload_file_async

        async def swap_then_upload(*args, **kwargs):
            pa_env.state = make_state(filament_id="GFG02")
            return await original_upload(*args, **kwargs)

        pa.upload_file_async = swap_then_upload
        try:
            await pa.run_slice_and_dispatch(row.id)
        finally:
            pa.upload_file_async = original_upload

        await db_session.refresh(row)
        assert row.status == "failed"
        assert "changed" in row.error_message
        assert pa_env.starts == []

    async def test_start_refused_fails_the_run_and_removes_the_file(self, db_session, h2s, pa_env):
        pa_env.start_result = False
        row = await make_run(db_session, h2s, status="slicing", stage="slicing")
        await pa.run_slice_and_dispatch(row.id)
        await db_session.refresh(row)

        assert row.status == "failed"
        # Pre-upload delete, then the cleanup delete.
        assert pa_env.deletes.count("/bambuddy_pa_cali.3mf") == 2


class TestResultReadBack:
    async def test_parks_the_measurement_for_confirmation(self, db_session, h2s, pa_env):
        row = await make_run(
            db_session,
            h2s,
            status="reading_result",
            stage="reading_result",
            remote_filename="bambuddy_pa_cali.3mf",
        )
        await pa.run_read_result(row.id, settle_seconds=0)
        await db_session.refresh(row)

        assert row.status == "awaiting_confirmation"
        assert row.k_value == pytest.approx(0.018612)
        assert row.n_coef == "0.750000"
        assert row.confidence == 0
        assert row.k_before == pytest.approx(0.02)
        # Nothing was written: only the read went out.
        assert "extrusion_cali_set" not in pa_env.mqtt.commands()
        # The job file is removed as soon as it has served its purpose.
        assert "/bambuddy_pa_cali.3mf" in pa_env.deletes

    async def test_no_result_fails_without_writing(self, db_session, h2s, pa_env):
        pa_env.mqtt._result = None
        row = await make_run(db_session, h2s, status="reading_result", stage="reading_result")
        await pa.run_read_result(row.id, settle_seconds=0)
        await db_session.refresh(row)

        assert row.status == "failed"
        assert "no calibration result" in row.error_message
        assert "extrusion_cali_set" not in pa_env.mqtt.commands()

    async def test_a_result_for_another_filament_fails_without_writing(self, db_session, h2s, pa_env):
        pa_env.mqtt._result = {
            "result": "success",
            "filaments": [{**RESULT_ENTRY, "filament_id": "GFG02"}],
        }
        row = await make_run(db_session, h2s, status="reading_result", stage="reading_result")
        await pa.run_read_result(row.id, settle_seconds=0)
        await db_session.refresh(row)

        assert row.status == "failed"
        assert "extrusion_cali_set" not in pa_env.mqtt.commands()


class TestTick:
    async def test_a_busy_printer_leaves_the_run_queued_with_a_reason(self, db_session, h2s, pa_env):
        pa_env.state = make_state(state="RUNNING")
        row = await make_run(db_session, h2s)
        held = await pa.tick(db_session)
        await db_session.refresh(row)

        assert row.status == "queued"
        assert row.waiting_reason == "printer_busy"
        assert held == set()
        assert pa_env.uploads == []
        assert pa_env.starts == []

    async def test_an_offline_printer_waits(self, db_session, h2s, pa_env):
        pa_env.state = None
        row = await make_run(db_session, h2s)
        await pa.tick(db_session)
        await db_session.refresh(row)
        assert row.status == "queued"
        assert row.waiting_reason == "printer_offline"

    async def test_plate_clear_is_honoured(self, db_session, h2s, pa_env):
        pa_env.plate_clear_pending = True
        row = await make_run(db_session, h2s)
        await pa.tick(db_session, require_plate_clear=True)
        await db_session.refresh(row)
        assert row.status == "queued"
        assert row.waiting_reason == "plate_not_cleared"

    async def test_a_run_without_the_plate_tick_fails(self, db_session, h2s, pa_env):
        # Only reachable from an older schema or a direct DB write -- the route
        # refuses it -- but this is the check that keeps a toolhead off a plate
        # nobody said was empty, so it is pinned here too.
        row = await make_run(db_session, h2s, plate_confirmed=False)
        await pa.tick(db_session)
        await db_session.refresh(row)
        assert row.status == "failed"
        assert pa_env.starts == []
        assert pa_env.uploads == []

    async def test_an_empty_slot_fails_the_run(self, db_session, h2s, pa_env):
        pa_env.state = make_state(filament_id="")
        row = await make_run(db_session, h2s)
        await pa.tick(db_session)
        await db_session.refresh(row)
        assert row.status == "failed"
        assert "empty" in row.error_message

    async def test_an_unsupported_model_fails_the_run(self, db_session, printer_factory, pa_env):
        x1c = await printer_factory(model="X1C")
        row = await make_run(db_session, x1c)
        await pa.tick(db_session)
        await db_session.refresh(row)
        assert row.status == "failed"
        assert "cannot measure" in row.error_message

    async def test_a_dispatched_run_holds_its_printer(self, db_session, h2s, pa_env):
        await make_run(db_session, h2s, status="printing", stage="printing")
        held = await pa.tick(db_session)
        assert held == {h2s.id}
        assert await pa.active_run_printer_ids(db_session) == {h2s.id}

    async def test_a_queued_run_holds_nothing(self, db_session, h2s, pa_env):
        """Reserving here would deadlock the run against the queue it waits for."""
        pa_env.state = make_state(state="RUNNING")
        await make_run(db_session, h2s)
        assert await pa.active_run_printer_ids(db_session) == set()

    async def test_finish_moves_to_reading_result(self, db_session, h2s, pa_env, monkeypatch):
        spawned = []
        monkeypatch.setattr(
            "backend.app.core.tasks.spawn_background_task",
            lambda coro, name=None: spawned.append(name) or coro.close(),
        )
        pa_env.state = make_state(state="FINISH", subtask="bambuddy_pa_cali")
        row = await make_run(
            db_session,
            h2s,
            status="printing",
            stage="printing",
            dispatched_subtask_id="bambuddy_pa_cali",
        )
        await pa.tick(db_session)
        await db_session.refresh(row)
        assert row.status == "reading_result"
        assert spawned == [f"pa-calibration-result-{row.id}"]

    async def test_a_finish_from_another_job_is_ignored(self, db_session, h2s, pa_env):
        pa_env.state = make_state(state="FINISH", subtask="someone_elses_benchy")
        row = await make_run(
            db_session,
            h2s,
            status="printing",
            stage="printing",
            dispatched_subtask_id="bambuddy_pa_cali",
        )
        await pa.tick(db_session)
        await db_session.refresh(row)
        assert row.status == "printing"

    async def test_a_failed_print_is_not_read_back(self, db_session, h2s, pa_env, monkeypatch):
        monkeypatch.setattr(
            "backend.app.core.tasks.spawn_background_task",
            lambda coro, name=None: coro.close(),
        )
        pa_env.state = make_state(state="FAILED")
        row = await make_run(db_session, h2s, status="printing", stage="printing")
        await pa.tick(db_session)
        await db_session.refresh(row)
        assert row.status == "failed"
        assert "did not finish" in row.error_message
        assert "extrusion_cali_get_result" not in pa_env.mqtt.commands()

    async def test_the_start_watchdog_fails_a_print_that_never_began(self, db_session, h2s, pa_env, monkeypatch):
        monkeypatch.setattr(
            "backend.app.core.tasks.spawn_background_task",
            lambda coro, name=None: coro.close(),
        )
        row = await make_run(
            db_session,
            h2s,
            status="printing",
            stage="printing",
            started_at=utcnow_naive() - timedelta(seconds=pa.START_WATCHDOG_SECONDS + 5),
        )
        await pa.tick(db_session)
        await db_session.refresh(row)
        assert row.status == "failed"
        assert "never started" in row.error_message


class TestStaleSweep:
    async def test_an_active_run_older_than_two_hours_fails(self, db_session, h2s):
        row = await make_run(
            db_session,
            h2s,
            status="slicing",
            stage="slicing",
            started_at=utcnow_naive() - timedelta(hours=3),
        )
        assert await pa.sweep_stale_runs(db_session) == 1
        await db_session.commit()
        await db_session.refresh(row)
        assert row.status == "failed"

    async def test_an_unanswered_result_is_cancelled_after_a_day(self, db_session, h2s, pa_env):
        row = await make_run(
            db_session,
            h2s,
            status="awaiting_confirmation",
            stage="awaiting_confirmation",
            k_value=0.018612,
            started_at=utcnow_naive() - timedelta(hours=25),
        )
        assert await pa.sweep_stale_runs(db_session) == 1
        await db_session.commit()
        await db_session.refresh(row)
        assert row.status == "cancelled"
        # Cancelled means nothing was written.
        assert "extrusion_cali_set" not in pa_env.mqtt.commands()

    async def test_a_fresh_run_is_left_alone(self, db_session, h2s):
        row = await make_run(db_session, h2s, status="printing", stage="printing", started_at=utcnow_naive())
        assert await pa.sweep_stale_runs(db_session) == 0
        await db_session.refresh(row)
        assert row.status == "printing"


class TestRoutes:
    async def test_create_requires_the_plate_tick(self, async_client, h2s, pa_env):
        body = {
            "ams_id": 0,
            "slot_id": 1,
            "plate_type": "textured_plate",
            "presets": {
                "printer": {"source": "standard", "id": "p"},
                "process": {"source": "standard", "id": "q"},
                "filament": {"source": "standard", "id": "f"},
            },
            "plate_confirmed": False,
        }
        response = await async_client.post(f"/api/v1/printers/{h2s.id}/pa-calibration/runs", json=body)
        assert response.status_code == 400
        assert "plate" in response.json()["detail"].lower()

    async def test_create_then_a_second_start_is_refused(self, async_client, h2s, pa_env):
        body = {
            "ams_id": 0,
            "slot_id": 1,
            "plate_type": "textured_plate",
            "presets": {
                "printer": {"source": "standard", "id": "p"},
                "process": {"source": "standard", "id": "q"},
                "filament": {"source": "standard", "id": "f"},
            },
            "plate_confirmed": True,
        }
        first = await async_client.post(f"/api/v1/printers/{h2s.id}/pa-calibration/runs", json=body)
        assert first.status_code == 202, first.text
        created = first.json()
        assert created["status"] == "queued"
        assert created["tray_id"] == 1
        assert created["k_before"] == pytest.approx(0.02)

        second = await async_client.post(f"/api/v1/printers/{h2s.id}/pa-calibration/runs", json=body)
        assert second.status_code == 409

        listed = await async_client.get(f"/api/v1/printers/{h2s.id}/pa-calibration/runs?active=true")
        assert len(listed.json()["runs"]) == 1

    async def test_create_is_refused_on_an_unsupported_model(self, async_client, printer_factory, pa_env):
        x1c = await printer_factory(model="X1C")
        body = {
            "ams_id": 0,
            "slot_id": 1,
            "plate_type": "textured_plate",
            "presets": {
                "printer": {"source": "standard", "id": "p"},
                "process": {"source": "standard", "id": "q"},
                "filament": {"source": "standard", "id": "f"},
            },
            "plate_confirmed": True,
        }
        response = await async_client.post(f"/api/v1/printers/{x1c.id}/pa-calibration/runs", json=body)
        assert response.status_code == 400

    async def test_confirm_writes_studios_payload_and_reads_back(self, async_client, db_session, h2s, pa_env):
        row = await make_run(
            db_session,
            h2s,
            status="awaiting_confirmation",
            stage="awaiting_confirmation",
            k_value=0.018612,
            n_coef="0.750000",
            confidence=0,
            result_raw=RESULT_ENTRY,
        )
        response = await async_client.post(
            f"/api/v1/printers/{h2s.id}/pa-calibration/runs/{row.id}/confirm",
        )
        assert response.status_code == 200, response.text
        assert response.json()["status"] == "done"

        writes = [payload for name, payload in pa_env.mqtt.published if name == "extrusion_cali_set"]
        assert len(writes) == 1
        filament = writes[0]["filament"]
        assert filament["k_value"] == "0.019000"
        assert filament["n_coef"] == "0.750000"
        assert filament["nozzle_id"] == "HS00-0.4"
        assert filament["name"] == "Bambu PLA Matte"
        assert filament["tray_id"] == 0
        assert "cali_idx" not in filament
        # Acked, then read back -- await_cali_ack answers True on a timeout, so
        # the table is the only proof.
        assert pa_env.mqtt.commands().count("extrusion_cali_get") >= 2
        assert "await_ack" in pa_env.mqtt.commands()

    async def test_confirm_refuses_when_the_filament_changed(self, async_client, db_session, h2s, pa_env):
        row = await make_run(
            db_session,
            h2s,
            status="awaiting_confirmation",
            stage="awaiting_confirmation",
            k_value=0.018612,
            n_coef="0.750000",
            result_raw=RESULT_ENTRY,
        )
        pa_env.state = make_state(filament_id="GFG02")
        response = await async_client.post(
            f"/api/v1/printers/{h2s.id}/pa-calibration/runs/{row.id}/confirm",
        )
        assert response.status_code == 409
        assert "extrusion_cali_set" not in pa_env.mqtt.commands()

    async def test_confirm_fails_when_the_printer_did_not_store_the_value(self, async_client, db_session, h2s, pa_env):
        class StubbornClient(FakeMqttClient):
            def set_measured_kprofile(self, nozzle_diameter, filament):
                self.published.append(("extrusion_cali_set", {"filament": filament}))
                return "1"  # accepts, stores nothing

        pa_env.mqtt = StubbornClient(result=None, stored_k="0.020000")
        row = await make_run(
            db_session,
            h2s,
            status="awaiting_confirmation",
            stage="awaiting_confirmation",
            k_value=0.018612,
            n_coef="0.750000",
            result_raw=RESULT_ENTRY,
        )
        response = await async_client.post(
            f"/api/v1/printers/{h2s.id}/pa-calibration/runs/{row.id}/confirm",
        )
        assert response.status_code == 502
        await db_session.refresh(row)
        assert row.status == "failed"
        assert "did not store" in row.error_message

    async def test_discard_writes_nothing(self, async_client, db_session, h2s, pa_env):
        row = await make_run(
            db_session,
            h2s,
            status="awaiting_confirmation",
            stage="awaiting_confirmation",
            k_value=0.018612,
            result_raw=RESULT_ENTRY,
        )
        response = await async_client.post(
            f"/api/v1/printers/{h2s.id}/pa-calibration/runs/{row.id}/discard",
        )
        assert response.status_code == 200
        assert response.json()["status"] == "cancelled"
        assert "extrusion_cali_set" not in pa_env.mqtt.commands()

    async def test_cancel_while_printing_needs_the_stop_flag_and_stops_once(
        self, async_client, db_session, h2s, pa_env
    ):
        row = await make_run(
            db_session,
            h2s,
            status="printing",
            stage="printing",
            remote_filename="bambuddy_pa_cali.3mf",
        )
        refused = await async_client.post(f"/api/v1/printers/{h2s.id}/pa-calibration/runs/{row.id}/cancel")
        assert refused.status_code == 409
        assert pa_env.mqtt.stop_print_calls == 0

        accepted = await async_client.post(
            f"/api/v1/printers/{h2s.id}/pa-calibration/runs/{row.id}/cancel?stop_print=true"
        )
        assert accepted.status_code == 200
        assert accepted.json()["status"] == "cancelled"
        assert pa_env.mqtt.stop_print_calls == 1
        assert "/bambuddy_pa_cali.3mf" in pa_env.deletes

    async def test_cancel_before_dispatch_is_free(self, async_client, db_session, h2s, pa_env):
        row = await make_run(db_session, h2s)
        response = await async_client.post(f"/api/v1/printers/{h2s.id}/pa-calibration/runs/{row.id}/cancel")
        assert response.status_code == 200
        assert pa_env.mqtt.stop_print_calls == 0
        assert pa_env.starts == []

    async def test_preflight_reports_every_blocker(self, async_client, h2s, pa_env):
        pa_env.state = make_state(state="RUNNING", filament_id="")
        response = await async_client.get(f"/api/v1/printers/{h2s.id}/pa-calibration/preflight?ams_id=0&slot_id=1")
        assert response.status_code == 200
        body = response.json()
        assert body["supported"] is True
        assert "printer_busy" in body["blocked_reasons"]
        assert "slot_empty" in body["blocked_reasons"]

    async def test_preflight_on_an_unsupported_model_says_so_and_nothing_else(
        self, async_client, printer_factory, pa_env
    ):
        x1c = await printer_factory(model="X1C")
        response = await async_client.get(f"/api/v1/printers/{x1c.id}/pa-calibration/preflight?ams_id=0&slot_id=1")
        body = response.json()
        assert body["supported"] is False
        assert body["blocked_reasons"] == ["model_not_supported"]

    async def test_preflight_shows_the_current_k(self, async_client, h2s, pa_env, monkeypatch):
        monkeypatch.setattr(
            "backend.app.api.routes.slicer_presets._fetch_bundled_presets",
            AsyncMock(
                return_value={
                    "printer": [SimpleNamespace(id="Bambu Lab H2S 0.4 nozzle", name="Bambu Lab H2S 0.4 nozzle")],
                    "process": [
                        SimpleNamespace(
                            id="0.20mm Standard @BBL H2S",
                            name="0.20mm Standard @BBL H2S",
                            compatible_printers=["Bambu Lab H2S 0.4 nozzle"],
                        )
                    ],
                    "filament": [
                        SimpleNamespace(
                            id="Bambu PLA Matte @BBL H2S",
                            name="Bambu PLA Matte @BBL H2S",
                            filament_type="PLA",
                            compatible_printers=["Bambu Lab H2S 0.4 nozzle"],
                        )
                    ],
                }
            ),
        )
        response = await async_client.get(f"/api/v1/printers/{h2s.id}/pa-calibration/preflight?ams_id=0&slot_id=1")
        body = response.json()
        assert body["blocked_reasons"] == []
        assert body["current_k"] == pytest.approx(0.02)
        assert body["nozzle_id"] == "HS01-0.4"
        assert body["nozzle_diameter"] == "0.4"
        assert body["filament"]["filament_id"] == "GFA01"
        assert body["presets"]["printer"]["id"] == "Bambu Lab H2S 0.4 nozzle"
        assert body["estimated_seconds"] == 420
        assert "textured_plate" in body["plate_types"]


class TestQueueInterlock:
    async def test_a_live_run_keeps_the_queue_off_the_printer(self, db_session, h2s, pa_env):
        """The print scheduler reads exactly this set before dispatching."""
        await make_run(db_session, h2s, status="printing", stage="printing")
        from backend.app.services.print_scheduler import PrintScheduler

        scheduler = PrintScheduler()
        await scheduler._check_pa_calibration_runs(db_session, False)
        assert scheduler._pa_calibrating_printer_ids == {h2s.id}

    async def test_a_finished_run_releases_the_printer(self, db_session, h2s, pa_env):
        await make_run(db_session, h2s, status="done", stage="done")
        from backend.app.services.print_scheduler import PrintScheduler

        scheduler = PrintScheduler()
        await scheduler._check_pa_calibration_runs(db_session, False)
        assert scheduler._pa_calibrating_printer_ids == set()

    async def test_awaiting_confirmation_releases_the_printer(self, db_session, h2s, pa_env):
        """Holding a farm printer for up to 24 hours waiting on a click would be
        worse than anything the hold protects -- the print is already done."""
        await make_run(db_session, h2s, status="awaiting_confirmation", stage="awaiting_confirmation")
        from backend.app.services.print_scheduler import PrintScheduler

        scheduler = PrintScheduler()
        await scheduler._check_pa_calibration_runs(db_session, False)
        assert scheduler._pa_calibrating_printer_ids == set()
