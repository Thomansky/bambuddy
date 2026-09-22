"""Is there a spool in this AMS slot?

Three backend decisions turn on the answer -- whether to push
``ams_filament_setting`` when a spool is assigned, whether a pre-assigned slot
has just been filled, and whether an assignment has gone stale -- and all three
used to read it off the tray's ``state`` field. That field cannot carry it.

## Why ``state`` is the wrong source

``state`` is firmware-variant. The A1 Mini BMCU and the P1S Standard AMS report
3 for a loaded slot and never emit 11; the AMS-HT's codes differ again (#2670).
Bambuddy already works around that on the printer card, where
``getEmptySlotKind`` (``PrintersPage.tsx``) reads the presence bit first and
only falls back to the 9/10 heuristic when there is no bit to read.

Worse, ``state`` is partly Bambuddy's own writing. ``apply_tray_exist_bits``
sets ``state = 9`` on every slot whose presence bit is 0 -- and when the bit
comes back it leaves the 9 exactly where it was, because the "slot occupied"
branch only annotates ``exists`` and moves on. So a slot the firmware says is
full can sit in the cache reading ``exists=True, state=9`` indefinitely. That
is #3084: a non-Bambu spool is swapped in, Assign Spool reads the stale 9,
calls the slot empty, sends no MQTT, and the printer keeps showing ``?``. The
deferred-configuration replay could not rescue it either, because its own
"loaded" test was the same 9/10 heuristic -- the exact deadlock #1322 removed
elsewhere. #3100 is the same stale 9 one step further on: the replay does not
fire, the assignment keeps the empty fingerprint it was stored with, and the
first real tray report is read as a spool swap and deleted.

## What this module answers

``tray_exist_bits`` is the firmware's own "which slots have a spool" bitmask --
the one BambuStudio draws its ``?`` from -- and ``apply_tray_exist_bits``
records it per tray as ``exists``. That bit is authoritative where it exists,
and absent otherwise; it is never a guess. Callers that want a decision for a
payload carrying no bit at all keep their own fallback, because the right
fallback differs per caller: the assign path wants to know whether the push is
doomed, the unlink pass wants to know whether a spool was removed, and a state
of 26 ("unloaded", mid-runout) answers those two questions differently.
"""

from collections.abc import Iterator, Mapping
from typing import Any


def spool_present(tray: Mapping[str, Any] | None) -> bool | None:
    """Does firmware's presence bit say a spool is in this slot?

    ``True`` / ``False`` straight from ``tray_exist_bits``; ``None`` when the
    tray carries no presence annotation, which means the caller has to decide
    on its own terms rather than assume either way.

    Only the internal AMS path annotates ``exists`` (``apply_tray_exist_bits``
    is called with ``annotate_exists=True`` there and False for the VP bridge),
    so the external spool's ``vt_tray`` entries answer ``None`` -- they have no
    bit in the mask.
    """
    if not isinstance(tray, Mapping):
        return None
    exists = tray.get("exists")
    return exists if isinstance(exists, bool) else None


# External spool "unit" as some firmware lists it inside ams.ams; it has no
# tag to read and no bit in any mask.
_EXTERNAL_AMS_ID = 254

# Firmware's own "no spool" tray states, the only presence signal a payload
# without tray_exist_bits leaves us.
_EMPTY_TRAY_STATES = (9, 10)


def _int_id(unit: Mapping[str, Any]) -> int | None:
    raw = unit.get("id")
    try:
        return int(raw) if raw is not None else None
    except (TypeError, ValueError):
        return None


def _looks_unread(tray: Mapping[str, Any]) -> bool:
    """The tray fields of a slot the AMS has not identified: nothing filled in.

    No tag, no preset, no type. Both the fallback for a payload without
    ``tray_read_done_bits`` and, on firmware that does send the mask, the
    check that catches a read-done bit set on a slot the AMS never actually
    identified -- an attempt that finished having found nothing reads as
    "done" too. A non-Bambu spool the AMS has already looked at reads the
    same way, which is why the scheduler only ever acts on this once per
    spool.
    """
    from backend.app.services.spool_tag_matcher import ZERO_TAG_UID

    tag_uid = tray.get("tag_uid") or ZERO_TAG_UID
    return tag_uid == ZERO_TAG_UID and not tray.get("tray_info_idx") and not tray.get("tray_type")


def _ams_slots(state: Any) -> Iterator[tuple[int, int, Mapping[str, Any], int]]:
    """Every real AMS slot in the report, as ``(ams_id, tray_id, tray, bit)``.

    The external spool (254) and the virtual trays never come out of here:
    there is no tag to read and no bit in any mask for them. Neither does a
    unit whose id has no known bit layout.
    """
    from backend.app.services.bambu_mqtt import tray_bit_position

    raw = getattr(state, "raw_data", None) or {}
    units = raw.get("ams") if isinstance(raw, Mapping) else None
    if not isinstance(units, list):
        return
    for unit in units:
        if not isinstance(unit, Mapping):
            continue
        ams_id = _int_id(unit)
        if ams_id is None or ams_id == _EXTERNAL_AMS_ID:
            continue
        for tray in unit.get("tray") or []:
            if not isinstance(tray, Mapping):
                continue
            tray_id = _int_id(tray)
            if tray_id is None:
                continue
            bit = tray_bit_position(ams_id, tray_id)
            if bit is None:
                continue
            yield ams_id, tray_id, tray, bit


def _slot_present(tray: Mapping[str, Any], bit: int, exist_mask: int | None) -> bool:
    """Is there a spool in this slot, by the best signal the payload carries?"""
    if exist_mask is not None:
        return bool((exist_mask >> bit) & 1)
    present = spool_present(tray)
    if present is None:
        present = tray.get("state") not in _EMPTY_TRAY_STATES
    return bool(present)


def slot_read_done(state: Any, ams_id: int, tray_id: int) -> bool | None:
    """Has the AMS finished reading this slot, per ``tray_read_done_bits``?

    ``None`` when the printer has not reported the mask, in which case the
    caller has to watch the tray's own fields instead.
    """
    from backend.app.services.bambu_mqtt import parse_tray_bits, tray_bit_position

    mask = parse_tray_bits(getattr(state, "tray_read_done_bits", None))
    if mask is None:
        return None
    bit = tray_bit_position(ams_id, tray_id)
    if bit is None:
        return None
    return bool((mask >> bit) & 1)


# Why a slot counts as unread, in the words the scheduler's log line uses.
#
# The two are not interchangeable to a caller. NOT_DONE is firmware's own "I
# have not read this one" and is worth a command every time it is seen -- it
# is the spool-inserted-during-a-print case the pre-read exists for. NO_IDENTITY
# is an inference drawn from empty tray fields, which stays true of a spool
# nothing can read for as long as it sits in the slot, so a caller acting on it
# has to bound itself to one attempt per spool.
UNREAD_NOT_DONE = "read-done bit clear"
UNREAD_NO_IDENTITY = "no identity"


def unread_ams_slot_reasons(state: Any) -> dict[tuple[int, int], str]:
    """Every unread slot with the rule that says so: ``{(ams_id, tray_id): reason}``.

    A slot qualifies when it holds a spool -- ``tray_exist_bits`` where the
    firmware sends it, the tray's own presence annotation or firmware's 9/10
    "no spool" states otherwise -- and either

    * its ``tray_read_done_bits`` bit is clear (``UNREAD_NOT_DONE``), the case
      of a spool inserted while the printer was busy: the AMS notices it (the
      exist bit flips) but cannot move filament to read its tag during a print,
      and does not come back to it afterwards; or
    * the AMS has put no identity on it at all (``UNREAD_NO_IDENTITY``,
      ``_looks_unread``), **whatever the read-done bit says**. Firmware sets
      that bit for an attempt that finished, including one that found nothing,
      so trusting it alone leaves exactly the slot this feature exists for
      looking read. It is also the only signal on firmware that sends no mask.

    The bit is asked first, so a slot firmware itself calls unread is never
    reported as the weaker inference: that distinction is what lets the
    scheduler spend one read per unreadable spool without ever going quiet on
    a slot firmware is still asking about.

    The queue asks for the read before a job is mapped, so the mapping sees
    what is actually loaded. The external spool (254) and the virtual trays
    never appear: there is nothing to read there.
    """
    from backend.app.services.bambu_mqtt import parse_tray_bits

    exist_mask = parse_tray_bits(getattr(state, "tray_exist_bits", None))
    done_mask = parse_tray_bits(getattr(state, "tray_read_done_bits", None))

    reasons: dict[tuple[int, int], str] = {}
    for ams_id, tray_id, tray, bit in _ams_slots(state):
        if not _slot_present(tray, bit, exist_mask):
            continue
        if done_mask is not None and not (done_mask >> bit) & 1:
            reasons[(ams_id, tray_id)] = UNREAD_NOT_DONE
        elif _looks_unread(tray):
            reasons[(ams_id, tray_id)] = UNREAD_NO_IDENTITY
    return reasons


def unread_ams_slots(state: Any) -> list[tuple[int, int]]:
    """Slots holding a spool the AMS has not read: ``[(ams_id, tray_id), ...]``.

    :func:`unread_ams_slot_reasons` without the reasons, for callers that only
    need the list.
    """
    return list(unread_ams_slot_reasons(state))


def detection_signals(state: Any) -> str:
    """Which signals :func:`unread_ams_slot_reasons` had to work with, in words.

    The scheduler prints this next to the raw masks, so a report of a wrong
    verdict says whether a mask was there to be read at all. It decodes the
    masks here rather than at the call site: one module owns that, and the
    log cannot drift from the detection it describes.
    """
    from backend.app.services.bambu_mqtt import parse_tray_bits

    exist = parse_tray_bits(getattr(state, "tray_exist_bits", None)) is not None
    done = parse_tray_bits(getattr(state, "tray_read_done_bits", None)) is not None
    if exist and done:
        return "masks"
    if done:
        return "read-done mask + tray fields"
    if exist:
        return "exist mask + tray fields"
    return "tray fields"


def unidentified_slots(state: Any) -> set[tuple[int, int]]:
    """Occupied slots the AMS has put no identity on: ``{(ams_id, tray_id)}``.

    The scheduler remembers the slots a pre-read left like this so it stops
    asking about a spool nothing can read, and needs to know when to forget
    again: an entry that is no longer in this set has either lost its spool
    or gained an identity, and the next spool gets its chance.
    """
    from backend.app.services.bambu_mqtt import parse_tray_bits

    exist_mask = parse_tray_bits(getattr(state, "tray_exist_bits", None))
    return {
        (ams_id, tray_id)
        for ams_id, tray_id, tray, bit in _ams_slots(state)
        if _slot_present(tray, bit, exist_mask) and _looks_unread(tray)
    }


def slot_identity(state: Any, ams_id: int, tray_id: int) -> tuple[Any, Any, Any] | None:
    """What the AMS currently says is in the slot: ``(tray_type, tag_uid, tray_info_idx)``.

    A read that completes on firmware without ``tray_read_done_bits`` shows up
    only as a change in these, so a caller waiting for such a read compares
    the tuple before and after. ``None`` when the slot is not in the report.
    """
    raw = getattr(state, "raw_data", None) or {}
    units = raw.get("ams") if isinstance(raw, Mapping) else None
    if not isinstance(units, list):
        return None
    for unit in units:
        if not isinstance(unit, Mapping) or _int_id(unit) != ams_id:
            continue
        for tray in unit.get("tray") or []:
            if isinstance(tray, Mapping) and _int_id(tray) == tray_id:
                return (tray.get("tray_type"), tray.get("tag_uid"), tray.get("tray_info_idx"))
    return None
