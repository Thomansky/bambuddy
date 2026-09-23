"""Turning a measured pressure-advance result into the value the printer stores.

Two conversions sit between ``extrusion_cali_get_result`` and
``extrusion_cali_set``, and Bambu Studio performs both. Neither is obvious from
the payloads alone, so both were read off a capture of Studio driving an H2S
(``mqtt-tap/tap-0938BJ611001133-20260920-103100.jsonl``, two complete runs) and
both are pinned by tests against exactly those numbers.
"""

from __future__ import annotations

import re
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

# ``HS01-0.4`` — two letters for the flow type, two digits for the hardware
# position, then the diameter. Anything else is not a nozzle id we know how to
# rewrite, and a write is refused rather than guessed at.
_NOZZLE_ID_RE = re.compile(r"^([A-Z]{2})(\d{2})-([\d.]+)$")


def round_k_value(k_value: str | float) -> str:
    """The measured K as the printer wants it stored.

    Studio rounds to three decimals and renders six: ``0.018612 -> "0.019000"``
    and ``0.021994 -> "0.022000"``, both confirmed twice.

    ``ROUND_HALF_UP`` rather than Python's ``round``: the built-in rounds half
    to even, so ``round(0.0185, 3)`` is ``0.018``. That is a different K value
    from the one Studio would have written, permanently, on the user's printer.

    Raises ``ValueError`` when the printer's value cannot be read as a number —
    never invent a K.
    """
    try:
        quantised = Decimal(str(k_value).strip()).quantize(Decimal("0.001"), rounding=ROUND_HALF_UP)
    except (InvalidOperation, ArithmeticError, ValueError) as exc:
        raise ValueError(f"Not a usable K value: {k_value!r}") from exc
    return f"{quantised:.6f}"


def write_nozzle_id(result_nozzle_id: str) -> str:
    """The ``nozzle_id`` for the write, from the one the result reported.

    The printer reports the *fitted* nozzle (``HS01-0.4``) but files its
    calibration table under the position-normalised form (``HS00-0.4``), and
    Studio translates between them: both captured runs read ``HS01-0.4`` and
    wrote ``HS00-0.4``. Only the two position digits change; the flow letters
    and the diameter are carried through, because the same printer's table held
    both ``HH00-0.4`` and ``HS00-0.4`` entries for one filament — so the letters
    are part of the profile's identity, not a constant.

    Raises ``ValueError`` on anything that does not match the shape. An X1C
    reports an empty ``nozzle_id`` on every entry; writing a K under a guessed
    one would file it against a nozzle the user never used.
    """
    match = _NOZZLE_ID_RE.match((result_nozzle_id or "").strip().upper())
    if not match:
        raise ValueError(f"Unrecognised nozzle id: {result_nozzle_id!r}")
    letters, _position, diameter = match.groups()
    return f"{letters}00-{diameter}"


def build_extrusion_cali_set_filament(
    *,
    ams_id: int,
    slot_id: int,
    extruder_id: int,
    filament_id: str,
    k_value: str,
    n_coef: str,
    name: str,
    nozzle_diameter: str,
    nozzle_id: str,
) -> dict:
    """One ``filaments[]`` entry for ``extrusion_cali_set``, shaped like Studio's.

    Four details are load-bearing and each is confirmed in both captured runs:

    * **no ``cali_idx``** — the printer matches on
      ``(filament_id, nozzle_id, extruder_id)`` and replaces in place. The
      follow-up ``extrusion_cali_get`` shows ``cali_idx: 1`` changing while
      ``cali_idx: 0`` (the same filament on ``HH00-0.4``) stays untouched;
    * **``n_coef`` verbatim** from the result (``"0.750000"``), not the
      ``"0.000000"`` ``set_kprofile`` defaults to nor the ``"1.400000"`` the
      manual ``extrusion_cali_set`` helper hardcodes;
    * **``tray_id: 0``** regardless of which slot was calibrated — single-nozzle
      firmware answers ``result: "fail", reason: "invalid tray_id"`` to -1
      (#2718) and Studio sends 0 here even though the result reported -1;
    * **``setting_id: ""``**, ``nozzle_pos: 0``, ``nozzle_sn: "N/A"``.
    """
    return {
        "ams_id": ams_id,
        "extruder_id": extruder_id,
        "filament_id": filament_id,
        "k_value": k_value,
        "n_coef": n_coef,
        "name": name,
        "nozzle_diameter": nozzle_diameter,
        "nozzle_id": nozzle_id,
        "nozzle_pos": 0,
        "nozzle_sn": "N/A",
        "setting_id": "",
        "slot_id": slot_id,
        "tray_id": 0,
    }
