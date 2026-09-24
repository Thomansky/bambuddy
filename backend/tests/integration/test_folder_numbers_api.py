"""The running number starts on the order folder and the project inherits it.

The owner files an enquiry as a folder with a number; only when the order is
placed does a project come out of that folder. Every test here pins one half of
that: the number is handed out once on the folder, and it is the *same* number
the project ends up carrying. A project that drew a fresh one at that point
would leave the quote and the invoice pointing at different jobs.
"""

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.models.library import LibraryFolder
from backend.app.models.number_series import NumberSeries
from backend.app.models.project import Project
from backend.app.services.number_series import SERIES_LIBRARY_FOLDER, SERIES_PROJECT

pytestmark = pytest.mark.integration


async def _seed_series(db: AsyncSession, key: str, **values) -> NumberSeries:
    """Create (or update) one series row. The app seeds these at startup; tests
    build them directly so they don't depend on init_db having run."""
    series = (await db.execute(select(NumberSeries).where(NumberSeries.key == key))).scalar_one_or_none()
    if series is None:
        series = NumberSeries(key=key)
        db.add(series)
    series.enabled = values.get("enabled", True)
    series.prefix = values.get("prefix", "")
    series.suffix = values.get("suffix", "")
    series.next_value = values.get("next_value", 1)
    series.padding = values.get("padding", 0)
    await db.commit()
    await db.refresh(series)
    return series


async def _next_value(db: AsyncSession, key: str) -> int:
    return (await db.execute(select(NumberSeries.next_value).where(NumberSeries.key == key))).scalar_one()


async def _folder(db: AsyncSession, **values) -> LibraryFolder:
    folder = LibraryFolder(name=values.pop("name", "Order"), **values)
    db.add(folder)
    await db.commit()
    await db.refresh(folder)
    return folder


async def _project_row(db: AsyncSession, project_id: int) -> Project:
    db.expire_all()
    return (await db.execute(select(Project).where(Project.id == project_id))).scalar_one()


async def _folder_row(db: AsyncSession, folder_id: int) -> LibraryFolder:
    db.expire_all()
    return (await db.execute(select(LibraryFolder).where(LibraryFolder.id == folder_id))).scalar_one()


class TestFolderAllocation:
    @pytest.mark.asyncio
    async def test_a_folder_created_with_the_flag_gets_the_next_number(self, async_client: AsyncClient, db_session):
        await _seed_series(db_session, SERIES_LIBRARY_FOLDER, prefix="A-", padding=4, next_value=7)

        response = await async_client.post(
            "/api/v1/library/folders", json={"name": "Reindl bracket", "use_number_series": True}
        )

        assert response.status_code == 200
        assert response.json()["number"] == "A-0007"
        assert await _next_value(db_session, SERIES_LIBRARY_FOLDER) == 8

    @pytest.mark.asyncio
    async def test_without_the_flag_a_folder_stays_unnumbered(self, async_client: AsyncClient, db_session):
        await _seed_series(db_session, SERIES_LIBRARY_FOLDER, next_value=7)

        response = await async_client.post("/api/v1/library/folders", json={"name": "Scratch"})

        assert response.status_code == 200
        assert response.json()["number"] is None
        assert await _next_value(db_session, SERIES_LIBRARY_FOLDER) == 7

    @pytest.mark.asyncio
    async def test_a_disabled_series_hands_out_nothing_and_the_flag_is_ignored(
        self, async_client: AsyncClient, db_session
    ):
        await _seed_series(db_session, SERIES_LIBRARY_FOLDER, enabled=False, next_value=7)

        response = await async_client.post(
            "/api/v1/library/folders", json={"name": "Enquiry", "use_number_series": True}
        )

        assert response.status_code == 200
        assert response.json()["number"] is None
        assert await _next_value(db_session, SERIES_LIBRARY_FOLDER) == 7

    @pytest.mark.asyncio
    async def test_a_number_the_caller_typed_wins_and_the_counter_stays_put(
        self, async_client: AsyncClient, db_session
    ):
        await _seed_series(db_session, SERIES_LIBRARY_FOLDER, prefix="A-", padding=4, next_value=7)

        response = await async_client.post(
            "/api/v1/library/folders",
            json={"name": "Legacy job", "number": "2024-118", "use_number_series": True},
        )

        assert response.status_code == 200
        assert response.json()["number"] == "2024-118"
        assert await _next_value(db_session, SERIES_LIBRARY_FOLDER) == 7

    @pytest.mark.asyncio
    async def test_a_duplicate_number_answers_409_not_500(self, async_client: AsyncClient):
        first = await async_client.post("/api/v1/library/folders", json={"name": "First", "number": "A-1"})
        assert first.status_code == 200

        clash = await async_client.post("/api/v1/library/folders", json={"name": "Second", "number": "A-1"})

        assert clash.status_code == 409
        assert "A-1" in clash.json()["detail"]

    @pytest.mark.asyncio
    async def test_a_create_that_fails_after_the_number_was_taken_gives_it_back(
        self, async_client: AsyncClient, db_session, monkeypatch
    ):
        """The counter advance rides in the create's own transaction.

        Forced at the flush because that is the real failure: the unique index
        is what actually guards the column, and two creates that both passed
        the pre-check meet there.
        """
        from fastapi import HTTPException

        await _seed_series(db_session, SERIES_LIBRARY_FOLDER, prefix="A-", padding=4, next_value=7)

        async def _the_index_says_no(db, number):
            raise HTTPException(status_code=409, detail=f"Folder number '{number}' is already in use")

        monkeypatch.setattr("backend.app.api.routes.library._flush_folder_number", _the_index_says_no)

        rejected = await async_client.post("/api/v1/library/folders", json={"name": "Loser", "use_number_series": True})

        assert rejected.status_code == 409
        assert "A-0007" in rejected.json()["detail"], "the create got as far as taking a number"
        monkeypatch.undo()
        assert await _next_value(db_session, SERIES_LIBRARY_FOLDER) == 7
        # And the number that was almost handed out is still the next one.
        created = await async_client.post("/api/v1/library/folders", json={"name": "Next", "use_number_series": True})
        assert created.json()["number"] == "A-0007"

    @pytest.mark.asyncio
    async def test_a_create_rejected_before_the_allocator_never_reaches_the_counter(
        self, async_client: AsyncClient, db_session
    ):
        await _seed_series(db_session, SERIES_LIBRARY_FOLDER, next_value=7)

        rejected = await async_client.post(
            "/api/v1/library/folders", json={"name": "Orphan", "parent_id": 999_999, "use_number_series": True}
        )

        assert rejected.status_code == 404
        assert await _next_value(db_session, SERIES_LIBRARY_FOLDER) == 7

    @pytest.mark.asyncio
    async def test_a_folder_that_is_only_a_number_is_accepted(self, async_client: AsyncClient, db_session):
        """The normal case for this workflow: the order has a number before it
        has a name."""
        await _seed_series(db_session, SERIES_LIBRARY_FOLDER, prefix="A-", padding=4, next_value=7)

        response = await async_client.post("/api/v1/library/folders", json={"name": "", "use_number_series": True})

        assert response.status_code == 200
        assert response.json()["number"] == "A-0007"
        assert response.json()["name"] == ""

    @pytest.mark.asyncio
    async def test_a_folder_with_neither_a_name_nor_a_number_is_refused(self, async_client: AsyncClient, db_session):
        await _seed_series(db_session, SERIES_LIBRARY_FOLDER, next_value=7)

        response = await async_client.post("/api/v1/library/folders", json={"name": "   "})

        assert response.status_code == 400
        assert await _next_value(db_session, SERIES_LIBRARY_FOLDER) == 7

    @pytest.mark.asyncio
    async def test_nothing_is_renumbered_retroactively(self, async_client: AsyncClient, db_session):
        before = await _folder(db_session, name="From last year")
        await _seed_series(db_session, SERIES_LIBRARY_FOLDER, prefix="A-", padding=4, next_value=7)

        response = await async_client.get("/api/v1/library/folders")

        assert response.status_code == 200
        listed = {item["id"]: item for item in response.json()}
        assert listed[before.id]["number"] is None


class TestFolderNumberEditing:
    @pytest.mark.asyncio
    async def test_the_tree_and_the_detail_response_carry_the_number(self, async_client: AsyncClient, db_session):
        folder = await _folder(db_session, name="Reindl", number="A-0007")

        tree = await async_client.get("/api/v1/library/folders")
        detail = await async_client.get(f"/api/v1/library/folders/{folder.id}")

        assert [item["number"] for item in tree.json() if item["id"] == folder.id] == ["A-0007"]
        assert detail.json()["number"] == "A-0007"

    @pytest.mark.asyncio
    async def test_a_rename_keeps_the_number(self, async_client: AsyncClient, db_session):
        folder = await _folder(db_session, name="Reindl", number="A-0007")

        response = await async_client.put(f"/api/v1/library/folders/{folder.id}", json={"name": "Reindl GmbH"})

        assert response.status_code == 200
        assert response.json()["name"] == "Reindl GmbH"
        assert response.json()["number"] == "A-0007"

    @pytest.mark.asyncio
    async def test_the_number_is_editable_and_can_be_cleared(self, async_client: AsyncClient, db_session):
        folder = await _folder(db_session, name="Reindl", number="A-0007")

        edited = await async_client.put(f"/api/v1/library/folders/{folder.id}", json={"number": "A-0008"})
        assert edited.json()["number"] == "A-0008"

        cleared = await async_client.put(f"/api/v1/library/folders/{folder.id}", json={"number": ""})
        assert cleared.json()["number"] is None

    @pytest.mark.asyncio
    async def test_a_nameless_folder_can_be_renumbered_without_being_given_a_name(
        self, async_client: AsyncClient, db_session
    ):
        """A folder that is only a number is a legitimate row, so editing the
        number on one must not demand a name it never had."""
        folder = await _folder(db_session, name="", number="A-0007")

        response = await async_client.put(f"/api/v1/library/folders/{folder.id}", json={"number": "A-0008"})

        assert response.status_code == 200
        assert response.json()["number"] == "A-0008"
        assert response.json()["name"] == ""

    @pytest.mark.asyncio
    async def test_an_edit_that_would_leave_neither_a_name_nor_a_number_is_refused(
        self, async_client: AsyncClient, db_session
    ):
        """Clearing the number off a nameless folder would make it invisible in
        every view there is — the same pairing the create enforces."""
        folder = await _folder(db_session, name="", number="A-0007")

        response = await async_client.put(f"/api/v1/library/folders/{folder.id}", json={"number": ""})

        assert response.status_code == 400
        assert (await _folder_row(db_session, folder.id)).number == "A-0007"

    @pytest.mark.asyncio
    async def test_a_nameless_folder_can_be_given_a_name_later(self, async_client: AsyncClient, db_session):
        folder = await _folder(db_session, name="", number="A-0007")

        response = await async_client.put(f"/api/v1/library/folders/{folder.id}", json={"name": "Reindl"})

        assert response.status_code == 200
        assert response.json() == {**response.json(), "name": "Reindl", "number": "A-0007"}

    @pytest.mark.asyncio
    async def test_editing_a_folder_onto_a_taken_number_answers_409(self, async_client: AsyncClient, db_session):
        await _folder(db_session, name="First", number="A-0007")
        second = await _folder(db_session, name="Second", number="A-0008")

        response = await async_client.put(f"/api/v1/library/folders/{second.id}", json={"number": "A-0007"})

        assert response.status_code == 409
        assert (await _folder_row(db_session, second.id)).number == "A-0008"


class TestProjectInheritsTheFolderNumber:
    @pytest.mark.asyncio
    async def test_a_project_created_from_a_numbered_folder_carries_that_number(
        self, async_client: AsyncClient, db_session
    ):
        """The point of the whole feature: the order keeps the number it was
        filed under, and the project series does not move."""
        await _seed_series(db_session, SERIES_PROJECT, prefix="P-", padding=4, next_value=31)
        folder = await _folder(db_session, name="Reindl", number="A-0007")

        response = await async_client.post(
            "/api/v1/projects/", json={"name": "Reindl bracket", "library_folder_id": folder.id}
        )

        assert response.status_code == 200
        body = response.json()
        assert body["number"] == "A-0007"
        assert body["number_source"] == "folder"
        assert await _next_value(db_session, SERIES_PROJECT) == 31
        # And the folder the order lived in is now the project's folder.
        assert (await _folder_row(db_session, folder.id)).project_id == body["id"]
        # The folder keeps its own number, so both sides read the same.
        assert (await _folder_row(db_session, folder.id)).number == "A-0007"

    @pytest.mark.asyncio
    async def test_a_folder_number_already_on_another_project_falls_back_to_the_series(
        self, async_client: AsyncClient, db_session
    ):
        """Losing the create over a duplicate identifier would be worse than
        the duplicate, so the project is created with a fresh number and the
        response says which number it got and where from."""
        await _seed_series(db_session, SERIES_PROJECT, prefix="P-", padding=4, next_value=31)
        taken = await async_client.post("/api/v1/projects/", json={"name": "Older job", "number": "A-0007"})
        assert taken.status_code == 200
        folder = await _folder(db_session, name="Reindl", number="A-0007")

        response = await async_client.post(
            "/api/v1/projects/", json={"name": "Reindl bracket", "library_folder_id": folder.id}
        )

        assert response.status_code == 200
        body = response.json()
        assert body["number"] == "P-0031"
        assert body["number_source"] == "series"
        assert await _next_value(db_session, SERIES_PROJECT) == 32

    @pytest.mark.asyncio
    async def test_a_project_from_an_unnumbered_folder_allocates_as_before(self, async_client: AsyncClient, db_session):
        await _seed_series(db_session, SERIES_PROJECT, prefix="P-", padding=4, next_value=31)
        folder = await _folder(db_session, name="Scratch")

        response = await async_client.post(
            "/api/v1/projects/", json={"name": "Whatever", "library_folder_id": folder.id}
        )

        assert response.status_code == 200
        assert response.json()["number"] == "P-0031"
        assert response.json()["number_source"] is None
        assert await _next_value(db_session, SERIES_PROJECT) == 32

    @pytest.mark.asyncio
    async def test_a_number_the_caller_typed_still_wins_over_the_folders(self, async_client: AsyncClient, db_session):
        await _seed_series(db_session, SERIES_PROJECT, prefix="P-", padding=4, next_value=31)
        folder = await _folder(db_session, name="Reindl", number="A-0007")

        response = await async_client.post(
            "/api/v1/projects/",
            json={"name": "Reindl bracket", "number": "RMA-9", "library_folder_id": folder.id},
        )

        assert response.status_code == 200
        assert response.json()["number"] == "RMA-9"
        assert await _next_value(db_session, SERIES_PROJECT) == 31

    @pytest.mark.asyncio
    async def test_an_unknown_folder_is_refused_before_a_number_is_taken(self, async_client: AsyncClient, db_session):
        await _seed_series(db_session, SERIES_PROJECT, prefix="P-", padding=4, next_value=31)

        response = await async_client.post("/api/v1/projects/", json={"name": "Orphan", "library_folder_id": 999_999})

        assert response.status_code == 400
        assert await _next_value(db_session, SERIES_PROJECT) == 31

    @pytest.mark.asyncio
    async def test_linking_a_numbered_project_to_a_numbered_folder_leaves_it_alone(
        self, async_client: AsyncClient, db_session
    ):
        """A number already handed out on paper is not ours to change."""
        project = await async_client.post("/api/v1/projects/", json={"name": "Quoted", "number": "P-0031"})
        project_id = project.json()["id"]
        folder_id = (await _folder(db_session, name="Reindl", number="A-0007")).id

        response = await async_client.put(f"/api/v1/library/folders/{folder_id}", json={"project_id": project_id})

        assert response.status_code == 200
        assert (await _project_row(db_session, project_id)).number == "P-0031"
        assert (await _folder_row(db_session, folder_id)).number == "A-0007"

    @pytest.mark.asyncio
    async def test_linking_an_unnumbered_project_to_a_numbered_folder_hands_the_number_over(
        self, async_client: AsyncClient, db_session
    ):
        await _seed_series(db_session, SERIES_PROJECT, enabled=False)
        project = await async_client.post("/api/v1/projects/", json={"name": "Unnumbered"})
        project_id = project.json()["id"]
        assert project.json()["number"] is None
        folder = await _folder(db_session, name="Reindl", number="A-0007")

        response = await async_client.put(f"/api/v1/library/folders/{folder.id}", json={"project_id": project_id})

        assert response.status_code == 200
        assert (await _project_row(db_session, project_id)).number == "A-0007"

    @pytest.mark.asyncio
    async def test_a_folder_number_already_on_a_project_is_not_handed_over_twice(
        self, async_client: AsyncClient, db_session
    ):
        """The link path has the same duplicate to avoid as the create path,
        and the same answer: the link succeeds, the number is not copied."""
        await _seed_series(db_session, SERIES_PROJECT, enabled=False)
        await async_client.post("/api/v1/projects/", json={"name": "Older job", "number": "A-0007"})
        project = await async_client.post("/api/v1/projects/", json={"name": "Unnumbered"})
        project_id = project.json()["id"]
        folder = await _folder(db_session, name="Reindl", number="A-0007")

        response = await async_client.put(f"/api/v1/library/folders/{folder.id}", json={"project_id": project_id})

        assert response.status_code == 200
        assert (await _project_row(db_session, project_id)).number is None

    @pytest.mark.asyncio
    async def test_a_folder_created_onto_an_unnumbered_project_hands_its_number_over(
        self, async_client: AsyncClient, db_session
    ):
        await _seed_series(db_session, SERIES_PROJECT, enabled=False)
        await _seed_series(db_session, SERIES_LIBRARY_FOLDER, prefix="A-", padding=4, next_value=7)
        project = await async_client.post("/api/v1/projects/", json={"name": "Unnumbered"})
        project_id = project.json()["id"]

        response = await async_client.post(
            "/api/v1/library/folders",
            json={"name": "Reindl", "project_id": project_id, "use_number_series": True},
        )

        assert response.status_code == 200
        assert response.json()["number"] == "A-0007"
        assert (await _project_row(db_session, project_id)).number == "A-0007"

    @pytest.mark.asyncio
    async def test_an_import_onto_a_numbered_folder_carries_that_number(self, async_client: AsyncClient, db_session):
        """The import creates the project and adopts the folder that is already
        there by name — the same handover, reached from the project side."""
        await _folder(db_session, name="Reindl", number="A-0007")

        response = await async_client.post(
            "/api/v1/projects/import",
            json={"name": "Reindl bracket", "linked_folders": [{"name": "Reindl"}]},
        )

        assert response.status_code == 200
        assert response.json()["number"] == "A-0007"
