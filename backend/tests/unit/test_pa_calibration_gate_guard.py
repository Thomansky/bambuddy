"""The content guard: a sliced file that cannot calibrate must never be printed.

This is the single most important check in the feature. A file without the
``M1002 judge_flag extrude_cali_flag`` gate is not a broken print -- it is a
perfectly normal six-minute print that heats the bed, lays a 30 mm line and
produces no K value, which is indistinguishable downstream from a measurement
that failed.
"""

import io
import zipfile

import pytest

from backend.app.services.slice_output_check import extrude_cali_gate_missing

# The real shape, trimmed: Bambu Studio's auto_pa_line_calib_mode job for an
# H2S, Metadata/plate_1.gcode lines 782-820.
CALIBRATING_GCODE = """
; HEADER_BLOCK_START
; BambuStudio 02.08.02.61
M1002 gcode_claim_action : 4
M620 S0A
;===== auto extrude cali start =========================
M975 S1
M1002 judge_flag extrude_cali_flag

M622 J0
    M983.3 F10.4167 A0.4 ; cali dynamic extrusion compensation
M623

M622 J1
    M1002 set_filament_type:PLA
    M1002 gcode_claim_action : 8
    M109 S220
    M983.3 F10.4167 A0.4 ; cali dynamic extrusion compensation
M623
;===== auto extrude cali end =========================
G1 X100 Y130 F30000
"""

# The same file with the calibration block gone: a printer preset whose start
# G-code has no gate, or a slice for a model that does not have one.
PLAIN_GCODE = CALIBRATING_GCODE.replace("M1002 judge_flag extrude_cali_flag", "").replace(
    "M983.3 F10.4167 A0.4 ; cali dynamic extrusion compensation", ""
)


def make_3mf(gcode: str, *, entry: str = "Metadata/plate_1.gcode") -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("[Content_Types].xml", "<Types/>")
        archive.writestr("Metadata/project_settings.config", "{}")
        archive.writestr(entry, gcode)
    return buffer.getvalue()


class TestExtrudeCaliGateMissing:
    def test_a_calibrating_file_passes(self):
        assert extrude_cali_gate_missing(make_3mf(CALIBRATING_GCODE), export_3mf=True) is False

    def test_the_gate_without_the_command_fails(self):
        gcode = CALIBRATING_GCODE.replace("M983.3 F10.4167 A0.4 ; cali dynamic extrusion compensation", "M400")
        assert extrude_cali_gate_missing(make_3mf(gcode), export_3mf=True) is True

    def test_the_command_without_the_gate_fails(self):
        gcode = CALIBRATING_GCODE.replace("M1002 judge_flag extrude_cali_flag", "M1002 judge_flag other_flag")
        assert extrude_cali_gate_missing(make_3mf(gcode), export_3mf=True) is True

    def test_neither_fails(self):
        assert extrude_cali_gate_missing(make_3mf(PLAIN_GCODE), export_3mf=True) is True

    def test_markers_only_inside_config_comments_fail(self):
        """The whole preset is embedded in the plate G-code as ``; key = value``
        comments, and ``machine_start_gcode`` is one of them -- so the template
        of the calibration block appears in the file even when the slicer did
        not expand it. Matching the comments would make this guard pass on
        exactly the file it exists to reject.
        """
        commented = (
            "; machine_start_gcode = M1002 judge_flag extrude_cali_flag\\nM622 J0\\n"
            "M983.3 F10.4167 A0.4\\nM623\n"
            "; layer_height = 0.25\n"
            "G1 X100 Y130 F30000\n"
        )
        assert extrude_cali_gate_missing(make_3mf(commented), export_3mf=True) is True

    def test_raw_gcode_body_is_handled(self):
        assert extrude_cali_gate_missing(CALIBRATING_GCODE.encode(), export_3mf=False) is False
        assert extrude_cali_gate_missing(PLAIN_GCODE.encode(), export_3mf=False) is True

    @pytest.mark.parametrize("payload", [b"", b"not a zip at all", b"PK\x03\x04truncated"])
    def test_unreadable_input_is_rejected_not_waved_through(self, payload):
        """The opposite default from ``start_gcode_is_missing``, deliberately.

        That check guards an ordinary print, where a file it cannot judge must
        still be allowed through. This one guards a job whose entire purpose is
        to run a measurement, so nothing may be uploaded on a maybe.
        """
        assert extrude_cali_gate_missing(payload, export_3mf=True) is True

    def test_a_3mf_without_any_plate_gcode_is_rejected(self):
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as archive:
            archive.writestr("Metadata/project_settings.config", "{}")
        assert extrude_cali_gate_missing(buffer.getvalue(), export_3mf=True) is True

    def test_every_plate_has_to_calibrate(self):
        """A multi-plate export where only one plate carries the gate is not a
        file we can dispatch plate 1 of and be sure about."""
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as archive:
            archive.writestr("Metadata/plate_1.gcode", CALIBRATING_GCODE)
            archive.writestr("Metadata/plate_2.gcode", PLAIN_GCODE)
        assert extrude_cali_gate_missing(buffer.getvalue(), export_3mf=True) is True
