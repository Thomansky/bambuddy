"""Which AMS slots hold a spool the AMS has not read yet.

A spool inserted while the printer is printing is noticed -- its
``tray_exist_bits`` bit flips -- but the AMS cannot move filament to read its
tag during a print and does not come back to it afterwards, so its
``tray_read_done_bits`` bit stays clear. The queue's pre-dispatch re-read is
built on telling those slots apart from empty ones and from spools that were
read normally, on every mask layout the firmware uses.

The mask shapes come from an H2S capture (tap-0938BJ611001133-20260920): the
bitmasks live on ``print.ams`` itself, one global mask across all units, with
the same bit layout ``apply_tray_exist_bits`` decodes.
"""

from types import SimpleNamespace

from backend.app.services.ams_slot_presence import (
    UNREAD_NO_IDENTITY,
    UNREAD_NOT_DONE,
    detection_signals,
    slot_identity,
    slot_read_done,
    unidentified_slots,
    unread_ams_slot_reasons,
    unread_ams_slots,
)
from backend.app.services.bambu_mqtt import apply_tray_exist_bits, tray_bit_position
from backend.app.services.spool_tag_matcher import ZERO_TAG_UID


def _tray(tray_id, *, read=True, state=11):
    if read:
        return {
            "id": str(tray_id),
            "tray_type": "PLA",
            "tray_info_idx": "GFA01",
            "tag_uid": "3CA4E7DF00000100",
            "state": state,
        }
    return {"id": str(tray_id), "tray_type": "", "tray_info_idx": "", "tag_uid": ZERO_TAG_UID, "state": state}


def _state(units, *, exist=None, read_done=None):
    return SimpleNamespace(
        raw_data={"ams": units},
        tray_exist_bits=exist,
        tray_read_done_bits=read_done,
        tray_now=255,
    )


class TestTheCapturedShapes:
    def test_a_fully_read_ams_has_nothing_to_read(self):
        """The capture's steady state: exist "f", read_done "f"."""
        units = [{"id": "0", "tray": [_tray(i) for i in range(4)]}]
        assert unread_ams_slots(_state(units, exist="f", read_done="f")) == []

    def test_one_present_slot_the_ams_never_read(self):
        """exist "f", read_done "7": slot 3 is occupied and unread."""
        units = [{"id": "0", "tray": [_tray(0), _tray(1), _tray(2), _tray(3, read=False)]}]
        assert unread_ams_slots(_state(units, exist="f", read_done="7")) == [(0, 3)]

    def test_an_empty_slot_is_not_unread(self):
        """exist "7", read_done "7": slot 3 is simply empty."""
        units = [{"id": "0", "tray": [_tray(0), _tray(1), _tray(2), _tray(3, read=False, state=9)]}]
        assert unread_ams_slots(_state(units, exist="7", read_done="7")) == []

    def test_the_a2l_lite_bit_base_quirk(self):
        """The Lite reports unit 16 but its bits sit at base 24 (id 6). The
        merge normalises the unit to 6; the decoder has to read bit 24+slot,
        not 16*4+slot, or every Lite slot reads as empty."""
        units = [{"id": 6, "tray": [_tray(0), _tray(1, read=False), _tray(2, read=False), _tray(3, read=False)]}]
        exist = format(0b0111 << 24, "x")
        read_done = format(0b0001 << 24, "x")
        assert unread_ams_slots(_state(units, exist=exist, read_done=read_done)) == [(6, 1), (6, 2)]
        assert tray_bit_position(16, 1) == tray_bit_position(6, 1) == 25

    def test_a_unit_without_read_done_falls_back_to_the_tray_fields(self):
        """No ``tray_read_done_bits`` at all: an occupied slot with a zero tag,
        no preset and no type is the only unread signal left."""
        units = [{"id": "0", "tray": [_tray(0), _tray(1, read=False), _tray(2), _tray(3, read=False, state=9)]}]
        assert unread_ams_slots(_state(units, exist="7", read_done=None)) == [(0, 1)]

    def test_no_masks_at_all_uses_the_firmware_empty_states(self):
        units = [{"id": "0", "tray": [_tray(0), _tray(1, read=False), _tray(2, read=False, state=10)]}]
        assert unread_ams_slots(_state(units)) == [(0, 1)]


class TestTheBitSaysDoneButNobodyKnowsWhatIsInThere:
    """Firmware sets the read-done bit for an attempt that *finished*, which
    includes one that finished having found nothing. A slot left like that --
    occupied, no tag, no preset, no type -- is precisely the one this feature
    exists for, and trusting the bit alone makes it invisible."""

    def test_done_but_empty_identity_is_unread(self):
        units = [{"id": "0", "tray": [_tray(0), _tray(1), _tray(2), _tray(3, read=False)]}]
        assert unread_ams_slots(_state(units, exist="f", read_done="f")) == [(0, 3)]

    def test_done_and_identified_is_not_unread(self):
        units = [{"id": "0", "tray": [_tray(i) for i in range(4)]}]
        assert unread_ams_slots(_state(units, exist="f", read_done="f")) == []

    def test_a_tag_alone_is_an_identity(self):
        """Any one of the three fields filled in means the AMS got something."""
        units = [{"id": "0", "tray": [{"id": "0", "tray_type": "", "tray_info_idx": "", "tag_uid": "3CA4E7DF"}]}]
        assert unread_ams_slots(_state(units, exist="1", read_done="1")) == []

    def test_an_empty_slot_is_never_unread_whatever_the_bits_say(self):
        units = [{"id": "0", "tray": [_tray(0, read=False, state=9)]}]
        assert unread_ams_slots(_state(units, exist="0", read_done="1")) == []


class TestWhichRuleSaidSo:
    """The caller has to tell the two rules apart. Firmware's own cleared bit
    is a fresh "not read yet" and is worth a command every time it is seen;
    "no identity" is an inference that stays true of a spool nothing can read
    for as long as it sits in the slot, so acting on it has to be bounded."""

    def test_a_cleared_bit_is_reported_as_the_bit(self):
        units = [{"id": "0", "tray": [_tray(0), _tray(1, read=False)]}]
        assert unread_ams_slot_reasons(_state(units, exist="3", read_done="1")) == {(0, 1): UNREAD_NOT_DONE}

    def test_a_nameless_slot_the_firmware_calls_done_is_reported_as_the_inference(self):
        units = [{"id": "0", "tray": [_tray(0), _tray(1, read=False)]}]
        assert unread_ams_slot_reasons(_state(units, exist="3", read_done="3")) == {(0, 1): UNREAD_NO_IDENTITY}

    def test_the_bit_wins_when_both_would_fit(self):
        """A spool put in mid-print is nameless *and* has its bit clear. It is
        firmware's word, not ours: a caller that skips slots it once found
        unreadable must not skip this one."""
        units = [{"id": "0", "tray": [_tray(0, read=False)]}]
        assert unread_ams_slot_reasons(_state(units, exist="1", read_done="0")) == {(0, 0): UNREAD_NOT_DONE}

    def test_without_a_read_done_mask_only_the_inference_is_left(self):
        units = [{"id": "0", "tray": [_tray(0, read=False)]}]
        assert unread_ams_slot_reasons(_state(units, exist="1")) == {(0, 0): UNREAD_NO_IDENTITY}

    def test_the_slots_are_the_reasons_keys(self):
        units = [{"id": "0", "tray": [_tray(0, read=False), _tray(1), _tray(2, read=False)]}]
        state = _state(units, exist="7", read_done="3")
        assert unread_ams_slots(state) == list(unread_ams_slot_reasons(state)) == [(0, 0), (0, 2)]


class TestWhichSignalsWereAvailable:
    """Printed next to the raw masks, so a report of a wrong verdict says
    whether there was a mask to read at all."""

    def test_both_masks(self):
        assert detection_signals(_state([], exist="f", read_done="f")) == "masks"

    def test_read_done_only(self):
        assert detection_signals(_state([], read_done="f")) == "read-done mask + tray fields"

    def test_exist_only(self):
        assert detection_signals(_state([], exist="f")) == "exist mask + tray fields"

    def test_neither(self):
        assert detection_signals(_state([])) == "tray fields"


class TestUnidentifiedSlots:
    """What the scheduler's per-slot memory is pruned against: still occupied,
    still nameless. Anything else means the spool changed or the AMS finally
    read it, and the slot has earned another attempt."""

    def test_names_the_occupied_nameless_slots(self):
        units = [{"id": "0", "tray": [_tray(0), _tray(1, read=False), _tray(2, read=False, state=9)]}]
        assert unidentified_slots(_state(units, exist="3", read_done="f")) == {(0, 1)}

    def test_an_identified_slot_is_not_in_it(self):
        units = [{"id": "0", "tray": [_tray(0)]}]
        assert unidentified_slots(_state(units, exist="1", read_done="0")) == set()

    def test_a_slot_whose_spool_was_pulled_is_not_in_it(self):
        units = [{"id": "0", "tray": [_tray(0, read=False)]}]
        assert unidentified_slots(_state(units, exist="0", read_done="0")) == set()

    def test_it_works_without_any_mask(self):
        units = [{"id": "0", "tray": [_tray(0, read=False), _tray(1, read=False, state=9)]}]
        assert unidentified_slots(_state(units)) == {(0, 0)}


class TestWhatIsNeverIncluded:
    def test_the_external_spool_unit(self):
        units = [{"id": "254", "tray": [_tray(0, read=False)]}]
        assert unread_ams_slots(_state(units, exist="f", read_done="0")) == []

    def test_virtual_trays_are_not_ams_units(self):
        state = SimpleNamespace(
            raw_data={"ams": [], "vt_tray": [_tray(254, read=False)]},
            tray_exist_bits="f",
            tray_read_done_bits="0",
        )
        assert unread_ams_slots(state) == []

    def test_an_ams_ht_reads_its_single_bit(self):
        """HT units pack ONE bit at 16 + (id - 128), the same layout the
        presence decoder uses."""
        units = [{"id": 128, "tray": [_tray(0, read=False)]}]
        assert unread_ams_slots(_state(units, exist=format(1 << 16, "x"), read_done="0")) == [(128, 0)]
        identified = [{"id": 128, "tray": [_tray(0)]}]
        assert unread_ams_slots(_state(identified, exist=format(1 << 16, "x"), read_done=format(1 << 16, "x"))) == []

    def test_a_unit_with_no_known_bit_layout_is_skipped(self):
        units = [{"id": 40, "tray": [_tray(0, read=False)]}]
        assert unread_ams_slots(_state(units, exist="f", read_done="0")) == []

    def test_garbage_state_is_nothing_to_read(self):
        assert unread_ams_slots(SimpleNamespace(raw_data={})) == []
        assert unread_ams_slots(SimpleNamespace(raw_data={"ams": "nope"})) == []
        assert unread_ams_slots(_state([{"id": "x", "tray": [_tray(0)]}], exist="zz", read_done="f")) == []


class TestOneDecoderForBothViews:
    def test_presence_and_read_done_agree_on_the_bit(self):
        """``apply_tray_exist_bits`` and ``unread_ams_slots`` read through the
        same position function, so a slot the presence pass calls occupied is
        the slot the read-done pass looks at."""
        units = [{"id": 1, "tray": [_tray(0, read=False), _tray(1, read=False)]}]
        exist = format(0b10 << 4, "x")
        apply_tray_exist_bits(units, exist, annotate_exists=True)
        assert [t["exists"] for t in units[0]["tray"]] == [False, True]
        assert unread_ams_slots(_state(units, exist=exist, read_done="0")) == [(1, 1)]


class TestSlotReadDone:
    def test_reads_the_bit(self):
        state = _state([], read_done="7")
        assert slot_read_done(state, 0, 2) is True
        assert slot_read_done(state, 0, 3) is False

    def test_none_without_a_mask(self):
        assert slot_read_done(_state([]), 0, 0) is None
        assert slot_read_done(_state([], read_done="f"), 40, 0) is None


class TestSlotIdentity:
    def test_names_the_tray_fields(self):
        units = [{"id": "0", "tray": [_tray(0)]}]
        assert slot_identity(_state(units), 0, 0) == ("PLA", "3CA4E7DF00000100", "GFA01")

    def test_none_for_a_slot_not_reported(self):
        assert slot_identity(_state([{"id": "0", "tray": [_tray(0)]}]), 1, 0) is None


class TestTheMasksReachPrinterState:
    """``_handle_ams_data`` keeps the three masks the detection reads, as the
    hex strings the wire carries, and leaves them alone on a frame without."""

    @staticmethod
    def _client():
        from backend.app.services.bambu_mqtt import BambuMQTTClient

        return BambuMQTTClient(ip_address="10.0.0.1", serial_number="SERIAL", access_code="code", model="H2S")

    def test_stored_from_the_capture_frame(self):
        client = self._client()
        client._handle_ams_data(
            {
                "ams": [{"id": "0", "tray": [_tray(i) for i in range(4)]}],
                "tray_exist_bits": "f",
                "tray_read_done_bits": "7",
                "tray_reading_bits": "8",
                "tray_now": "255",
            }
        )
        assert client.state.tray_exist_bits == "f"
        assert client.state.tray_read_done_bits == "7"
        assert client.state.tray_reading_bits == "8"
        assert unread_ams_slots(client.state) == [(0, 3)]

    def test_a_partial_frame_keeps_the_last_masks(self):
        client = self._client()
        client._handle_ams_data({"tray_exist_bits": "f", "tray_read_done_bits": "f"})
        client._handle_ams_data({"tray_now": "255"})
        assert client.state.tray_read_done_bits == "f"

    def test_an_integer_is_kept_as_hex(self):
        client = self._client()
        client._handle_ams_data({"tray_read_done_bits": 15})
        assert client.state.tray_read_done_bits == "f"
