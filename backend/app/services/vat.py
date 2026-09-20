"""VAT working basis for cost calculations.

When ``vat_enabled`` is on, ``price_vat_basis`` is the single basis every
calculated and displayed amount is in. A spool whose ``cost_vat_included``
differs from that basis is converted with ``vat_rate_percent`` before its
price becomes a print cost; the stored ``cost_per_kg`` is never rewritten.
Every other monetary input (default filament cost, energy price, catalogue
and BOM prices, Spoolman spool prices) is assumed to already be in the
working basis, so only spool prices pass through here.

With the switch off (the default) every function is the identity, which
keeps a private install byte-identical to before the feature.
"""

import logging
from dataclasses import dataclass

logger = logging.getLogger(__name__)

# Decimal places kept when the rate is applied. The final cost keeps its
# existing 2-decimal rounding at the call site.
_RATE_PRECISION = 4


@dataclass(frozen=True)
class VatContext:
    enabled: bool = False
    rate_percent: float = 0.0
    basis: str = "gross"

    @property
    def working_gross(self) -> bool:
        return self.basis != "net"

    @classmethod
    async def load(cls, db) -> "VatContext":
        """Read the three VAT settings once for a calculation.

        Fails closed to *disabled*: a setting that cannot be read is not
        evidence that the user turned the distinction on, and a cost that is
        recorded unconverted is what the app did before the feature existed.
        """
        try:
            from backend.app.api.routes.settings import get_setting

            enabled_raw = await get_setting(db, "vat_enabled")
            if not enabled_raw or str(enabled_raw).strip().lower() not in ("true", "1", "yes", "on"):
                return cls()
            rate_raw = await get_setting(db, "vat_rate_percent")
            try:
                rate = float(rate_raw) if rate_raw is not None else 19.0
            except (TypeError, ValueError):
                rate = 19.0
            basis_raw = await get_setting(db, "price_vat_basis")
            basis = "net" if str(basis_raw or "").strip().lower() == "net" else "gross"
            return cls(enabled=True, rate_percent=rate, basis=basis)
        except Exception as exc:  # noqa: BLE001 — a settings probe must not raise into a cost writer
            logger.debug("Could not read the VAT settings, pricing without conversion: %s", exc)
            return cls()


DISABLED = VatContext()


def convert(amount: float, from_gross: bool, to_gross: bool, rate_percent: float) -> float:
    """Move ``amount`` between gross and net at ``rate_percent``.

    Same basis, a zero amount or a zero rate all return the amount as-is.
    """
    if from_gross == to_gross or not amount or not rate_percent:
        return amount
    factor = 1.0 + rate_percent / 100.0
    converted = amount / factor if from_gross else amount * factor
    return round(converted, _RATE_PRECISION)


def normalise_cost_per_kg(cost_per_kg: float | None, spool_vat_included: bool | None, ctx: VatContext) -> float | None:
    """Express a spool's ``cost_per_kg`` in the working basis.

    ``None`` passes through so callers keep their "no price → default rate"
    branch. A missing per-spool flag counts as gross, the schema default.
    """
    if cost_per_kg is None or not ctx.enabled:
        return cost_per_kg
    spool_gross = True if spool_vat_included is None else bool(spool_vat_included)
    return convert(cost_per_kg, spool_gross, ctx.working_gross, ctx.rate_percent)


def spool_cost_per_kg(spool, ctx: VatContext, default: float) -> float:
    """The rate a print draws on ``spool`` is priced at, or ``default`` when it has none."""
    normalised = normalise_cost_per_kg(spool.cost_per_kg, getattr(spool, "cost_vat_included", True), ctx)
    return default if normalised is None else normalised
