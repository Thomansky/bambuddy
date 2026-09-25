"""Unit tests for the actionable-maintenance helpers (#3127)."""

from datetime import datetime, timezone

import pytest

from backend.app.services.maintenance_actions import (
    ACTION_CALIBRATION,
    ACTION_MOTION_PRECISION,
    CALIBRATION_CANCELLED_PRINT_ERROR,
    CALIBRATION_FLAGS,
    available_calibration_options,
    bed_condition_wait,
    bed_temp_below,
    compute_due_state,
    compute_schedule_next_at,
    current_bed_temperature,
    has_options,
    motion_precision_gcode_path,
    normalize_bed_temp_below,
    normalize_calibration_options,
    parse_schedule_time,
    resolve_run_outcome,
    response_action_options,
    run_options,
    selected_calibration_flags,
    stored_action_options,
)
from backend.app.utils.print_jobs import action_for_job, is_internal_printer_job, job_names_for_action, matches_action
from backend.app.utils.printer_models import has_vision_encoder

pytestmark = pytest.mark.unit


class TestOptions:
    def test_unconfigured_means_the_three_defaults(self):
        assert selected_calibration_flags(None) == ["bed_leveling", "vibration", "motor_noise"]

    def test_every_flag_is_normalised_to_a_bool(self):
        options = normalize_calibration_options({"bed_leveling": 1, "micro_lidar": True})
        assert set(options) == set(CALIBRATION_FLAGS)
        assert options["bed_leveling"] is True
        assert options["micro_lidar"] is True
        assert options["vibration"] is False

    def test_an_empty_set_selects_nothing(self):
        """An explicit empty dict is a real choice, not "use the defaults"."""
        assert selected_calibration_flags({}) == []

    def test_nozzle_offset_only_on_dual_nozzle_models(self):
        assert "nozzle_offset" in available_calibration_options("H2D")
        assert "nozzle_offset" not in available_calibration_options("X1C")
        assert "nozzle_offset" not in available_calibration_options(None)

    def test_micro_lidar_only_on_the_x1_series(self):
        # The lidar sits under the X1's toolhead and nowhere else; offering the
        # box on a P1S asks for a calibration that machine cannot run.
        for model in ("X1C", "X1", "X1E"):
            assert "micro_lidar" in available_calibration_options(model)
        for model in ("P1S", "P1P", "A1", "H2D", "H2S", None):
            assert "micro_lidar" not in available_calibration_options(model)

    def test_micro_lidar_gate_reads_the_x1_internal_codes(self):
        # A printer can report its internal code instead of a display name --
        # a virtual printer stores exactly these in its model column. The X1
        # codes are BL-P001 / BL-P002 / BL-P003 and C13; C11 and C12 are P1P
        # and P1S, which is what firmware_check, the virtual printer's manager
        # and mqtt_server, vp_model_fixes and STG_CUR_IDLE_BUG_MODELS all say.
        for code in ("BL-P001", "BL-P002", "BL-P003", "C13"):
            assert "micro_lidar" in available_calibration_options(code)
        for code in ("C11", "C12"):
            assert "micro_lidar" not in available_calibration_options(code)

    def test_the_other_flags_are_offered_everywhere(self):
        offered = available_calibration_options("P1S")
        for flag in ("bed_leveling", "vibration", "motor_noise", "high_temp_heatbed", "nozzle_clumping"):
            assert flag in offered


class TestRunOptions:
    def test_calibration_carries_its_flags(self):
        assert has_options(ACTION_CALIBRATION)
        options = run_options(ACTION_CALIBRATION, {"vibration": True})
        assert options["vibration"] is True and options["bed_leveling"] is False

    def test_calibration_with_nothing_selected_is_refused(self):
        with pytest.raises(ValueError):
            run_options(ACTION_CALIBRATION, {})

    def test_motion_precision_has_no_options(self):
        """The vision encoder job takes no flags; an empty set is not an error."""
        assert not has_options(ACTION_MOTION_PRECISION)
        assert run_options(ACTION_MOTION_PRECISION, None) is None
        assert run_options(ACTION_MOTION_PRECISION, {}) is None


class TestBedTempBelow:
    @pytest.mark.parametrize("value", [28, 28.5, 0.1, 120, 120.0])
    def test_accepts_a_temperature_in_range(self, value):
        assert normalize_bed_temp_below(value) == float(value)

    def test_null_means_no_condition(self):
        assert normalize_bed_temp_below(None) is None

    @pytest.mark.parametrize("value", [0, -5, 120.1, 500, "28", True, [28]])
    def test_rejects_out_of_range_and_non_numbers(self, value):
        with pytest.raises(ValueError):
            normalize_bed_temp_below(value)

    def test_rejects_more_than_one_decimal(self):
        with pytest.raises(ValueError):
            normalize_bed_temp_below(28.25)

    def test_the_stored_value_is_read_leniently(self):
        """A bad stored value is no condition rather than a crash in the scheduler."""
        assert bed_temp_below({"bed_leveling": True, "bed_temp_below": 28}) == 28.0
        assert bed_temp_below({"bed_leveling": True}) is None
        assert bed_temp_below({"bed_temp_below": "warm"}) is None
        assert bed_temp_below(None) is None

    def test_stored_options_fill_the_flags_and_keep_the_condition(self):
        stored = stored_action_options(ACTION_CALIBRATION, {"bed_leveling": True, "bed_temp_below": 28})
        assert stored["bed_leveling"] is True and stored["vibration"] is False
        assert stored["bed_temp_below"] == 28.0

    def test_stored_options_drop_a_null_condition(self):
        stored = stored_action_options(ACTION_CALIBRATION, {"bed_leveling": True, "bed_temp_below": None})
        assert "bed_temp_below" not in stored
        assert stored_action_options(ACTION_MOTION_PRECISION, {}) == {}
        assert stored_action_options(ACTION_MOTION_PRECISION, {"bed_temp_below": 30}) == {"bed_temp_below": 30.0}

    def test_response_options_carry_the_condition_only_when_set(self):
        assert "bed_temp_below" not in response_action_options(ACTION_CALIBRATION, None)
        assert (
            response_action_options(ACTION_CALIBRATION, {"bed_leveling": True, "bed_temp_below": 28})["bed_temp_below"]
            == 28.0
        )
        assert response_action_options(ACTION_MOTION_PRECISION, None) is None
        assert response_action_options(ACTION_MOTION_PRECISION, {}) is None
        assert response_action_options(ACTION_MOTION_PRECISION, {"bed_temp_below": 28}) == {"bed_temp_below": 28.0}

    def test_the_bed_temperature_comes_from_the_state_temperatures(self):
        class State:
            temperatures = {"bed": 34, "nozzle": 200}

        assert current_bed_temperature(State()) == 34.0
        State.temperatures = {"nozzle": 200}
        assert current_bed_temperature(State()) is None
        assert current_bed_temperature(None) is None

    def test_condition_wait_reasons(self):
        assert bed_condition_wait(None, 80.0) is None
        assert bed_condition_wait(30.0, None) == ("bed_temp_unknown", None)
        assert bed_condition_wait(30.0, 30.0) == ("bed_too_warm", {"bed_temp": 30.0, "threshold": 30.0})
        assert bed_condition_wait(30.0, 34.26) == ("bed_too_warm", {"bed_temp": 34.3, "threshold": 30.0})
        assert bed_condition_wait(30.0, 29.9) is None


class TestMotionPrecisionPath:
    def test_the_learned_directory_wins(self):
        assert motion_precision_gcode_path("H2D", "O1E") == "/usr/etc/print/O1E/calibrate_motion_precision.gcode"

    @pytest.mark.parametrize(
        ("model", "directory"),
        [("H2S", "O1S"), ("O1S", "O1S"), ("H2D", "O1D"), ("H2D Pro", "O1D"), ("O2D", "O1D"), ("H2C", "O1C")],
    )
    def test_the_model_map_when_nothing_was_learned(self, model, directory, caplog):
        with caplog.at_level("WARNING"):
            path = motion_precision_gcode_path(model, None)
        assert path == f"/usr/etc/print/{directory}/calibrate_motion_precision.gcode"
        assert "assuming" in caplog.text

    @pytest.mark.parametrize("model", ["X1C", "P1S", "A1", "X2D", None, ""])
    def test_models_without_a_vision_encoder_are_refused(self, model):
        assert motion_precision_gcode_path(model, "O1S") is None
        assert not has_vision_encoder(model)

    def test_the_h2_series_has_one(self):
        for model in ("H2S", "H2D", "H2D Pro", "H2C", "O1S", "O1C2"):
            assert has_vision_encoder(model), model


class TestScheduleTime:
    @pytest.mark.parametrize("value", ["06:00", "6:5", "23:59", " 00:00 "])
    def test_accepts_hh_mm(self, value):
        assert parse_schedule_time(value) is not None

    @pytest.mark.parametrize("value", [None, "", "6", "24:00", "12:60", "ab:cd", "1:2:3"])
    def test_rejects_everything_else(self, value):
        assert parse_schedule_time(value) is None


class TestComputeScheduleNextAt:
    """TZ is pinned to UTC so the local wall clock and the stored UTC agree."""

    @pytest.fixture(autouse=True)
    def _utc(self, monkeypatch):
        monkeypatch.setenv("TZ", "UTC")

    def test_later_today_when_today_is_a_scheduled_day(self):
        # 2026-09-16 is a Wednesday (weekday 2)
        now = datetime(2026, 9, 16, 5, 0, tzinfo=timezone.utc)
        assert compute_schedule_next_at([2], "06:00", now=now) == datetime(2026, 9, 16, 6, 0)

    def test_skips_to_the_next_scheduled_weekday_once_the_time_has_passed(self):
        now = datetime(2026, 9, 16, 7, 0, tzinfo=timezone.utc)
        # Saturday is weekday 5 -> 2026-09-19
        assert compute_schedule_next_at([5], "06:00", now=now) == datetime(2026, 9, 19, 6, 0)

    def test_wraps_to_next_week(self):
        """Saturday 07:00, schedule Saturday 06:00 -> a week later, not today."""
        now = datetime(2026, 9, 19, 7, 0, tzinfo=timezone.utc)
        assert compute_schedule_next_at([5], "06:00", now=now) == datetime(2026, 9, 26, 6, 0)

    def test_an_exact_hit_counts_as_already_passed(self):
        now = datetime(2026, 9, 19, 6, 0, tzinfo=timezone.utc)
        assert compute_schedule_next_at([5], "06:00", now=now) == datetime(2026, 9, 26, 6, 0)

    def test_picks_the_earliest_of_several_days(self):
        now = datetime(2026, 9, 16, 7, 0, tzinfo=timezone.utc)  # Wednesday
        assert compute_schedule_next_at([5, 4, 0], "06:00", now=now) == datetime(2026, 9, 18, 6, 0)  # Friday

    def test_returns_none_without_days_or_time(self):
        now = datetime(2026, 9, 16, 7, 0, tzinfo=timezone.utc)
        assert compute_schedule_next_at([], "06:00", now=now) is None
        assert compute_schedule_next_at([5], None, now=now) is None
        assert compute_schedule_next_at([5], "nope", now=now) is None

    def test_accepts_naive_utc_now(self):
        now = datetime(2026, 9, 16, 5, 0)
        assert compute_schedule_next_at([2], "06:00", now=now) == datetime(2026, 9, 16, 6, 0)


class TestComputeScheduleNextAtLocalZone:
    def test_time_of_day_is_read_in_the_local_zone(self, monkeypatch):
        """06:00 Berlin in September is 04:00 UTC (CEST)."""
        monkeypatch.setenv("TZ", "Europe/Berlin")
        now = datetime(2026, 9, 16, 0, 0, tzinfo=timezone.utc)
        assert compute_schedule_next_at([2], "06:00", now=now) == datetime(2026, 9, 16, 4, 0)

    def test_stays_on_the_local_hour_across_the_dst_change(self, monkeypatch):
        """Europe falls back on 2026-10-25; Monday 06:00 after it is 05:00 UTC."""
        monkeypatch.setenv("TZ", "Europe/Berlin")
        now = datetime(2026, 10, 24, 12, 0, tzinfo=timezone.utc)  # Saturday, still CEST
        assert compute_schedule_next_at([0], "06:00", now=now) == datetime(2026, 10, 26, 5, 0)

    def test_the_repeated_hour_of_the_fall_back_night_is_compared_in_utc(self, monkeypatch):
        """Sunday 02:30 schedule; the 02:30 CEST run (00:30 UTC) is over and it
        is now 02:20 CET (01:20 UTC), inside the repeated hour. The wall clock
        says 02:30 fold=0 is still ahead; in UTC it is fifty minutes gone, so
        the next slot is a week away, not the run that just finished."""
        monkeypatch.setenv("TZ", "Europe/Berlin")
        now = datetime(2026, 10, 25, 1, 20, tzinfo=timezone.utc)  # 02:20 CET, fold=1
        assert compute_schedule_next_at([6], "02:30", now=now) == datetime(2026, 11, 1, 1, 30)

    def test_the_first_pass_of_the_repeated_hour_still_finds_the_earlier_slot(self, monkeypatch):
        monkeypatch.setenv("TZ", "Europe/Berlin")
        now = datetime(2026, 10, 25, 0, 0, tzinfo=timezone.utc)  # 02:00 CEST, fold=0
        assert compute_schedule_next_at([6], "02:30", now=now) == datetime(2026, 10, 25, 0, 30)


class TestDueState:
    NOW = datetime(2026, 9, 16, 12, 0, tzinfo=timezone.utc)

    def test_hours_interval_due_and_warning(self):
        due = compute_due_state(100, "hours", 250, 140, None, self.NOW)
        assert due.hours_since == 110
        assert due.hours_until == -10
        assert due.is_due and not due.is_warning
        assert due.days_since is None and due.days_until is None

        warn = compute_due_state(100, "hours", 235, 140, None, self.NOW)
        assert warn.is_warning and not warn.is_due

    def test_days_interval_counts_calendar_days(self):
        performed = datetime(2026, 9, 1, 12, 0)  # naive UTC, as stored
        due = compute_due_state(30, "days", 0, 0, performed, self.NOW)
        assert due.days_since == pytest.approx(15)
        assert due.days_until == pytest.approx(15)
        assert not due.is_due and not due.is_warning
        assert due.hours_until == 0

    def test_days_interval_never_performed_is_due(self):
        due = compute_due_state(30, "days", 0, 0, None, self.NOW)
        assert due.is_due


class TestRunOutcome:
    def test_finish_completes(self):
        assert resolve_run_outcome("completed", None) == "completed"

    def test_cancel_from_the_printer_screen(self):
        assert resolve_run_outcome("failed", CALIBRATION_CANCELLED_PRINT_ERROR) == "cancelled"

    def test_abort_to_idle_is_a_cancel(self):
        assert resolve_run_outcome("aborted", None) == "cancelled"

    def test_any_other_failure_fails(self):
        assert resolve_run_outcome("failed", 83886081) == "failed"
        assert resolve_run_outcome("failed", None) == "failed"


class TestActionJobs:
    def test_the_h2s_levelling_run_as_captured(self):
        path = "/usr/etc/print/H2S/auto_cali_for_user_param.gcode"
        assert matches_action("calibration", path, "auto_cali_for_user_param.gcode")
        assert matches_action("calibration", None, "auto_cali_for_user_param.gcode")
        assert is_internal_printer_job(None, "auto_cali_for_user_param.gcode")
        assert action_for_job(path, "auto_cali_for_user_param.gcode") == "calibration"

    def test_the_older_name(self):
        assert matches_action("calibration", None, "auto_cali_for_user")

    def test_the_pressure_advance_line_is_internal_but_belongs_to_no_action(self):
        assert is_internal_printer_job("", "auto_pa_line_calib_mode")
        assert not matches_action("calibration", "", "auto_pa_line_calib_mode")
        assert action_for_job("", "auto_pa_line_calib_mode") is None

    def test_a_users_print_is_neither(self):
        assert not matches_action("calibration", "Benchy.gcode.3mf", "Benchy")
        assert action_for_job("Benchy.gcode.3mf", "Benchy") is None

    def test_the_vision_encoder_run_as_captured(self):
        """calibrate_motion_precision (H2S capture) is the motion_precision
        action's job and nobody else's: a run waiting for bed levelling must
        not be closed by it, and vice versa."""
        motion = "/usr/etc/print/O1S/calibrate_motion_precision.gcode"
        assert is_internal_printer_job(motion, "calibrate_motion_precision.gcode")
        assert is_internal_printer_job(None, "calibrate_motion_precision.gcode")
        assert matches_action("motion_precision", motion, "calibrate_motion_precision.gcode")
        assert matches_action("motion_precision", None, "calibrate_motion_precision")
        assert not matches_action("calibration", motion, "calibrate_motion_precision.gcode")
        assert not matches_action("motion_precision", None, "auto_cali_for_user_param.gcode")
        assert action_for_job(motion, "calibrate_motion_precision.gcode") == "motion_precision"

    def test_unknown_or_missing_action_matches_nothing(self):
        assert job_names_for_action(None) == frozenset()
        assert not matches_action(None, "/usr/etc/print/O1S/calibrate_motion_precision.gcode", None)
        assert not matches_action("pressure_advance", "", "auto_pa_line_calib_mode")
