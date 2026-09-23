"""Reading the flow-dynamics measurement back, and writing it.

``extrusion_cali_get_result`` is the only way to learn what the print measured
-- the value is produced inside the start G-code, not returned by any command
-- so the request/response matching has to be exactly as careful as the
K-profile table's. The printer broadcasts on the same report topic Bambu Studio
queries, so an answer to somebody else must never resolve our wait.
"""

import asyncio
import json
from unittest.mock import MagicMock

import pytest

from backend.app.services.bambu_mqtt import BambuMQTTClient

# The capture's own result payload, run 1.
CAPTURED_RESULT = {
    "command": "extrusion_cali_get_result",
    "result": "success",
    "reason": "",
    "nozzle_diameter": "0.4",
    "filaments": [
        {
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
    ],
}


def _client() -> BambuMQTTClient:
    client = BambuMQTTClient(ip_address="10.0.0.1", serial_number="TESTSERIAL0000", access_code="00000000")
    client._client = MagicMock()
    client.state.connected = True
    return client


def _published(client) -> list[dict]:
    return [json.loads(call.args[1]) for call in client._client.publish.call_args_list]


async def _answer_after_publish(client, response: dict, *, echo_sequence: bool = True) -> None:
    """Feed ``response`` back once the request has gone out.

    Mirrors the real timing: the publish happens inside the awaited call, and
    the MQTT callback thread fills the slot a fraction of a second later.
    """
    for _ in range(200):
        await asyncio.sleep(0.005)
        commands = _published(client)
        if commands:
            break
    else:  # pragma: no cover - the request always goes out
        raise AssertionError("no request was published")
    payload = dict(response)
    if echo_sequence:
        payload["sequence_id"] = commands[-1]["print"]["sequence_id"]
    client._handle_cali_result_response(payload)


class TestGetExtrusionCaliResult:
    @pytest.mark.asyncio
    async def test_matched_by_the_echoed_sequence_id(self):
        client = _client()
        task = asyncio.create_task(client.get_extrusion_cali_result("0.4", timeout=2.0))
        await _answer_after_publish(client, CAPTURED_RESULT)
        result = await task

        assert result is not None
        assert result["filaments"][0]["k_value"] == "0.018612"
        assert result["filaments"][0]["n_coef"] == "0.750000"
        sent = _published(client)[0]["print"]
        assert sent["command"] == "extrusion_cali_get_result"
        assert sent["nozzle_diameter"] == "0.4"

    @pytest.mark.asyncio
    async def test_a_broadcast_with_another_sequence_id_still_matches_on_the_nozzle(self):
        """Firmware that does not echo the id has to be served too -- the same
        fallback ``_handle_kprofile_response`` keeps (#1748)."""
        client = _client()
        task = asyncio.create_task(client.get_extrusion_cali_result("0.4", timeout=2.0))
        await _answer_after_publish(client, {**CAPTURED_RESULT, "sequence_id": "99999"}, echo_sequence=False)
        assert (await task) is not None

    @pytest.mark.asyncio
    async def test_a_broadcast_for_a_different_nozzle_does_not_resolve_the_wait(self):
        client = _client()
        task = asyncio.create_task(client.get_extrusion_cali_result("0.4", timeout=0.4, max_retries=1))
        await _answer_after_publish(
            client,
            {**CAPTURED_RESULT, "nozzle_diameter": "0.6", "sequence_id": "77777"},
            echo_sequence=False,
        )
        assert (await task) is None

    @pytest.mark.asyncio
    async def test_three_attempts_then_none(self):
        client = _client()
        assert await client.get_extrusion_cali_result("0.4", timeout=0.05, max_retries=3) is None
        commands = [c["print"]["command"] for c in _published(client)]
        assert commands == ["extrusion_cali_get_result"] * 3

    @pytest.mark.asyncio
    async def test_a_failure_is_returned_not_swallowed(self):
        """``result: "fail"`` is an answer, and its reason belongs in front of
        the user rather than being reported as "no result"."""
        client = _client()
        failure = {
            "command": "extrusion_cali_get_result",
            "result": "fail",
            "reason": "Unsupport",
            "nozzle_diameter": "0.4",
            "filaments": [],
        }
        task = asyncio.create_task(client.get_extrusion_cali_result("0.4", timeout=2.0))
        await _answer_after_publish(client, failure)
        result = await task
        assert result is not None
        assert result["result"] == "fail"
        assert result["reason"] == "Unsupport"

    @pytest.mark.asyncio
    async def test_not_connected_returns_none_without_publishing(self):
        client = _client()
        client.state.connected = False
        assert await client.get_extrusion_cali_result("0.4") is None
        assert client._client.publish.call_count == 0

    def test_the_result_does_not_clobber_the_fitted_nozzle(self):
        """The response echoes the *requested* diameter and carries no status
        telemetry, exactly like ``extrusion_cali_get`` -- feeding it to the
        state parser cost the real nozzle size once already (#2663)."""
        client = _client()
        client.state.nozzles[0].nozzle_diameter = "0.6"
        client._process_message({"print": {**CAPTURED_RESULT, "sequence_id": "1"}})
        assert client.state.nozzles[0].nozzle_diameter == "0.6"


class TestWrites:
    def test_set_kprofile_still_sends_the_old_n_coef_by_default(self):
        """Every existing caller depends on this default. Changing it would
        rewrite the coefficient of every hand-entered profile."""
        client = _client()
        client.set_kprofile(filament_id="GFA01", name="Manual", k_value="0.020000")
        entry = _published(client)[0]["print"]["filaments"][0]
        assert entry["n_coef"] == "0.000000"

    def test_set_kprofile_can_carry_a_measured_n_coef(self):
        client = _client()
        client.set_kprofile(
            filament_id="GFA01",
            name="Measured",
            k_value="0.019000",
            n_coef="0.750000",
        )
        entry = _published(client)[0]["print"]["filaments"][0]
        assert entry["n_coef"] == "0.750000"

    def test_set_measured_kprofile_publishes_studios_payload(self):
        client = _client()
        filament = {
            "ams_id": 0,
            "extruder_id": 0,
            "filament_id": "GFA01",
            "k_value": "0.019000",
            "n_coef": "0.750000",
            "name": "Bambu PLA Matte",
            "nozzle_diameter": "0.4",
            "nozzle_id": "HS00-0.4",
            "nozzle_pos": 0,
            "nozzle_sn": "N/A",
            "setting_id": "",
            "slot_id": 0,
            "tray_id": 0,
        }
        seq = client.set_measured_kprofile("0.4", filament)

        assert seq is not None
        sent = _published(client)[0]["print"]
        assert sent["command"] == "extrusion_cali_set"
        assert sent["nozzle_diameter"] == "0.4"
        assert sent["sequence_id"] == seq
        # Verbatim, in particular with no cali_idx added on the way out.
        assert sent["filaments"] == [filament]
        assert "cali_idx" not in sent["filaments"][0]
        # The ack slot is armed before the publish, so a 70-150 ms answer
        # cannot arrive before anyone is waiting for it.
        assert seq in client._pending_cali_acks

    def test_set_measured_kprofile_refuses_when_disconnected(self):
        client = _client()
        client.state.connected = False
        assert client.set_measured_kprofile("0.4", {"filament_id": "GFA01"}) is None
        assert client._client.publish.call_count == 0


# Bambu Studio's own project_file for the calibration job, verbatim from
# mqtt-tap/tap-0938BJ611001133-20260920-103100.jsonl (run 1, sequence_id
# 20001). Kept whole rather than as a handful of assertions: the point of this
# fixture is that a future change to the shared payload is compared against
# what Studio actually sent, not against what somebody remembered.
STUDIO_PROJECT_FILE = {
    "ams_mapping": [0],
    "ams_mapping2": [{"ams_id": 0, "slot_id": 0}],
    "auto_bed_leveling": 0,
    "bed_leveling": True,
    "bed_type": "textured_plate",
    "cfg": "0",
    "command": "project_file",
    "extrude_cali_flag": 1,
    "extrude_cali_manual_mode": 0,
    "file": "auto_pa_line_calib_mode.gcode.3mf",
    "flow_cali": True,
    "layer_inspect": True,
    "md5": "2DF378A8D92ECD391B3C5CDE6F06B6EC",
    "nozzle_offset_cali": 0,
    "param": "Metadata/plate_1.gcode",
    "profile_id": "0",
    "project_id": "0",
    "sequence_id": "20001",
    "subtask_id": "0",
    "subtask_name": "auto_pa_line_calib_mode",
    "task_id": "0",
    "timelapse": False,
    "url": "brtc://emmc/auto_pa_line_calib_mode.gcode.3mf",
    "use_ams": True,
    "vibration_cali": False,
}

# Fields where Bambuddy deliberately differs, each for a reason that has
# nothing to do with this calibration:
#   url/file  - Bambuddy uploads over FTP, so the job is ftp://<name>; Studio
#               pushed it to the printer's internal storage.
#   md5       - "" means "skip validation"; Bambuddy has never had the digest
#               at dispatch time and every print it starts sends "".
#   bed_type  - hardcoded "auto" for every Bambuddy print. The plate that
#               matters is curr_bed_type INSIDE the G-code, which came from the
#               slice.
#   bed_leveling - a JSON bool that is true only for the explicit "on" state,
#               while auto_bed_leveling carries the tri-state int. Studio sent
#               the bool true with the int 0; the int is what the G-code
#               branches on (M1002 judge_flag g29_before_print_flag -> M622 J0
#               -> plain G28), so both files take the same no-mesh branch.
#   cfg       - removed upstream (#3040); firmware ignores it.
#   *_id      - unique per submission since #1011.
#   ams_mapping/ams_mapping2 - per slot; the capture calibrated tray 0 and
#               these are asserted on their own below.
_EXPECTED_DIVERGENCES = {
    "ams_mapping",
    "ams_mapping2",
    "url",
    "file",
    "md5",
    "bed_type",
    "bed_leveling",
    "cfg",
    "sequence_id",
    "subtask_name",
    "project_id",
    "subtask_id",
    "task_id",
}


class TestCalibrationProjectFile:
    """What the flow-dynamics dispatch actually puts on the wire.

    The run is a print, so the only thing that makes it a *calibration* print
    is this payload. ``extrude_cali_flag: 1`` is the whole feature; everything
    else here exists so that a change to the shared project_file payload for
    some other caller cannot quietly stop this one measuring.
    """

    def _dispatch(self) -> dict:
        from backend.app.services.pa_calibration import PA_REMOTE_FILENAME, start_calibration_print

        client = _client()
        client.model = "H2S"

        published: dict = {}

        def _start(printer_id, filename, **kwargs):
            client.start_print(filename, **kwargs)
            published.update(json.loads(client._client.publish.call_args.args[1])["print"])
            return True

        from backend.app.services import pa_calibration as pa

        original = pa.printer_manager.start_print
        pa.printer_manager.start_print = _start
        try:
            row = MagicMock()
            row.printer_id = 1
            row.remote_filename = PA_REMOTE_FILENAME
            row.tray_id = 1
            row.ams_id = 0
            assert start_calibration_print(row) is True
        finally:
            pa.printer_manager.start_print = original
        return published

    def test_the_calibration_flag_is_forced_on(self):
        cmd = self._dispatch()
        # The one field the measurement depends on. flow_cali="on" is the only
        # way to put it on the wire: the field is not independently settable.
        assert cmd["extrude_cali_flag"] == 1
        assert cmd["flow_cali"] is True

    def test_no_mesh_levelling_is_requested(self):
        cmd = self._dispatch()
        # auto_bed_leveling 0 selects the g29_before_print_flag J0 branch, a
        # plain G28. Studio's job took the same branch.
        assert cmd["auto_bed_leveling"] == 0

    def test_it_matches_studios_payload_apart_from_the_known_divergences(self):
        cmd = self._dispatch()
        for field, expected in STUDIO_PROJECT_FILE.items():
            if field in _EXPECTED_DIVERGENCES:
                continue
            assert cmd[field] == expected, f"{field} diverges from the capture"

    def test_the_divergences_are_the_ones_we_documented(self):
        cmd = self._dispatch()
        assert cmd["url"] == "ftp://bambuddy_pa_cali.3mf"
        assert cmd["md5"] == ""
        assert cmd["bed_type"] == "auto"
        assert cmd["bed_leveling"] is False
        assert "cfg" not in cmd

    def test_the_calibrated_slot_is_the_only_one_mapped(self):
        cmd = self._dispatch()
        assert cmd["ams_mapping"] == [1]
        assert cmd["ams_mapping2"] == [{"ams_id": 0, "slot_id": 1}]
