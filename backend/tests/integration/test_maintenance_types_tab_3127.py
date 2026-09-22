"""The maintenance types tab (#3127): coverage, assign/unassign, restore, custom actions.

What the tab needs from the API and what it must never do: a type says how
far it reaches across the fleet, ticking a printer switches a disabled item
back on instead of starting a new one, unticking leaves the history alone and
the overview does not undo it, a hidden type comes back with its items, and a
custom type can carry one of the two actions -- but only on printers that can
run it.
"""

import pytest
from httpx import AsyncClient
from sqlalchemy import select

from backend.app.models.maintenance import MaintenanceHistory, MaintenanceRun, MaintenanceType, PrinterMaintenance

pytestmark = [pytest.mark.asyncio, pytest.mark.integration]

VISION_TYPE = "Vision Encoder Calibration"
CALIBRATION_TYPE = "Printer Calibration"


async def _types(async_client: AsyncClient) -> dict[str, dict]:
    response = await async_client.get("/api/v1/maintenance/types")
    assert response.status_code == 200, response.text
    return {t["name"]: t for t in response.json()}


async def _items(async_client: AsyncClient, printer_id: int) -> dict[str, dict]:
    response = await async_client.get(f"/api/v1/maintenance/printers/{printer_id}")
    assert response.status_code == 200, response.text
    return {i["maintenance_type_name"]: i for i in response.json()["maintenance_items"]}


class TestCoverage:
    async def test_a_universal_type_covers_every_printer_once_the_items_exist(self, async_client, printer_factory):
        h2s = await printer_factory(name="H2S", model="H2S")
        x1c = await printer_factory(name="X1C", model="X1C")
        # The overview is what creates the items for the system types.
        await _items(async_client, h2s.id)
        await _items(async_client, x1c.id)

        plate = (await _types(async_client))["Clean Build Plate"]
        assert plate["eligible_count"] == 2
        assert plate["printer_count"] == 2
        assert sorted(plate["printer_ids"]) == sorted([h2s.id, x1c.id])

    async def test_the_vision_type_is_only_eligible_on_the_h2_series(self, async_client, printer_factory):
        h2s = await printer_factory(name="H2S", model="H2S")
        x1c = await printer_factory(name="X1C", model="X1C")
        await _items(async_client, h2s.id)
        await _items(async_client, x1c.id)

        vision = (await _types(async_client))[VISION_TYPE]
        assert vision["eligible_count"] == 1
        assert vision["printer_count"] == 1
        assert vision["printer_ids"] == [h2s.id]
        assert vision["eligible_printer_ids"] == [h2s.id]

        # ... and the X1C never gets a card for it at all.
        assert VISION_TYPE not in await _items(async_client, x1c.id)

    async def test_the_overview_says_which_actions_the_printer_can_run(self, async_client, printer_factory):
        h2s = await printer_factory(name="H2S", model="H2S")
        x1c = await printer_factory(name="X1C", model="X1C")

        response = await async_client.get(f"/api/v1/maintenance/printers/{h2s.id}")
        assert sorted(response.json()["available_actions"]) == ["calibration", "motion_precision"]
        response = await async_client.get(f"/api/v1/maintenance/printers/{x1c.id}")
        assert response.json()["available_actions"] == ["calibration"]

    async def test_the_coverage_is_right_before_any_overview_load(self, async_client, printer_factory):
        """A printer added since the last overview load is still covered.

        The items of a system type used to be created by the overview alone,
        so the tab read "on 0 of 2 printers" until it had run -- and ticking
        the box then answered "already assigned" (#3127).
        """
        a = await printer_factory(name="Fresh A", model="X1C")
        b = await printer_factory(name="Fresh B", model="X1C")

        plate = (await _types(async_client))["Clean Build Plate"]
        assert plate["eligible_count"] == 2
        assert plate["printer_count"] == 2
        assert sorted(plate["printer_ids"]) == sorted([a.id, b.id])

        # ... and the items the tab just counted are the ones the overview uses.
        assert (await _items(async_client, a.id))["Clean Build Plate"]["enabled"] is True

    async def test_the_types_list_does_not_resurrect_a_switched_off_item(self, async_client, printer_factory):
        printer = await printer_factory(name="Stays off", model="X1C")
        item = (await _items(async_client, printer.id))["Clean Build Plate"]
        assert (
            await async_client.patch(f"/api/v1/maintenance/items/{item['id']}", json={"enabled": False})
        ).status_code == 200

        assert (await _types(async_client))["Clean Build Plate"]["printer_count"] == 0
        again = (await _items(async_client, printer.id))["Clean Build Plate"]
        assert again["id"] == item["id"]
        assert again["enabled"] is False

    async def test_a_disabled_item_does_not_count_towards_the_coverage(self, async_client, printer_factory):
        a = await printer_factory(name="A", model="X1C")
        b = await printer_factory(name="B", model="X1C")
        item = (await _items(async_client, a.id))["Clean Build Plate"]
        await _items(async_client, b.id)

        response = await async_client.patch(f"/api/v1/maintenance/items/{item['id']}", json={"enabled": False})
        assert response.status_code == 200, response.text

        plate = (await _types(async_client))["Clean Build Plate"]
        assert plate["eligible_count"] == 2
        assert plate["printer_count"] == 1
        assert plate["printer_ids"] == [b.id]
        # Still a printer the type applies to, so the tab keeps its checkbox.
        assert sorted(plate["eligible_printer_ids"]) == sorted([a.id, b.id])


class TestAssignAndUnassign:
    async def test_unticking_disables_the_item_and_keeps_its_history(self, async_client, printer_factory, db_session):
        printer = await printer_factory(name="Keeps history", model="X1C")
        item = (await _items(async_client, printer.id))["Clean Build Plate"]
        assert (
            await async_client.post(f"/api/v1/maintenance/items/{item['id']}/perform", json={"notes": "done"})
        ).status_code == 200

        response = await async_client.patch(f"/api/v1/maintenance/items/{item['id']}", json={"enabled": False})
        assert response.status_code == 200, response.text

        rows = (
            (
                await db_session.execute(
                    select(MaintenanceHistory).where(MaintenanceHistory.printer_maintenance_id == item["id"])
                )
            )
            .scalars()
            .all()
        )
        assert len(rows) == 1

    async def test_the_overview_does_not_resurrect_a_disabled_system_item(self, async_client, printer_factory):
        printer = await printer_factory(name="Deliberately off", model="X1C")
        item = (await _items(async_client, printer.id))["Clean Build Plate"]
        assert (
            await async_client.patch(f"/api/v1/maintenance/items/{item['id']}", json={"enabled": False})
        ).status_code == 200

        # Two more overview loads: neither may create a second item nor flip
        # the flag back on.
        await _items(async_client, printer.id)
        again = (await _items(async_client, printer.id))["Clean Build Plate"]
        assert again["id"] == item["id"]
        assert again["enabled"] is False

        response = await async_client.get("/api/v1/maintenance/overview")
        assert response.status_code == 200
        plate_items = [
            i
            for overview in response.json()
            for i in overview["maintenance_items"]
            if i["maintenance_type_name"] == "Clean Build Plate" and i["printer_id"] == printer.id
        ]
        assert len(plate_items) == 1
        assert plate_items[0]["enabled"] is False

    async def test_ticking_a_printer_again_switches_the_same_item_back_on(self, async_client, printer_factory):
        printer = await printer_factory(name="Back on", model="X1C")
        item = (await _items(async_client, printer.id))["Clean Build Plate"]
        type_id = item["maintenance_type_id"]
        assert (
            await async_client.patch(f"/api/v1/maintenance/items/{item['id']}", json={"enabled": False})
        ).status_code == 200

        response = await async_client.post(f"/api/v1/maintenance/printers/{printer.id}/assign/{type_id}")
        assert response.status_code == 200, response.text
        assert response.json()["id"] == item["id"]
        assert response.json()["enabled"] is True

        assert (await _types(async_client))["Clean Build Plate"]["printer_ids"] == [printer.id]

    async def test_ticking_a_printer_that_already_has_it_on_is_refused(self, async_client, printer_factory):
        printer = await printer_factory(name="Already on", model="X1C")
        item = (await _items(async_client, printer.id))["Clean Build Plate"]

        response = await async_client.post(
            f"/api/v1/maintenance/printers/{printer.id}/assign/{item['maintenance_type_id']}"
        )
        assert response.status_code == 400

    async def test_a_type_the_printer_model_cannot_use_is_refused(self, async_client, printer_factory):
        h2s = await printer_factory(name="H2S", model="H2S")
        x1c = await printer_factory(name="X1C", model="X1C")
        vision_type_id = (await _items(async_client, h2s.id))[VISION_TYPE]["maintenance_type_id"]

        response = await async_client.post(f"/api/v1/maintenance/printers/{x1c.id}/assign/{vision_type_id}")
        assert response.status_code == 400
        assert "printer model" in response.json()["detail"]


class TestRunsOfItemsSwitchedOff:
    """Switching an item off takes its queued run with it (#3127).

    The card is the only place a run can be cancelled from, and it leaves
    the printer section the moment the item is off or its type is hidden.
    A run left pending would go on holding the print queue and would still
    be sent to the printer once its wait cleared.
    """

    async def _pending_calibration_run(self, async_client, printer_factory, name="Runs"):
        printer = await printer_factory(name=name, model="X1C")
        item = (await _items(async_client, printer.id))[CALIBRATION_TYPE]
        queued = await async_client.post(f"/api/v1/maintenance/items/{item['id']}/run")
        assert queued.status_code == 200, queued.text
        assert queued.json()["status"] == "pending"
        return printer, item, queued.json()

    async def _run_status(self, async_client, item_id: int, run_id: int) -> str:
        response = await async_client.get(f"/api/v1/maintenance/items/{item_id}/runs")
        assert response.status_code == 200, response.text
        return next(r["status"] for r in response.json() if r["id"] == run_id)

    async def test_switching_the_item_off_cancels_its_pending_run(self, async_client, printer_factory):
        printer, item, run = await self._pending_calibration_run(async_client, printer_factory, "Off")

        response = await async_client.patch(f"/api/v1/maintenance/items/{item['id']}", json={"enabled": False})
        assert response.status_code == 200, response.text

        assert await self._run_status(async_client, item["id"], run["id"]) == "cancelled"
        # Nothing is left for the scheduler to dispatch or to hold the queue with.
        assert (await _items(async_client, printer.id))[CALIBRATION_TYPE]["current_run"] is None

    async def test_unticking_the_printer_on_the_types_tab_cancels_it_too(self, async_client, printer_factory):
        # The tab's untick is the same PATCH the card's toggle sends.
        printer, item, run = await self._pending_calibration_run(async_client, printer_factory, "Unticked")
        assert (
            await async_client.patch(f"/api/v1/maintenance/items/{item['id']}", json={"enabled": False})
        ).status_code == 200
        assert await self._run_status(async_client, item["id"], run["id"]) == "cancelled"

    async def test_hiding_the_type_cancels_the_pending_runs_of_its_items(self, async_client, printer_factory):
        printer, item, run = await self._pending_calibration_run(async_client, printer_factory, "Hidden type")

        response = await async_client.delete(f"/api/v1/maintenance/types/{item['maintenance_type_id']}")
        assert response.status_code == 200, response.text

        assert await self._run_status(async_client, item["id"], run["id"]) == "cancelled"
        # The card is gone from the printer section, so there would be no way back.
        assert CALIBRATION_TYPE not in await _items(async_client, printer.id)

    async def test_a_run_already_on_the_printer_is_left_to_finish(self, async_client, printer_factory, db_session):
        printer, item, run = await self._pending_calibration_run(async_client, printer_factory, "Running")
        row = (await db_session.execute(select(MaintenanceRun).where(MaintenanceRun.id == run["id"]))).scalar_one()
        row.status = "running"
        await db_session.commit()

        assert (
            await async_client.patch(f"/api/v1/maintenance/items/{item['id']}", json={"enabled": False})
        ).status_code == 200

        # The command is on the printer; its own completion event closes it.
        assert await self._run_status(async_client, item["id"], run["id"]) == "running"


class TestDeletedTypes:
    async def test_a_hidden_system_type_is_listed_with_its_items_and_comes_back(self, async_client, printer_factory):
        printer = await printer_factory(name="Restores", model="X1C")
        item = (await _items(async_client, printer.id))["Clean Build Plate"]
        type_id = item["maintenance_type_id"]

        assert (await async_client.delete(f"/api/v1/maintenance/types/{type_id}")).status_code == 200
        assert "Clean Build Plate" not in await _items(async_client, printer.id)

        response = await async_client.get("/api/v1/maintenance/types/deleted")
        assert response.status_code == 200, response.text
        hidden = {t["name"]: t for t in response.json()}
        assert hidden["Clean Build Plate"]["deleted_at"] is not None
        assert hidden["Clean Build Plate"]["deleted_at"].endswith("Z")
        assert hidden["Clean Build Plate"]["item_count"] == 1
        assert hidden["Clean Build Plate"]["is_system"] is True

        response = await async_client.post(f"/api/v1/maintenance/types/{type_id}/restore")
        assert response.status_code == 200, response.text
        assert response.json()["printer_ids"] == [printer.id]

        back = (await _items(async_client, printer.id))["Clean Build Plate"]
        assert back["id"] == item["id"]
        assert (await async_client.get("/api/v1/maintenance/types/deleted")).json() == []

    async def test_a_custom_type_is_hidden_rather_than_erased(self, async_client, printer_factory, db_session):
        printer = await printer_factory(name="Custom restore", model="X1C")
        created = await async_client.post(
            "/api/v1/maintenance/types",
            json={"name": "Replace HEPA Filter", "default_interval_hours": 500.0, "printer_ids": [printer.id]},
        )
        assert created.status_code == 200, created.text
        type_id = created.json()["id"]
        assert created.json()["printer_count"] == 1

        assert (await async_client.delete(f"/api/v1/maintenance/types/{type_id}")).status_code == 200
        assert "Replace HEPA Filter" not in await _types(async_client)
        assert "Replace HEPA Filter" not in await _items(async_client, printer.id)

        row = (await db_session.execute(select(MaintenanceType).where(MaintenanceType.id == type_id))).scalar_one()
        assert row.is_deleted is True
        items = (
            (
                await db_session.execute(
                    select(PrinterMaintenance).where(PrinterMaintenance.maintenance_type_id == type_id)
                )
            )
            .scalars()
            .all()
        )
        assert len(items) == 1

        assert (await async_client.post(f"/api/v1/maintenance/types/{type_id}/restore")).status_code == 200
        assert "Replace HEPA Filter" in await _items(async_client, printer.id)

    async def test_restoring_an_unknown_type_is_a_404(self, async_client):
        assert (await async_client.post("/api/v1/maintenance/types/987654/restore")).status_code == 404


class TestCustomActionTypes:
    async def test_a_custom_calibration_type_creates_items_that_carry_the_action(self, async_client, printer_factory):
        printer = await printer_factory(name="Monthly", model="H2S")
        response = await async_client.post(
            "/api/v1/maintenance/types",
            json={
                "name": "Monthly full calibration",
                "default_interval_hours": 30.0,
                "interval_type": "days",
                "action": "calibration",
                "printer_ids": [printer.id],
            },
        )
        assert response.status_code == 200, response.text
        assert response.json()["action"] == "calibration"
        assert response.json()["is_system"] is False

        item = (await _items(async_client, printer.id))["Monthly full calibration"]
        assert item["action"] == "calibration"
        # The per-action defaults, exactly as the seeded type's card shows them.
        assert item["action_options"]["bed_leveling"] is True
        assert item["action_available_options"]

        # It runs like any other actionable item.
        queued = await async_client.post(f"/api/v1/maintenance/items/{item['id']}/run")
        assert queued.status_code == 200, queued.text
        assert queued.json()["status"] == "pending"

    async def test_a_custom_vision_type_is_refused_on_a_printer_without_the_hardware(
        self, async_client, printer_factory
    ):
        h2s = await printer_factory(name="H2S", model="H2S")
        x1c = await printer_factory(name="X1C", model="X1C")

        response = await async_client.post(
            "/api/v1/maintenance/types",
            json={
                "name": "Extra vision check",
                "default_interval_hours": 14.0,
                "interval_type": "days",
                "action": "motion_precision",
                "printer_ids": [h2s.id, x1c.id],
            },
        )
        assert response.status_code == 400, response.text
        assert "X1C" in response.json()["detail"]
        # Nothing half-created.
        assert "Extra vision check" not in await _types(async_client)

    async def test_the_vision_action_limits_the_coverage_of_a_custom_type(self, async_client, printer_factory):
        h2s = await printer_factory(name="H2S", model="H2S")
        await printer_factory(name="X1C", model="X1C")
        response = await async_client.post(
            "/api/v1/maintenance/types",
            json={
                "name": "Extra vision check",
                "default_interval_hours": 14.0,
                "interval_type": "days",
                "action": "motion_precision",
                "printer_ids": [h2s.id],
            },
        )
        assert response.status_code == 200, response.text

        extra = (await _types(async_client))["Extra vision check"]
        assert extra["eligible_count"] == 1
        assert extra["printer_count"] == 1

    async def test_an_unknown_action_is_rejected(self, async_client):
        response = await async_client.post(
            "/api/v1/maintenance/types",
            json={"name": "Nonsense", "default_interval_hours": 10.0, "action": "make_coffee"},
        )
        assert response.status_code == 422

    async def test_the_action_cannot_be_changed_afterwards(self, async_client):
        created = await async_client.post(
            "/api/v1/maintenance/types",
            json={"name": "Fixed action", "default_interval_hours": 10.0, "action": "calibration"},
        )
        assert created.status_code == 200, created.text
        type_id = created.json()["id"]

        response = await async_client.patch(f"/api/v1/maintenance/types/{type_id}", json={"action": None})
        assert response.status_code == 200
        assert (await _types(async_client))["Fixed action"]["action"] == "calibration"

    async def test_a_reminder_type_still_reaches_every_printer(self, async_client, printer_factory):
        h2s = await printer_factory(name="H2S", model="H2S")
        x1c = await printer_factory(name="X1C", model="X1C")
        response = await async_client.post(
            "/api/v1/maintenance/types",
            json={"name": "Wipe the cabinet", "default_interval_hours": 30.0, "printer_ids": [h2s.id, x1c.id]},
        )
        assert response.status_code == 200, response.text
        assert response.json()["action"] is None
        assert response.json()["eligible_count"] == 2
        assert response.json()["printer_count"] == 2

    async def test_an_unknown_printer_id_is_a_404(self, async_client):
        response = await async_client.post(
            "/api/v1/maintenance/types",
            json={"name": "Nowhere", "default_interval_hours": 10.0, "printer_ids": [987654]},
        )
        assert response.status_code == 404
