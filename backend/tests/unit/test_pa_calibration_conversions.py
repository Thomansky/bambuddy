"""The two conversions between a measured K value and the one that gets stored.

Every number here comes from a capture of Bambu Studio running a flow-dynamics
calibration on an H2S twice
(``mqtt-tap/tap-0938BJ611001133-20260920-103100.jsonl``). They are pinned
because the printer keeps whatever is written to it: a wrong value here is not
a failed test run, it is a filament permanently calibrated wrong.
"""

import pytest

from backend.app.utils.pa_calibration import (
    build_extrusion_cali_set_filament,
    round_k_value,
    write_nozzle_id,
)
from backend.app.utils.printer_models import PA_CALIBRATION_MODELS, supports_sliced_pa_calibration


class TestRoundKValue:
    @pytest.mark.parametrize(
        ("measured", "written"),
        [
            # Run 1 and run 2 of the capture, verbatim.
            ("0.018612", "0.019000"),
            ("0.021994", "0.022000"),
        ],
    )
    def test_matches_the_captured_runs(self, measured, written):
        assert round_k_value(measured) == written

    def test_half_rounds_up_not_to_even(self):
        """The reason this uses Decimal/ROUND_HALF_UP and not ``round``.

        ``round(0.0185, 3)`` is 0.018 under banker's rounding, which would
        write a different K value than Studio would.
        """
        assert round(0.0185, 3) == 0.018  # the behaviour being avoided
        assert round_k_value(0.0185) == "0.019000"

    def test_accepts_a_float(self):
        assert round_k_value(0.018612) == "0.019000"

    def test_refuses_a_non_number(self):
        with pytest.raises(ValueError):
            round_k_value("not a number")


class TestWriteNozzleId:
    @pytest.mark.parametrize(
        ("fitted", "written"),
        [
            # Both captured runs: the result reports HS01-0.4, the write says
            # HS00-0.4.
            ("HS01-0.4", "HS00-0.4"),
            ("HH01-0.6", "HH00-0.6"),
            # Already normalised — unchanged, not double-translated.
            ("HS00-0.4", "HS00-0.4"),
            ("HH00-0.8", "HH00-0.8"),
        ],
    )
    def test_forces_the_position_digits_to_zero(self, fitted, written):
        assert write_nozzle_id(fitted) == written

    def test_keeps_the_flow_letters(self):
        """The printer's own table held HH00-0.4 and HS00-0.4 for one filament,
        so the letters are part of the profile's identity, not a constant."""
        assert write_nozzle_id("HH01-0.4").startswith("HH")
        assert write_nozzle_id("HS01-0.4").startswith("HS")

    @pytest.mark.parametrize("bad", ["", "   ", "0.4", "HS-0.4", "HSS01-0.4", "HS1-0.4", None])
    def test_refuses_anything_else(self, bad):
        # An X1C reports an empty nozzle_id on every entry. Writing a K under a
        # guessed one would file it against a nozzle the user never used.
        with pytest.raises(ValueError):
            write_nozzle_id(bad)


class TestWritePayload:
    def _payload(self, **overrides):
        kwargs = {
            "ams_id": 0,
            "slot_id": 0,
            "extruder_id": 0,
            "filament_id": "GFA01",
            "k_value": "0.019000",
            "n_coef": "0.750000",
            "name": "Bambu PLA Matte",
            "nozzle_diameter": "0.4",
            "nozzle_id": "HS00-0.4",
        }
        kwargs.update(overrides)
        return build_extrusion_cali_set_filament(**kwargs)

    def test_reproduces_studios_entry_exactly(self):
        assert self._payload() == {
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

    def test_carries_no_cali_idx(self):
        """Without it the printer matches (filament_id, nozzle_id, extruder_id)
        and replaces in place; the capture's read-back shows cali_idx 1 changing
        while cali_idx 0 for the same filament stays untouched."""
        assert "cali_idx" not in self._payload()

    def test_tray_id_is_zero_whatever_slot_was_calibrated(self):
        # Single-nozzle firmware answers "invalid tray_id" to -1 (#2718), and
        # Studio sent 0 here even though the result reported -1.
        assert self._payload(slot_id=3, ams_id=1)["tray_id"] == 0
        assert self._payload(slot_id=3, ams_id=1)["slot_id"] == 3
        assert self._payload(slot_id=3, ams_id=1)["ams_id"] == 1

    def test_n_coef_is_whatever_was_measured(self):
        assert self._payload(n_coef="0.812345")["n_coef"] == "0.812345"


class TestSupportsSlicedPaCalibration:
    def test_the_allow_list_is_exactly_h2s(self):
        """Widening this is a deliberate edit that has to change this test too.

        Adding a model means its start G-code gate, its per-extruder result
        shape and its slot -> extruder mapping have all been verified on real
        hardware. H2D/H2D Pro are not in yet because guessing the extruder on a
        dual-nozzle machine writes a correct K to the wrong nozzle, silently.
        """
        assert sorted(PA_CALIBRATION_MODELS) == ["H2S"]

    @pytest.mark.parametrize("model", ["H2S", "O1S", "Bambu Lab H2S", "h2s", " H2S "])
    def test_supported(self, model):
        assert supports_sliced_pa_calibration(model) is True

    @pytest.mark.parametrize(
        "model",
        ["H2D", "H2D Pro", "O1D", "H2C", "X1C", "C11", "P1S", "A1", "A1 Mini", "X2D", "", None, "nonsense"],
    )
    def test_not_supported(self, model):
        assert supports_sliced_pa_calibration(model) is False
