"""Unit tests for maintenance rod-type filtering logic."""

import pytest

from backend.app.api.routes.maintenance import _should_apply_to_printer, _type_applies_to_printer
from backend.app.models.maintenance import MaintenanceType
from backend.app.services import maintenance_actions


class TestShouldApplyToPrinter:
    """Tests for _should_apply_to_printer() model-specific filtering."""

    # Carbon rod tasks should only apply to X1/P1 models
    @pytest.mark.parametrize("model", ["X1C", "X1", "X1E", "P1P", "P1S"])
    def test_carbon_rod_tasks_apply_to_carbon_models(self, model: str):
        assert _should_apply_to_printer("Clean Carbon Rods", model) is True

    def test_carbon_rod_tasks_do_not_apply_to_p2s(self):
        """P2S has steel rods, not carbon rods (#640)."""
        assert _should_apply_to_printer("Clean Carbon Rods", "P2S") is False

    def test_carbon_rod_tasks_do_not_apply_to_a1(self):
        assert _should_apply_to_printer("Clean Carbon Rods", "A1") is False

    # Steel rod tasks should only apply to P2S
    def test_steel_rod_tasks_apply_to_p2s(self):
        assert _should_apply_to_printer("Lubricate Steel Rods", "P2S") is True
        assert _should_apply_to_printer("Clean Steel Rods", "P2S") is True

    def test_steel_rod_tasks_do_not_apply_to_x1c(self):
        assert _should_apply_to_printer("Lubricate Steel Rods", "X1C") is False
        assert _should_apply_to_printer("Clean Steel Rods", "X1C") is False

    def test_steel_rod_tasks_do_not_apply_to_a1(self):
        assert _should_apply_to_printer("Lubricate Steel Rods", "A1") is False

    # Linear rail tasks should only apply to A1/H2 models
    @pytest.mark.parametrize("model", ["A1", "A1 Mini", "H2D", "H2C", "H2S"])
    def test_linear_rail_tasks_apply_to_rail_models(self, model: str):
        assert _should_apply_to_printer("Lubricate Linear Rails", model) is True
        assert _should_apply_to_printer("Clean Linear Rails", model) is True

    def test_linear_rail_tasks_do_not_apply_to_p2s(self):
        assert _should_apply_to_printer("Lubricate Linear Rails", "P2S") is False

    # Universal tasks apply to all models
    @pytest.mark.parametrize("model", ["X1C", "P2S", "A1", "H2D"])
    def test_universal_tasks_apply_to_all(self, model: str):
        assert _should_apply_to_printer("Clean Nozzle/Hotend", model) is True
        assert _should_apply_to_printer("Check Belt Tension", model) is True

    # Unknown models default to carbon (legacy behavior)
    def test_unknown_model_defaults_to_carbon(self):
        assert _should_apply_to_printer("Clean Carbon Rods", "UNKNOWN") is True
        assert _should_apply_to_printer("Lubricate Steel Rods", "UNKNOWN") is False
        assert _should_apply_to_printer("Lubricate Linear Rails", "UNKNOWN") is False


class TestVisionEncoderGate:
    """The vision encoder gate keys on the action column, not the name (#3127).

    update_maintenance_type has no is_system guard, so a seeded type can be
    renamed. The name would stop matching; the action cannot be changed over
    the API at all, and it is the column the dispatcher itself reads.
    """

    def test_the_seeded_name_gates_when_the_action_is_missing(self):
        # Fallback for a row whose action has not been backfilled yet.
        assert _should_apply_to_printer("Vision Encoder Calibration", "H2D") is True
        assert _should_apply_to_printer("Vision Encoder Calibration", "X1C") is False

    @pytest.mark.parametrize("model", ["H2S", "H2D", "H2D Pro", "H2C"])
    def test_a_renamed_vision_type_still_applies_to_the_h2_series(self, model: str):
        renamed = MaintenanceType(
            name="Encoder check",
            is_system=True,
            action=maintenance_actions.ACTION_MOTION_PRECISION,
        )
        assert _type_applies_to_printer(renamed, model) is True

    @pytest.mark.parametrize("model", ["X1C", "X1E", "P1S", "P1P", "A1", "P2S", None])
    def test_a_renamed_vision_type_still_stays_off_everything_else(self, model: str | None):
        renamed = MaintenanceType(
            name="Encoder check",
            is_system=True,
            action=maintenance_actions.ACTION_MOTION_PRECISION,
        )
        assert _type_applies_to_printer(renamed, model) is False

    @pytest.mark.parametrize("model", ["H2D", "X1C", "P1S", "A1", "P2S"])
    def test_a_renamed_levelling_type_still_applies_everywhere(self, model: str):
        renamed = MaintenanceType(
            name="Kalibrierung",
            is_system=True,
            action=maintenance_actions.ACTION_CALIBRATION,
        )
        assert _type_applies_to_printer(renamed, model) is True
