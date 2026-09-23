"""Choosing the measured entry, building the write, and reading the slot.

The entry-picking rules are the ones that decide whether a K value gets
attributed to the right filament and the right nozzle, so they refuse rather
than guess wherever the answer is not unique.
"""

from types import SimpleNamespace

import pytest

from backend.app.models.pa_calibration import PaCalibrationRun
from backend.app.services import pa_calibration as pa

RESULT = {
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
            "nozzle_id": "HS01-0.4",
            "slot_id": 0,
            "tray_id": -1,
        }
    ],
}


class TestPickResultEntry:
    def test_the_captured_result(self):
        entry = pa.pick_result_entry(RESULT, filament_id="GFA01", extruder_id=0)
        assert entry["k_value"] == "0.018612"

    def test_a_failure_surfaces_the_printers_reason(self):
        response = {"result": "fail", "reason": "Unsupport", "filaments": []}
        with pytest.raises(pa.PaCalibrationError, match="Unsupport"):
            pa.pick_result_entry(response, filament_id="GFA01", extruder_id=0)

    def test_an_empty_list_fails(self):
        with pytest.raises(pa.PaCalibrationError):
            pa.pick_result_entry({"result": "success", "filaments": []}, filament_id="GFA01", extruder_id=0)

    def test_another_filament_does_not_match(self):
        with pytest.raises(pa.PaCalibrationError):
            pa.pick_result_entry(RESULT, filament_id="GFG02", extruder_id=0)

    def test_another_extruder_does_not_match(self):
        """The guard that keeps a dual-nozzle machine out of this feature until
        its per-extruder result shape is measured."""
        with pytest.raises(pa.PaCalibrationError):
            pa.pick_result_entry(RESULT, filament_id="GFA01", extruder_id=1)

    def test_two_matches_refuse_rather_than_guess(self):
        doubled = {**RESULT, "filaments": RESULT["filaments"] * 2}
        with pytest.raises(pa.PaCalibrationError, match="refusing to guess"):
            pa.pick_result_entry(doubled, filament_id="GFA01", extruder_id=0)

    def test_a_missing_n_coef_fails(self):
        """Never invent one: the coefficient is part of what the profile means
        and the printer is its only source."""
        stripped = {**RESULT, "filaments": [{**RESULT["filaments"][0], "n_coef": ""}]}
        with pytest.raises(pa.PaCalibrationError, match="n_coef"):
            pa.pick_result_entry(stripped, filament_id="GFA01", extruder_id=0)


class TestBuildWritePayload:
    def _row(self, **overrides) -> PaCalibrationRun:
        defaults = {
            "printer_id": 1,
            "ams_id": 0,
            "slot_id": 0,
            "tray_id": 0,
            "extruder_id": 0,
            "filament_id": "GFA01",
            "nozzle_diameter": "0.4",
            "nozzle_id": "HS01-0.4",
            "result_raw": RESULT["filaments"][0],
            "k_value": 0.018612,
            "n_coef": "0.750000",
        }
        defaults.update(overrides)
        return PaCalibrationRun(**defaults)

    def test_matches_the_captured_write(self):
        payload = pa.build_write_payload(self._row(), profile_name="Bambu PLA Matte")
        assert payload == {
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

    def test_a_malformed_nozzle_id_refuses_the_write(self):
        row = self._row(result_raw={**RESULT["filaments"][0], "nozzle_id": ""}, nozzle_id="")
        with pytest.raises(ValueError):
            pa.build_write_payload(row, profile_name="whatever")


class TestGlobalTrayId:
    @pytest.mark.parametrize(
        ("ams_id", "slot_id", "expected"),
        [
            (0, 0, 0),
            (0, 3, 3),
            (1, 1, 5),
            (3, 2, 14),
            # AMS-HT: one slot per unit, sharing the unit's global id.
            (128, 0, 128),
            (135, 0, 135),
            # External spool.
            (254, 0, 254),
            (255, 0, 254),
        ],
    )
    def test_matches_the_frontend_helper(self, ams_id, slot_id, expected):
        assert pa.global_tray_id(ams_id, slot_id) == expected


def _state(*, connected=True, state="IDLE", trays=None, nozzle=("HS01", "0.4"), external=None):
    raw = {}
    if trays is not None:
        raw["ams"] = [{"id": "0", "tray": trays}]
    if external is not None:
        raw["vt_tray"] = external if isinstance(external, list) else [external]
    return SimpleNamespace(
        connected=connected,
        state=state,
        raw_data=raw,
        nozzles=[SimpleNamespace(nozzle_type=nozzle[0], nozzle_diameter=nozzle[1])],
        ams_extruder_map={},
        ams_switch_inlet={},
    )


class TestFindSlot:
    def test_an_ams_slot(self):
        trays = [{"id": "0", "state": 10}, {"id": "1", "tray_info_idx": "GFA01", "tray_type": "PLA"}]
        tray = pa.find_slot(_state(trays=trays), 0, 1)
        assert pa.slot_filament_id(tray) == "GFA01"

    def test_an_empty_slot_reports_no_filament(self):
        trays = [{"id": "0", "state": 10}]
        assert pa.slot_filament_id(pa.find_slot(_state(trays=trays), 0, 0)) == ""

    def test_the_external_spool(self):
        state = _state(external={"id": "255", "tray_info_idx": "GFG02", "tray_type": "PETG"})
        assert pa.slot_filament_id(pa.find_slot(state, 254, 0)) == "GFG02"

    def test_a_dual_nozzle_machines_two_external_trays_are_told_apart(self):
        # Ext-L is vt_tray 254 and Ext-R is 255; the UI addresses both as
        # ams_id 255 with slot 0/1, so taking the first entry would calibrate
        # whichever spool the firmware happened to list first.
        state = _state(
            external=[
                {"id": "254", "tray_info_idx": "GFA01", "tray_type": "PLA"},
                {"id": "255", "tray_info_idx": "GFG02", "tray_type": "PETG"},
            ]
        )
        assert pa.slot_filament_id(pa.find_slot(state, 255, 0)) == "GFA01"
        assert pa.slot_filament_id(pa.find_slot(state, 255, 1)) == "GFG02"

    def test_a_missing_unit(self):
        assert pa.find_slot(_state(trays=[{"id": "0"}]), 2, 0) is None

    def test_a_malformed_id_does_not_raise(self):
        state = _state(trays=[{"id": "not a number", "tray_info_idx": "GFA01"}])
        assert pa.find_slot(state, 0, 0) is None


class TestSlotFilamentName:
    def test_prefers_the_sub_brand(self):
        assert pa.slot_filament_name({"tray_sub_brands": "PLA Matte", "tray_type": "PLA"}) == "PLA Matte"

    def test_falls_back_to_the_type(self):
        assert pa.slot_filament_name({"tray_type": "PETG"}) == "PETG"

    def test_nothing_known(self):
        assert pa.slot_filament_name(None) == ""


class TestNozzleIdForExtruder:
    def test_builds_the_calibration_table_form(self):
        assert pa.nozzle_id_for_extruder(_state(), 0) == "HS01-0.4"

    def test_unknown_nozzle_reports_none(self):
        assert pa.nozzle_id_for_extruder(_state(nozzle=("", "")), 0) is None

    def test_an_out_of_range_extruder_falls_back_to_the_only_nozzle(self):
        assert pa.nozzle_id_for_extruder(_state(), 1) == "HS01-0.4"


class TestBlockingReasons:
    def _printer(self, model="H2S"):
        return SimpleNamespace(model=model, name="H2S", ip_address="10.0.0.1", access_code="x")

    def _call(self, **overrides):
        kwargs = {
            "printer": self._printer(),
            "state": _state(trays=[{"id": "0", "tray_info_idx": "GFA01", "tray_type": "PLA"}]),
            "ams_id": 0,
            "slot_id": 0,
            "extruder_id": 0,
            "slicer_url": "http://127.0.0.1:3001",
            "has_active_run": False,
            "printer_reserved": False,
        }
        kwargs.update(overrides)
        return pa.blocking_reasons(**kwargs)

    def test_ready(self):
        assert self._call() == []

    def test_unsupported_model_is_the_only_reason_reported(self):
        """Listing four more blockers for a machine that cannot do this at all
        would suggest it might."""
        assert self._call(printer=self._printer("X1C")) == ["model_not_supported"]

    def test_offline(self):
        assert self._call(state=None) == ["printer_offline"]
        assert self._call(state=_state(connected=False)) == ["printer_offline"]

    def test_busy(self):
        assert "printer_busy" in self._call(state=_state(state="RUNNING", trays=[{"id": "0"}]))

    def test_reserved(self):
        assert "printer_reserved" in self._call(printer_reserved=True)

    def test_run_already_active(self):
        assert "run_already_active" in self._call(has_active_run=True)

    def test_no_sidecar(self):
        assert "slicer_not_configured" in self._call(slicer_url="")

    def test_empty_slot(self):
        assert "slot_empty" in self._call(state=_state(trays=[{"id": "0", "state": 10}]))

    def test_unknown_nozzle(self):
        state = _state(trays=[{"id": "0", "tray_info_idx": "GFA01"}], nozzle=("", ""))
        assert "nozzle_unknown" in self._call(state=state)


class TestRunToResponse:
    """The response map is hand-maintained, like archive_to_response.

    A column added to the model and to the documented response schema but not
    to ``run_to_response`` silently never reaches the client, which is the
    exact trap CONTRIBUTING warns about. Comparing the two in a test is how a
    missing line becomes a failure rather than an empty field in the UI.
    """

    def test_it_carries_every_field_the_schema_documents(self):
        from types import SimpleNamespace

        from backend.app.schemas.pa_calibration import PaCalibrationRunResponse

        row = SimpleNamespace(**dict.fromkeys(PaCalibrationRunResponse.model_fields, None))
        assert set(pa.run_to_response(row)) == set(PaCalibrationRunResponse.model_fields)
