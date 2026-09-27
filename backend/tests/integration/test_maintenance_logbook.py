"""The maintenance logbook: who marked a maintenance done and how, the card's
"last done" line, and one timeline of everything done plus every calibration
run that did not complete."""

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.app.models.maintenance import MaintenanceHistory, MaintenanceRun
from backend.app.services import maintenance_actions

pytestmark = [pytest.mark.asyncio, pytest.mark.integration]

LOGBOOK = "/api/v1/maintenance/logbook"


def _naive(dt: datetime) -> datetime:
    return dt.astimezone(timezone.utc).replace(tzinfo=None)


async def _items(async_client: AsyncClient, printer_id: int) -> dict[str, dict]:
    response = await async_client.get(f"/api/v1/maintenance/printers/{printer_id}")
    assert response.status_code == 200, response.text
    return {item["maintenance_type_name"]: item for item in response.json()["maintenance_items"]}


async def _calibration(async_client: AsyncClient, printer_id: int) -> dict:
    return next(i for i in (await _items(async_client, printer_id)).values() if i["action"] == "calibration")


async def _manual(async_client: AsyncClient, printer_id: int) -> dict:
    return next(i for i in (await _items(async_client, printer_id)).values() if not i["action"])


class TestMarkingDone:
    async def test_the_note_and_the_hours_reach_the_card(self, async_client, printer_factory):
        printer = await printer_factory()
        item = await _manual(async_client, printer.id)

        response = await async_client.post(
            f"/api/v1/maintenance/items/{item['id']}/perform", json={"notes": "  new nozzle, 0.4 hardened  "}
        )
        assert response.status_code == 200, response.text

        card = next(i for i in (await _items(async_client, printer.id)).values() if i["id"] == item["id"])
        assert card["last_performed_at"] is not None
        assert card["last_performed_notes"] == "new nozzle, 0.4 hardened"
        assert card["last_performed_source"] == "manual"
        assert card["last_performed_hours_at"] == pytest.approx(0.0)
        assert card["last_performed_by"] is None  # auth is off: nobody to name

    async def test_an_empty_note_is_no_note(self, async_client, printer_factory, db_session):
        printer = await printer_factory()
        item = await _manual(async_client, printer.id)

        await async_client.post(f"/api/v1/maintenance/items/{item['id']}/perform", json={"notes": "   "})

        entry = (await db_session.execute(select(MaintenanceHistory))).scalar_one()
        assert entry.notes is None

    async def test_the_user_who_marked_it_is_recorded(self, async_client, printer_factory):
        printer = await printer_factory()
        item = await _manual(async_client, printer.id)
        setup = await async_client.post(
            "/api/v1/auth/setup",
            json={"auth_enabled": True, "admin_username": "werkstatt", "admin_password": "AdminPass1!"},
        )
        assert setup.status_code == 200, setup.text
        login = await async_client.post("/api/v1/auth/login", json={"username": "werkstatt", "password": "AdminPass1!"})
        headers = {"Authorization": f"Bearer {login.json()['access_token']}"}

        response = await async_client.post(
            f"/api/v1/maintenance/items/{item['id']}/perform", json={"notes": None}, headers=headers
        )
        assert response.status_code == 200, response.text

        logbook = await async_client.get(LOGBOOK, headers=headers)
        [entry] = logbook.json()["entries"]
        assert entry["performed_by"] == "werkstatt"
        assert entry["source"] == "manual"


class TestCalibrationRuns:
    async def _run(self, db_session, item: dict, printer_id: int, **values) -> MaintenanceRun:
        run = MaintenanceRun(
            printer_maintenance_id=item["id"],
            printer_id=printer_id,
            options={"bed_leveling": True},
            **values,
        )
        db_session.add(run)
        await db_session.commit()
        return run

    async def test_a_completed_run_is_an_automatic_entry_naming_its_trigger(
        self, async_client, printer_factory, db_session, test_engine
    ):
        printer = await printer_factory()
        item = await _calibration(async_client, printer.id)
        run = await self._run(
            db_session,
            item,
            printer.id,
            status="running",
            source="schedule",
            started_at=_naive(datetime.now(timezone.utc)),
        )

        maker = async_sessionmaker(test_engine, class_=AsyncSession, expire_on_commit=False)
        with (
            patch("backend.app.services.maintenance_actions.async_session", maker),
            patch(
                "backend.app.services.notification_service.notification_service.on_maintenance_run",
                new_callable=AsyncMock,
            ),
        ):
            closed = await maintenance_actions.on_internal_job_finished(
                printer.id,
                "/usr/etc/print/H2S/auto_cali_for_user_param.gcode",
                "auto_cali_for_user_param.gcode",
                "completed",
                None,
            )
        assert closed

        [entry] = (await async_client.get(LOGBOOK)).json()["entries"]
        assert entry["kind"] == "performed"
        assert entry["outcome"] == "completed"
        assert entry["source"] == "automatic"
        assert entry["trigger"] == "schedule"
        assert entry["run_id"] == run.id
        card = next(i for i in (await _items(async_client, printer.id)).values() if i["id"] == item["id"])
        assert card["last_performed_source"] == "automatic"

    async def test_failed_and_cancelled_runs_are_in_the_record_pending_ones_are_not(
        self, async_client, printer_factory, db_session
    ):
        printer = await printer_factory()
        item = await _calibration(async_client, printer.id)
        now = datetime.now(timezone.utc)
        await self._run(
            db_session,
            item,
            printer.id,
            status="failed",
            source="due",
            error_message="Calibration failed (print_error 50348134)",
            completed_at=_naive(now - timedelta(hours=2)),
        )
        await self._run(
            db_session,
            item,
            printer.id,
            status="cancelled",
            source="manual",
            completed_at=_naive(now - timedelta(hours=1)),
        )
        await self._run(db_session, item, printer.id, status="pending", source="manual")

        entries = (await async_client.get(LOGBOOK)).json()["entries"]

        assert [(e["kind"], e["outcome"], e["source"]) for e in entries] == [
            ("run", "cancelled", "manual"),
            ("run", "failed", "due"),
        ]
        assert entries[1]["notes"] == "Calibration failed (print_error 50348134)"
        assert entries[1]["maintenance_type_name"] == "Printer Calibration"


class TestTheLogbook:
    async def test_newest_first_across_printers_and_filtered(self, async_client, printer_factory, db_session):
        first = await printer_factory(name="H2S 01")
        second = await printer_factory(name="H2S 02")
        first_item = await _manual(async_client, first.id)
        second_item = await _manual(async_client, second.id)
        other_item = next(
            i
            for i in (await _items(async_client, first.id)).values()
            if not i["action"] and i["id"] != first_item["id"]
        )

        for item in (first_item, second_item, other_item):
            response = await async_client.post(f"/api/v1/maintenance/items/{item['id']}/perform", json={})
            assert response.status_code == 200, response.text
        # Spread the entries out in time, oldest first.
        rows = (await db_session.execute(select(MaintenanceHistory).order_by(MaintenanceHistory.id))).scalars().all()
        base = datetime(2026, 9, 1, 8, 0)
        for offset, row in enumerate(rows):
            row.performed_at = base + timedelta(days=offset)
        await db_session.commit()

        everything = (await async_client.get(LOGBOOK)).json()
        assert everything["total"] == 3
        assert [e["printer_name"] for e in everything["entries"]] == ["H2S 01", "H2S 02", "H2S 01"]
        assert everything["entries"][0]["at"].endswith("Z")

        mine = (await async_client.get(LOGBOOK, params={"printer_id": first.id})).json()
        assert {e["printer_name"] for e in mine["entries"]} == {"H2S 01"}
        assert mine["total"] == 2

        one_type = (
            await async_client.get(
                LOGBOOK, params={"printer_id": first.id, "maintenance_type_id": first_item["maintenance_type_id"]}
            )
        ).json()
        assert [e["maintenance_type_id"] for e in one_type["entries"]] == [first_item["maintenance_type_id"]]

    async def test_the_limit_keeps_the_newest_and_reports_the_total(self, async_client, printer_factory):
        printer = await printer_factory()
        item = await _manual(async_client, printer.id)
        for note in ("first", "second", "third"):
            await async_client.post(f"/api/v1/maintenance/items/{item['id']}/perform", json={"notes": note})

        response = (await async_client.get(LOGBOOK, params={"limit": 2})).json()

        assert response["total"] == 3
        assert [e["notes"] for e in response["entries"]] == ["third", "second"]
