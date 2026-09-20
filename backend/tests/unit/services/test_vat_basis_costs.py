"""The VAT working basis applied to cost calculations.

Spool prices carry their own basis (``cost_vat_included``); every calculated
cost must come out in the configured working basis, and with the feature off
nothing may change.
"""

from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from backend.app.services import print_cost_estimate
from backend.app.services.usage_tracker import PrintSession, _active_sessions, on_print_complete
from backend.app.services.vat import DISABLED, VatContext, convert, normalise_cost_per_kg, spool_cost_per_kg

GROSS_CTX = VatContext(enabled=True, rate_percent=19.0, basis="gross")
NET_CTX = VatContext(enabled=True, rate_percent=19.0, basis="net")


class TestConvert:
    def test_net_to_gross(self):
        assert convert(100.0, from_gross=False, to_gross=True, rate_percent=19.0) == 119.0

    def test_gross_to_net(self):
        assert convert(119.0, from_gross=True, to_gross=False, rate_percent=19.0) == 100.0

    def test_same_basis_is_identity(self):
        assert convert(33.3333, from_gross=True, to_gross=True, rate_percent=19.0) == 33.3333

    def test_zero_amount_and_zero_rate(self):
        assert convert(0.0, from_gross=False, to_gross=True, rate_percent=19.0) == 0.0
        assert convert(25.0, from_gross=False, to_gross=True, rate_percent=0.0) == 25.0

    def test_rate_application_keeps_four_decimals(self):
        # 21 / 1.19 = 17.647058... -> the rate step keeps 4 decimals; the
        # 2-decimal rounding of a print cost happens later at the call site.
        assert convert(21.0, from_gross=True, to_gross=False, rate_percent=19.0) == 17.6471


class TestNormaliseCostPerKg:
    def test_disabled_returns_input_unchanged(self):
        assert normalise_cost_per_kg(21.0, False, DISABLED) == 21.0
        assert normalise_cost_per_kg(21.0, True, DISABLED) == 21.0

    def test_none_passes_through(self):
        assert normalise_cost_per_kg(None, False, GROSS_CTX) is None

    def test_net_spool_under_gross_basis_is_grossed_up(self):
        assert normalise_cost_per_kg(20.0, False, GROSS_CTX) == 23.8

    def test_gross_spool_under_net_basis_is_netted(self):
        assert normalise_cost_per_kg(23.8, True, NET_CTX) == 20.0

    def test_matching_basis_is_untouched(self):
        assert normalise_cost_per_kg(23.8, True, GROSS_CTX) == 23.8
        assert normalise_cost_per_kg(20.0, False, NET_CTX) == 20.0

    def test_missing_flag_counts_as_gross(self):
        assert normalise_cost_per_kg(23.8, None, NET_CTX) == 20.0

    def test_spool_helper_falls_back_to_default_only_without_a_price(self):
        assert spool_cost_per_kg(SimpleNamespace(cost_per_kg=None, cost_vat_included=False), GROSS_CTX, 25.0) == 25.0
        assert spool_cost_per_kg(SimpleNamespace(cost_per_kg=20.0, cost_vat_included=False), GROSS_CTX, 25.0) == 23.8


class TestVatContextLoad:
    @pytest.mark.asyncio
    async def test_reads_the_three_settings(self):
        values = {"vat_enabled": "true", "vat_rate_percent": "7.7", "price_vat_basis": "net"}
        with patch(
            "backend.app.api.routes.settings.get_setting",
            AsyncMock(side_effect=lambda _db, key: values.get(key)),
        ):
            ctx = await VatContext.load(MagicMock())
        assert ctx == VatContext(enabled=True, rate_percent=7.7, basis="net")
        assert ctx.working_gross is False

    @pytest.mark.asyncio
    async def test_off_or_missing_is_disabled(self):
        for raw in (None, "false", "15.0"):
            with patch("backend.app.api.routes.settings.get_setting", AsyncMock(return_value=raw)):
                assert (await VatContext.load(MagicMock())).enabled is False

    @pytest.mark.asyncio
    async def test_unreadable_settings_fail_closed(self):
        with patch("backend.app.api.routes.settings.get_setting", AsyncMock(side_effect=RuntimeError("no db"))):
            assert await VatContext.load(MagicMock()) == DISABLED


def _make_spool(spool_id, cost_per_kg, cost_vat_included):
    spool = MagicMock()
    spool.id = spool_id
    spool.label_weight = 1000
    spool.weight_used = 0
    spool.cost_per_kg = cost_per_kg
    spool.cost_vat_included = cost_vat_included
    spool.last_used = None
    spool.material = "PLA"
    return spool


def _make_assignment(spool_id, tray_id):
    assignment = MagicMock()
    assignment.spool_id = spool_id
    assignment.printer_id = 1
    assignment.ams_id = 0
    assignment.tray_id = tray_id
    return assignment


def _sequential_db(responses):
    db = AsyncMock()
    calls = [0]

    async def mock_execute(*_args, **_kwargs):
        idx = calls[0]
        calls[0] += 1
        result = MagicMock()
        value = responses[idx] if idx < len(responses) else None
        result.scalar_one_or_none.return_value = value
        result.scalar.return_value = value
        return result

    db.execute = mock_execute
    return db


def _settings(vat: dict | None):
    values = {"default_filament_cost": "15.0", **(vat or {})}
    return AsyncMock(side_effect=lambda _db, key: values.get(key))


async def _complete_two_spool_print(vat_settings: dict | None, tmp_path) -> tuple[list[dict], MagicMock]:
    """One print drawing 100 g from a gross spool (23.80/kg) and 100 g from a net spool (20.00/kg)."""
    _active_sessions.clear()
    gross_spool = _make_spool(1, 23.8, True)
    net_spool = _make_spool(2, 20.0, False)
    archive = MagicMock()
    archive.id = 10
    archive.file_path = str(tmp_path / "print.3mf")
    archive.filament_used_grams = 200
    archive.cost = None
    archive.print_name = "Test"
    archive.printer_id = 1

    _active_sessions[1] = PrintSession(
        printer_id=1,
        print_name="Test",
        started_at=datetime.now(timezone.utc),
        tray_remain_start={(0, 0): 80, (0, 1): 90},
        tray_now_at_start=0,
    )
    printer_manager = MagicMock()
    printer_manager.get_status.return_value = SimpleNamespace(
        raw_data={"ams": [{"id": 0, "tray": [{"id": 0, "remain": 70}, {"id": 1, "remain": 80}]}]},
        progress=100,
        layer_num=50,
        tray_now=0,
    )
    # archive, assignment1, spool1, assignment2, spool2, archive (cost write), run count
    db = _sequential_db([archive, _make_assignment(1, 0), gross_spool, _make_assignment(2, 1), net_spool, archive, 0])
    filament_usage = [
        {"slot_id": 1, "used_g": 100.0, "type": "PLA", "color": "#FF0000"},
        {"slot_id": 2, "used_g": 100.0, "type": "PLA", "color": "#00FF00"},
    ]
    with (
        patch("backend.app.core.config.settings") as mock_settings,
        patch("backend.app.api.routes.settings.get_setting", _settings(vat_settings)),
        patch("backend.app.utils.threemf_tools.extract_filament_usage_from_3mf", return_value=filament_usage),
    ):
        mock_settings.base_dir = MagicMock()
        mock_path = MagicMock()
        mock_path.exists.return_value = True
        mock_settings.base_dir.__truediv__ = MagicMock(return_value=mock_path)
        results = await on_print_complete(
            printer_id=1,
            data={"status": "completed"},
            printer_manager=printer_manager,
            db=db,
            archive_id=10,
            ams_mapping=[0, 1],
        )
    _active_sessions.clear()
    return results, archive


class TestUsageTrackerNormalisation:
    @pytest.mark.asyncio
    async def test_gross_working_basis_grosses_up_the_net_spool(self, tmp_path):
        results, archive = await _complete_two_spool_print(
            {"vat_enabled": "true", "vat_rate_percent": "19", "price_vat_basis": "gross"}, tmp_path
        )
        by_spool = {r["spool_id"]: r["cost"] for r in results}
        assert by_spool[1] == 2.38  # already gross
        assert by_spool[2] == 2.38  # 20.00 net -> 23.80 gross
        assert archive.cost == 4.76

    @pytest.mark.asyncio
    async def test_net_working_basis_nets_the_gross_spool(self, tmp_path):
        results, archive = await _complete_two_spool_print(
            {"vat_enabled": "true", "vat_rate_percent": "19", "price_vat_basis": "net"}, tmp_path
        )
        by_spool = {r["spool_id"]: r["cost"] for r in results}
        assert by_spool[1] == 2.0  # 23.80 gross -> 20.00 net
        assert by_spool[2] == 2.0
        assert archive.cost == 4.0

    @pytest.mark.asyncio
    async def test_disabled_leaves_every_cost_as_before(self, tmp_path):
        """Off (the default) records raw per-spool prices, mixed bases and all."""
        results, archive = await _complete_two_spool_print(None, tmp_path)
        by_spool = {r["spool_id"]: r["cost"] for r in results}
        assert by_spool[1] == 2.38
        assert by_spool[2] == 2.0
        assert archive.cost == 4.38


class TestQueueEstimateNormalisation:
    @pytest.fixture
    def library_file(self, tmp_path):
        path = tmp_path / "queued.gcode.3mf"
        path.write_bytes(b"stub")
        return SimpleNamespace(file_path=str(path), file_metadata={})

    async def _estimate(self, monkeypatch, library_file, vat_ctx):
        monkeypatch.setattr(
            print_cost_estimate.threemf_tools,
            "extract_plate_metadata_from_3mf",
            lambda *_args: SimpleNamespace(
                filament_usage=[{"slot_id": 1, "used_g": 100.0}, {"slot_id": 2, "used_g": 100.0}]
            ),
        )
        monkeypatch.setattr(print_cost_estimate, "_default_cost_per_kg", AsyncMock(return_value=25.0))
        monkeypatch.setattr(print_cost_estimate, "spoolman_owns_assignments", AsyncMock(return_value=False))
        monkeypatch.setattr(print_cost_estimate.VatContext, "load", AsyncMock(return_value=vat_ctx))
        assignments = [
            SimpleNamespace(ams_id=0, tray_id=0, spool=SimpleNamespace(cost_per_kg=23.8, cost_vat_included=True)),
            SimpleNamespace(ams_id=0, tray_id=1, spool=SimpleNamespace(cost_per_kg=20.0, cost_vat_included=False)),
        ]
        result = SimpleNamespace(scalars=lambda: SimpleNamespace(all=lambda: assignments))
        db = SimpleNamespace(execute=AsyncMock(return_value=result))
        return await print_cost_estimate.estimate_queue_source_cost(
            db, library_file=library_file, plate_id=1, ams_mapping=[0, 1], printer_id=7
        )

    @pytest.mark.asyncio
    async def test_gross_basis(self, monkeypatch, library_file):
        assert await self._estimate(monkeypatch, library_file, GROSS_CTX) == 4.76

    @pytest.mark.asyncio
    async def test_net_basis(self, monkeypatch, library_file):
        assert await self._estimate(monkeypatch, library_file, NET_CTX) == 4.0

    @pytest.mark.asyncio
    async def test_disabled(self, monkeypatch, library_file):
        assert await self._estimate(monkeypatch, library_file, DISABLED) == 4.38


class TestSpoolmanPricesAreTheWorkingBasis:
    """Spoolman carries no VAT flag on a price, so its figure is taken as
    already being in the working basis, like the default rate it falls back
    to. A future per-price flag would have to come through here."""

    def test_the_spoolman_rate_is_never_converted(self):
        from backend.app.services import spoolman_tracking

        spool = {"id": 1, "price": 20.0, "filament": {"price": 25.0, "weight": 1000}}
        with (
            patch("backend.app.services.vat.normalise_cost_per_kg") as normalise,
            patch("backend.app.services.vat.convert") as convert_,
        ):
            assert spoolman_tracking._spool_cost_per_gram(spool) == pytest.approx(0.02)
        normalise.assert_not_called()
        convert_.assert_not_called()
