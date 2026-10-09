"""Creating a project out of a library folder follows #3201's linking rules.

POST /projects with ``library_folder_id`` links the folder to the new project,
just like PUT /library/folders/{id} with ``project_id``. So it asks for the
same right (library:update_all), and a folder the caller cannot see answers
like a missing one instead of being linked by its raw id.
"""

import pytest
from httpx import AsyncClient
from sqlalchemy import func, select

from backend.app.models.library import LibraryFolder
from backend.app.models.project import Project
from backend.tests.integration.test_ownership_permissions import TestOwnershipPermissionsSetup


def _h(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


class TestProjectFolderLink(TestOwnershipPermissionsSetup):
    @pytest.fixture
    async def folder(self, db_session):
        async def _create(**kwargs):
            folder = LibraryFolder(**{"name": "Order", **kwargs})
            db_session.add(folder)
            await db_session.commit()
            await db_session.refresh(folder)
            return folder

        return _create

    @staticmethod
    async def _linked_project(db_session, folder_id: int) -> int | None:
        db_session.expire_all()
        return (
            await db_session.execute(select(LibraryFolder.project_id).where(LibraryFolder.id == folder_id))
        ).scalar_one()

    @staticmethod
    async def _project_count(db_session) -> int:
        return (await db_session.execute(select(func.count(Project.id)))).scalar_one()

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_operator_cannot_link_another_users_private_folder(
        self, async_client: AsyncClient, auth_setup, folder, db_session
    ):
        theirs = await folder(name="Customer X", number="A-0042", created_by_id=auth_setup["operator2_user"]["id"])
        before = await self._project_count(db_session)

        response = await async_client.post(
            "/api/v1/projects/",
            headers=_h(auth_setup["operator_token"]),
            json={"name": "Claimed", "library_folder_id": theirs.id},
        )
        missing = await async_client.post(
            "/api/v1/projects/",
            headers=_h(auth_setup["operator_token"]),
            json={"name": "Claimed", "library_folder_id": 999_999},
        )

        # Indistinguishable from an id that does not exist, so it says nothing
        # about the folder (or its number) either.
        assert response.status_code == 400
        assert (response.status_code, response.json()) == (missing.status_code, missing.json())
        assert await self._linked_project(db_session, theirs.id) is None
        assert await self._project_count(db_session) == before

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_operator_needs_update_all_even_for_folders_they_see(
        self, async_client: AsyncClient, auth_setup, folder, db_session
    ):
        """update_folder refuses linking without library:update_all, own folder or not."""
        mine = await folder(name="Mine", created_by_id=auth_setup["operator_user"]["id"])
        shared = await folder(name="Class", created_by_id=auth_setup["operator2_user"]["id"], shared=True)
        # Plain ids: the expire_all() in _linked_project would otherwise make
        # the next folder's id a lazy load.
        targets = [mine.id, shared.id]
        before = await self._project_count(db_session)

        for folder_id in targets:
            response = await async_client.post(
                "/api/v1/projects/",
                headers=_h(auth_setup["operator_token"]),
                json={"name": "From folder", "library_folder_id": folder_id},
            )
            assert response.status_code == 403
            assert response.json()["detail"] == "Linking folders requires library:update_all"
            assert await self._linked_project(db_session, folder_id) is None

        assert await self._project_count(db_session) == before

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_operator_still_creates_projects_without_a_folder(self, async_client: AsyncClient, auth_setup):
        response = await async_client.post(
            "/api/v1/projects/", headers=_h(auth_setup["operator_token"]), json={"name": "Plain"}
        )

        assert response.status_code == 200

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_admin_links_any_users_private_folder(
        self, async_client: AsyncClient, auth_setup, folder, db_session
    ):
        theirs = await folder(name="Customer X", number="A-0042", created_by_id=auth_setup["operator2_user"]["id"])

        response = await async_client.post(
            "/api/v1/projects/",
            headers=_h(auth_setup["admin_token"]),
            json={"name": "Customer X order", "library_folder_id": theirs.id},
        )

        assert response.status_code == 200
        body = response.json()
        assert body["number"] == "A-0042"
        assert body["number_source"] == "folder"
        assert await self._linked_project(db_session, theirs.id) == body["id"]
