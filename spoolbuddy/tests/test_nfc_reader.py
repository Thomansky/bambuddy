"""Tests for daemon.nfc_reader — tray UUID extraction from Bambu tag blocks."""

from daemon.nfc_reader import _extract_tray_uuid

# Measured on a real spool: block 9 equals the tray_uuid the AMS reports (#984).
BLOCK9 = bytes.fromhex("9E0B0717BEE94D7887EB1D8DFD1A14F3")
# Block 4 of a "PLA Matte" spool -- what the old blocks 4+5 read sent as tray_uuid.
BLOCK4_PLA_MATTE = b"PLA Matte" + b"\x00" * 7


class TestExtractTrayUuid:
    def test_reads_block_9(self):
        blocks = {1: b"\x01" * 16, 2: b"\x02" * 16, 4: BLOCK4_PLA_MATTE, 5: b"\x00" * 16, 9: BLOCK9}
        assert _extract_tray_uuid(blocks) == "9E0B0717BEE94D7887EB1D8DFD1A14F3"

    def test_ignores_blocks_4_and_5(self):
        """Without block 9 there is no tray UUID, never the filament type from block 4."""
        blocks = {4: BLOCK4_PLA_MATTE, 5: b"\x00" * 16}
        assert _extract_tray_uuid(blocks) is None

    def test_all_zero_block_9_is_no_uuid(self):
        assert _extract_tray_uuid({9: b"\x00" * 16}) is None

    def test_short_block_9_is_no_uuid(self):
        assert _extract_tray_uuid({9: b"\x9e\x0b"}) is None

    def test_empty_blocks(self):
        assert _extract_tray_uuid({}) is None
