"""Optional printer depreciation per printing hour (#694).

Contract under test:
- a printer carries optional ``purchase_price`` / ``expected_lifetime_hours``
  and returns them on every printer response shape;
- at completion the run's measured duration × the printer's hourly rate is
  written onto the PrintLogEntry, and onto the archive for its FIRST run only
  (the #1378 convention cost / energy_cost already follow);
- a printer without a price leaves every depreciation field None;
- the stats endpoint sums the per-run values;
- POST /archives/recalculate-costs never touches depreciation.
"""

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

import pytest
from httpx import AsyncClient
from sqlalchemy import select

from backend.app.models.print_log import PrintLogEntry
from backend.app.services.depreciation import (
    hourly_depreciation_rate,
    run_depreciation_cost,
    snapshot_run_depreciation,
)
from backend.app.services.print_log import run_duration_seconds, write_log_entry


@pytest.fixture(autouse=True)
def _mock_printer_test_connection():
    with patch(
        "backend.app.services.printer_manager.printer_manager.test_connection",
        new=AsyncMock(return_value={"success": True, "state": "IDLE", "model": "X1C"}),
    ):
        yield


async def _complete_run(db_session, archive, printer_id: int, hours: float) -> PrintLogEntry:
    """Replay the completion path's log-entry write for one run of ``hours``."""
    started = datetime(2026, 1, 1, 8, 0, tzinfo=timezone.utc)
    completed = started + timedelta(hours=hours)
    archive.started_at = started
    archive.completed_at = completed
    depreciation = await snapshot_run_depreciation(
        db_session, archive, printer_id, run_duration_seconds(started, completed)
    )
    entry = await write_log_entry(
        db_session,
        archive_id=archive.id,
        status="completed",
        printer_id=printer_id,
        started_at=started,
        completed_at=completed,
        depreciation_cost=depreciation,
    )
    await db_session.commit()
    return entry


class TestRateMath:
    def test_rate_requires_both_inputs_positive(self):
        assert hourly_depreciation_rate(1200.0, 6000.0) == pytest.approx(0.2)
        assert hourly_depreciation_rate(None, 6000.0) is None
        assert hourly_depreciation_rate(1200.0, None) is None
        assert hourly_depreciation_rate(0.0, 6000.0) is None
        assert hourly_depreciation_rate(1200.0, 0.0) is None

    def test_run_cost_is_hours_times_rate(self):
        # 2h30m at 0.20/h
        assert run_depreciation_cost(9000, 1200.0, 6000.0) == pytest.approx(0.5)
        assert run_depreciation_cost(None, 1200.0, 6000.0) is None
        assert run_depreciation_cost(9000, None, None) is None

    def test_reconciled_zero_duration_is_unknown_not_free(self):
        # A reconciled completion stores duration 0 because the real end time
        # is unknown (#2592). Unknown wear must stay None: a 0.0 would render
        # as "$0.00" on the card and, as the first-run snapshot, stick forever.
        assert run_depreciation_cost(run_duration_seconds(None, None, reconciled=True), 1200.0, 6000.0) is None
        assert run_depreciation_cost(0, 1200.0, 6000.0) is None


class TestPrinterFields:
    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_patch_round_trips_both_fields(self, async_client: AsyncClient, printer_factory, db_session):
        printer = await printer_factory()

        response = await async_client.patch(
            f"/api/v1/printers/{printer.id}",
            json={"purchase_price": 1200.0, "expected_lifetime_hours": 6000.0},
        )

        assert response.status_code == 200
        body = response.json()
        assert body["purchase_price"] == 1200.0
        assert body["expected_lifetime_hours"] == 6000.0

        listed = await async_client.get("/api/v1/printers/")
        row = next(p for p in listed.json() if p["id"] == printer.id)
        assert row["purchase_price"] == 1200.0
        assert row["expected_lifetime_hours"] == 6000.0

        single = await async_client.get(f"/api/v1/printers/{printer.id}")
        assert single.json()["purchase_price"] == 1200.0
        assert single.json()["expected_lifetime_hours"] == 6000.0

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_patch_can_clear_fields(self, async_client: AsyncClient, printer_factory, db_session):
        printer = await printer_factory(purchase_price=1200.0, expected_lifetime_hours=6000.0)

        response = await async_client.patch(
            f"/api/v1/printers/{printer.id}",
            json={"purchase_price": None, "expected_lifetime_hours": None},
        )

        assert response.status_code == 200
        assert response.json()["purchase_price"] is None
        assert response.json()["expected_lifetime_hours"] is None

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_negative_values_rejected(self, async_client: AsyncClient, printer_factory, db_session):
        printer = await printer_factory()

        response = await async_client.patch(f"/api/v1/printers/{printer.id}", json={"purchase_price": -1})

        assert response.status_code == 422

    @pytest.mark.asyncio
    @pytest.mark.integration
    @pytest.mark.parametrize("field", ["purchase_price", "expected_lifetime_hours"])
    @pytest.mark.parametrize("value", ["inf", "Infinity", "nan"])
    async def test_non_finite_values_rejected(
        self, async_client: AsyncClient, printer_factory, db_session, field: str, value: str
    ):
        # Lax float parsing accepts "inf" unless told otherwise; an infinite
        # rate would be snapshotted onto every later run.
        printer = await printer_factory()

        response = await async_client.patch(f"/api/v1/printers/{printer.id}", json={field: value})

        assert response.status_code == 422
        await db_session.refresh(printer)
        assert getattr(printer, field) is None

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_create_accepts_fields_and_defaults_to_none(self, async_client: AsyncClient, db_session):
        base = {
            "serial_number": "00M09A694000001",
            "ip_address": "192.168.1.150",
            "access_code": "12345678",
            "model": "X1C",
        }

        with_price = await async_client.post(
            "/api/v1/printers/",
            json={**base, "name": "Priced", "purchase_price": 900, "expected_lifetime_hours": 4500},
        )
        assert with_price.status_code == 200
        assert with_price.json()["purchase_price"] == 900.0
        assert with_price.json()["expected_lifetime_hours"] == 4500.0

        without = await async_client.post(
            "/api/v1/printers/",
            json={**base, "name": "Unpriced", "serial_number": "00M09A694000002", "ip_address": "192.168.1.151"},
        )
        assert without.status_code == 200
        assert without.json()["purchase_price"] is None
        assert without.json()["expected_lifetime_hours"] is None


class TestCompletionSnapshot:
    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_first_run_writes_entry_and_archive_second_run_entry_only(
        self, async_client: AsyncClient, archive_factory, printer_factory, db_session
    ):
        printer = await printer_factory(purchase_price=1200.0, expected_lifetime_hours=6000.0)
        archive = await archive_factory(printer.id, with_run=False)

        first = await _complete_run(db_session, archive, printer.id, hours=2.5)
        assert first.duration_seconds == 9000
        assert first.depreciation_cost == pytest.approx(0.5)
        await db_session.refresh(archive)
        assert archive.depreciation_cost == pytest.approx(0.5)

        # Price goes up before the reprint — the new rate applies to the new
        # run only, and the archive keeps its first-run figure.
        printer.purchase_price = 2400.0
        await db_session.commit()

        second = await _complete_run(db_session, archive, printer.id, hours=1.0)
        assert second.depreciation_cost == pytest.approx(0.4)
        await db_session.refresh(archive)
        assert archive.depreciation_cost == pytest.approx(0.5)
        assert first.depreciation_cost == pytest.approx(0.5)

        # Both shapes the client reads expose the snapshot.
        detail = await async_client.get(f"/api/v1/archives/{archive.id}")
        assert detail.json()["depreciation_cost"] == pytest.approx(0.5)
        runs = await async_client.get(f"/api/v1/archives/{archive.id}/runs")
        by_id = {r["id"]: r["depreciation_cost"] for r in runs.json()["items"]}
        assert by_id[first.id] == pytest.approx(0.5)
        assert by_id[second.id] == pytest.approx(0.4)

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_printer_without_price_leaves_everything_none(
        self, async_client: AsyncClient, archive_factory, printer_factory, db_session
    ):
        printer = await printer_factory()
        archive = await archive_factory(printer.id, with_run=False)

        entry = await _complete_run(db_session, archive, printer.id, hours=3.0)

        assert entry.depreciation_cost is None
        await db_session.refresh(archive)
        assert archive.depreciation_cost is None
        detail = await async_client.get(f"/api/v1/archives/{archive.id}")
        assert detail.json()["depreciation_cost"] is None
        stats = await async_client.get("/api/v1/archives/stats")
        assert stats.json()["total_depreciation_cost"] == 0

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_reconciled_run_leaves_archive_slot_open_for_a_real_run(
        self, archive_factory, printer_factory, db_session
    ):
        printer = await printer_factory(purchase_price=1200.0, expected_lifetime_hours=6000.0)
        archive = await archive_factory(printer.id, with_run=False)

        reconciled = await snapshot_run_depreciation(
            db_session, archive, printer.id, run_duration_seconds(None, None, reconciled=True)
        )
        assert reconciled is None
        assert archive.depreciation_cost is None

        # The reprint that follows is the archive's first priced run.
        real = await _complete_run(db_session, archive, printer.id, hours=1.0)
        assert real.depreciation_cost == pytest.approx(0.2)
        await db_session.refresh(archive)
        assert archive.depreciation_cost == pytest.approx(0.2)

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_missing_printer_leaves_none(self, archive_factory, printer_factory, db_session):
        printer = await printer_factory(purchase_price=1200.0, expected_lifetime_hours=6000.0)
        archive = await archive_factory(printer.id, with_run=False)

        assert await snapshot_run_depreciation(db_session, archive, None, 3600) is None
        assert await snapshot_run_depreciation(db_session, archive, printer.id + 1000, 3600) is None
        assert archive.depreciation_cost is None


class TestCompletionHook:
    """Drive the real ``on_print_complete`` against the test database.

    The service tests above pin the math; this pins the block in main.py that
    wires it in — snapshot BEFORE write_log_entry, the ``_reconciled`` flag
    forwarded, and the value reaching the log entry.
    """

    @staticmethod
    def _mock_stack(stack, test_engine):
        from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

        from backend.app.services.bambu_ftp import DeleteResult

        maker = async_sessionmaker(test_engine, class_=AsyncSession, expire_on_commit=False)
        stack.enter_context(patch("backend.app.main.async_session", maker))
        stack.enter_context(patch("backend.app.core.database.async_session", maker))
        # SD-card cleanup (#374) would otherwise FTP to the factory printer's IP.
        stack.enter_context(
            patch("backend.app.services.bambu_ftp.delete_file_async", AsyncMock(return_value=DeleteResult.NOT_FOUND))
        )
        stack.enter_context(patch("backend.app.main.notification_service")).on_print_complete = AsyncMock()
        stack.enter_context(patch("backend.app.main.smart_plug_manager")).on_print_complete = AsyncMock()
        mock_ws = stack.enter_context(patch("backend.app.main.ws_manager"))
        mock_ws.send_print_complete = AsyncMock()
        mock_ws.send_archive_updated = AsyncMock()
        mock_ws.broadcast = AsyncMock()
        # The energy / photo / maintenance follow-ups would otherwise hit the
        # test engine after the fixture tears it down.
        stack.enter_context(
            patch("backend.app.main.spawn_background_task", side_effect=lambda coro, **_kw: coro.close())
        )
        mock_relay = stack.enter_context(patch("backend.app.main.mqtt_relay"))
        mock_relay.on_print_complete = AsyncMock()
        mock_relay.on_queue_job_completed = AsyncMock()
        mock_pm = stack.enter_context(patch("backend.app.main.printer_manager"))
        mock_pm.get_printer.return_value = None
        mock_pm.get_current_print_user.return_value = None
        mock_pm.get_client.return_value = None

    async def _run_hook(self, stack, test_engine, printer_id: int, archive, payload: dict):
        from backend.app import main as main_module

        self._mock_stack(stack, test_engine)
        main_module._active_prints[(printer_id, archive.filename)] = archive.id
        try:
            await main_module.on_print_complete(printer_id, payload)
        finally:
            main_module._active_prints.pop((printer_id, archive.filename), None)

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_on_print_complete_prices_the_run_and_snapshots_the_archive(
        self, test_engine, archive_factory, printer_factory, db_session
    ):
        from contextlib import ExitStack

        printer = await printer_factory(purchase_price=1200.0, expected_lifetime_hours=6000.0)
        started = datetime.now(timezone.utc) - timedelta(hours=2, minutes=30)
        archive = await archive_factory(printer.id, with_run=False, status="printing", started_at=started)

        with ExitStack() as stack:
            await self._run_hook(
                stack,
                test_engine,
                printer.id,
                archive,
                {"status": "completed", "filename": archive.filename, "subtask_name": "Test Print"},
            )

        archive_id = archive.id
        db_session.expire_all()
        entry = (
            await db_session.execute(select(PrintLogEntry).where(PrintLogEntry.archive_id == archive_id))
        ).scalar_one()
        assert entry.duration_seconds is not None and entry.duration_seconds >= 9000
        expected = round(entry.duration_seconds / 3600 * 0.2, 3)
        assert entry.depreciation_cost == pytest.approx(expected)
        await db_session.refresh(archive)
        assert archive.status == "completed"
        assert archive.depreciation_cost == pytest.approx(expected)

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_on_print_complete_reconciled_run_stores_unknown(
        self, test_engine, archive_factory, printer_factory, db_session
    ):
        from contextlib import ExitStack

        printer = await printer_factory(purchase_price=1200.0, expected_lifetime_hours=6000.0)
        started = datetime.now(timezone.utc) - timedelta(hours=8)
        archive = await archive_factory(printer.id, with_run=False, status="printing", started_at=started)

        with ExitStack() as stack:
            await self._run_hook(
                stack,
                test_engine,
                printer.id,
                archive,
                {
                    "status": "completed",
                    "filename": archive.filename,
                    "subtask_name": "Test Print",
                    "_reconciled": True,
                },
            )

        archive_id = archive.id
        db_session.expire_all()
        entry = (
            await db_session.execute(select(PrintLogEntry).where(PrintLogEntry.archive_id == archive_id))
        ).scalar_one()
        assert entry.duration_seconds == 0
        assert entry.depreciation_cost is None
        await db_session.refresh(archive)
        assert archive.failure_reason == "noStatusUpdate"
        assert archive.depreciation_cost is None


class TestStatsAndRecalculate:
    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_stats_sum_depreciation_over_runs(
        self, async_client: AsyncClient, archive_factory, printer_factory, db_session
    ):
        printer = await printer_factory(purchase_price=1200.0, expected_lifetime_hours=6000.0)
        archive = await archive_factory(printer.id, with_run=False)
        await _complete_run(db_session, archive, printer.id, hours=2.5)  # 0.5
        await _complete_run(db_session, archive, printer.id, hours=1.0)  # 0.2
        other = await archive_factory(printer.id, with_run=False)
        await _complete_run(db_session, other, printer.id, hours=0.5)  # 0.1

        response = await async_client.get("/api/v1/archives/stats")

        assert response.status_code == 200
        assert response.json()["total_depreciation_cost"] == pytest.approx(0.8)
        slim = await async_client.get("/api/v1/archives/slim")
        assert sorted(r["depreciation_cost"] for r in slim.json()) == pytest.approx([0.1, 0.2, 0.5])

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_recalculate_costs_leaves_depreciation_untouched(
        self, async_client: AsyncClient, archive_factory, printer_factory, db_session
    ):
        printer = await printer_factory(purchase_price=1200.0, expected_lifetime_hours=6000.0)
        archive = await archive_factory(printer.id, with_run=False, filament_used_grams=100.0)
        entry_id = (await _complete_run(db_session, archive, printer.id, hours=2.5)).id

        printer.purchase_price = 9999.0
        await db_session.commit()
        response = await async_client.post("/api/v1/archives/recalculate-costs")
        assert response.status_code == 200

        db_session.expire_all()
        await db_session.refresh(archive)
        assert archive.depreciation_cost == pytest.approx(0.5)
        row = (await db_session.execute(select(PrintLogEntry).where(PrintLogEntry.id == entry_id))).scalar_one()
        assert row.depreciation_cost == pytest.approx(0.5)
