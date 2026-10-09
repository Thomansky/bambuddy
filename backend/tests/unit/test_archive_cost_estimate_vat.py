"""The print-start cost estimate prices inventory spools in the VAT working basis.

Upstream's estimate (#3261) read ``Spool.cost_per_kg`` as stored. The fork's
completion writers convert it to ``price_vat_basis`` first (``vat.py``), so with
a net basis a gross-entered spool showed a ~19% too high cost while the print
ran -- and kept it when completion produced no usage results. Spoolman prices
are taken as already in the working basis and stay unconverted.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from backend.app.models.settings import Settings
from backend.app.models.spool import Spool
from backend.app.models.spool_assignment import SpoolAssignment
from backend.app.services import archive_cost_estimate
from backend.app.services.archive_cost_estimate import _internal_rates, estimate_archive_cost_at_start
from backend.app.services.vat import VatContext

DEFAULT_PER_KG = 30.0


def _printer_manager():
    trays = [{"id": i, "tray_type": "PLA", "tray_color": "FFFFFFFF"} for i in range(4)]
    state = SimpleNamespace(raw_data={"ams": [{"id": 0, "tray": trays}], "vt_tray": []})
    return SimpleNamespace(get_status=lambda _pid: state)


@pytest.fixture
def three_mf(tmp_path, monkeypatch):
    """Point base_dir at tmp_path, drop a 3MF there, and stub its per-slot usage."""
    monkeypatch.setattr(archive_cost_estimate.app_settings, "base_dir", tmp_path)
    (tmp_path / "archive").mkdir()
    (tmp_path / "archive" / "print.3mf").write_bytes(b"3mf")
    usage: list[dict] = []
    monkeypatch.setattr(
        "backend.app.utils.threemf_tools.extract_filament_usage_from_3mf",
        lambda _path, _plate: usage,
    )
    return usage


@pytest.fixture
def ws():
    with patch("backend.app.core.websocket.ws_manager.send_archive_updated", new=AsyncMock()) as sent:
        yield sent


def _vat_settings(db_session, *, enabled: bool):
    db_session.add(Settings(key="default_filament_cost", value=str(DEFAULT_PER_KG)))
    if enabled:
        db_session.add(Settings(key="vat_enabled", value="true"))
        db_session.add(Settings(key="vat_rate_percent", value="19"))
        db_session.add(Settings(key="price_vat_basis", value="net"))


async def _assign_spool(db_session, printer_id: int, tray_id: int, cost_per_kg: float, vat_included: bool):
    spool = Spool(material="PLA", cost_per_kg=cost_per_kg, cost_vat_included=vat_included)
    db_session.add(spool)
    await db_session.flush()
    db_session.add(SpoolAssignment(spool_id=spool.id, printer_id=printer_id, ams_id=0, tray_id=tray_id))
    await db_session.commit()


async def _printing_archive(db_session, printer_factory, archive_factory, grams: float = 1000.0):
    printer = await printer_factory()
    archive = await archive_factory(
        printer.id,
        status="printing",
        cost=30.0,
        with_run=False,
        file_path="archive/print.3mf",
        filament_used_grams=grams,
    )
    await db_session.commit()
    return printer, archive


class TestInternalRates:
    @pytest.mark.asyncio
    async def test_gross_spool_is_netted_under_a_net_basis(self, db_session, printer_factory):
        _vat_settings(db_session, enabled=True)
        printer = await printer_factory()
        await _assign_spool(db_session, printer.id, 0, cost_per_kg=25.0, vat_included=True)

        rates = await _internal_rates(db_session, printer.id, {0}, DEFAULT_PER_KG)

        # 25.00 / 1.19 = 21.0084 per kg
        assert rates[0] * 1000.0 == pytest.approx(21.0084)

    @pytest.mark.asyncio
    async def test_vat_disabled_uses_the_raw_price(self, db_session, printer_factory):
        _vat_settings(db_session, enabled=False)
        printer = await printer_factory()
        await _assign_spool(db_session, printer.id, 0, cost_per_kg=25.0, vat_included=True)

        rates = await _internal_rates(db_session, printer.id, {0}, DEFAULT_PER_KG)

        assert rates[0] * 1000.0 == pytest.approx(25.0)

    @pytest.mark.asyncio
    async def test_spool_already_in_the_working_basis_is_untouched(self, db_session, printer_factory):
        _vat_settings(db_session, enabled=True)
        printer = await printer_factory()
        await _assign_spool(db_session, printer.id, 0, cost_per_kg=25.0, vat_included=False)

        rates = await _internal_rates(db_session, printer.id, {0}, DEFAULT_PER_KG)

        assert rates[0] * 1000.0 == pytest.approx(25.0)

    @pytest.mark.asyncio
    async def test_vat_settings_are_read_once_per_estimate(self, db_session, printer_factory):
        printer = await printer_factory()
        for tray_id in range(3):
            await _assign_spool(db_session, printer.id, tray_id, cost_per_kg=25.0, vat_included=True)
        net_ctx = VatContext(enabled=True, rate_percent=19.0, basis="net")

        with patch.object(VatContext, "load", AsyncMock(return_value=net_ctx)) as load:
            rates = await _internal_rates(db_session, printer.id, {0, 1, 2}, DEFAULT_PER_KG)

        load.assert_awaited_once()
        assert all(rate * 1000.0 == pytest.approx(21.0084) for rate in rates.values())


class TestEstimateAtStart:
    @pytest.mark.asyncio
    async def test_archive_cost_in_the_net_basis(self, db_session, printer_factory, archive_factory, three_mf, ws):
        _vat_settings(db_session, enabled=True)
        printer, archive = await _printing_archive(db_session, printer_factory, archive_factory)
        await _assign_spool(db_session, printer.id, 0, cost_per_kg=25.0, vat_included=True)
        three_mf.append({"slot_id": 1, "used_g": 1000.0})

        await estimate_archive_cost_at_start(db_session, printer.id, archive.id, _printer_manager(), ams_mapping=[0])

        await db_session.refresh(archive)
        assert archive.cost == 21.01
        ws.assert_awaited_once_with({"id": archive.id, "cost": 21.01})

    @pytest.mark.asyncio
    async def test_archive_cost_with_vat_disabled(self, db_session, printer_factory, archive_factory, three_mf, ws):
        _vat_settings(db_session, enabled=False)
        printer, archive = await _printing_archive(db_session, printer_factory, archive_factory)
        await _assign_spool(db_session, printer.id, 0, cost_per_kg=25.0, vat_included=True)
        three_mf.append({"slot_id": 1, "used_g": 1000.0})

        await estimate_archive_cost_at_start(db_session, printer.id, archive.id, _printer_manager(), ams_mapping=[0])

        await db_session.refresh(archive)
        assert archive.cost == 25.00

    @pytest.mark.asyncio
    async def test_spoolman_prices_are_not_converted(
        self, db_session, printer_factory, archive_factory, three_mf, ws, monkeypatch
    ):
        _vat_settings(db_session, enabled=True)
        db_session.add(Settings(key="spoolman_enabled", value="true"))
        printer, archive = await _printing_archive(db_session, printer_factory, archive_factory)
        three_mf.append({"slot_id": 1, "used_g": 1000.0})
        monkeypatch.setattr(archive_cost_estimate, "_spoolman_rates", AsyncMock(return_value={0: 0.025}))

        await estimate_archive_cost_at_start(db_session, printer.id, archive.id, _printer_manager(), ams_mapping=[0])

        await db_session.refresh(archive)
        assert archive.cost == 25.00
