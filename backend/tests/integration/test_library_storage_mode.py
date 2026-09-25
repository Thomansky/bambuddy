"""The library living in a real directory tree (#3160).

The point of the mode is that a folder is a directory and a file keeps its
name, so almost every assertion here is about the filesystem rather than about
a response body: the share is what the owner opens in Explorer, and a row that
says the right thing about a directory nobody made is exactly the failure this
feature has to not have.
"""

import pytest
from httpx import AsyncClient
from sqlalchemy import select

from backend.app.models.library import LibraryFile, LibraryFolder
from backend.app.models.settings import Settings
from backend.app.services import library_storage

pytestmark = pytest.mark.integration


@pytest.fixture(autouse=True)
def _permit_tmp_as_external_root(monkeypatch, tmp_path):
    """The tree lives under pytest's tmp_path, which is not an allowlisted root.

    Directory mode does not go through the external-folder mount allowlist --
    it is configured by the operator in settings, not mounted by a user -- but
    the reserved-roots check shares its helper, so the env var keeps the two
    consistent with what the external-folder tests already do.
    """
    monkeypatch.setenv("BAMBUDDY_EXTERNAL_ROOTS", str(tmp_path.parent))


@pytest.fixture
def tree(tmp_path):
    """The directory the library is pointed at."""
    root = tmp_path / "nas" / "Bambuddy"
    root.mkdir(parents=True)
    return root


async def _set(db_session, key: str, value: str) -> None:
    existing = (await db_session.execute(select(Settings).where(Settings.key == key))).scalar_one_or_none()
    if existing is None:
        db_session.add(Settings(key=key, value=value))
    else:
        existing.value = value
    await db_session.commit()


async def _directory_mode(db_session, tree) -> None:
    await _set(db_session, "library_storage_mode", "directory")
    await _set(db_session, "library_storage_path", str(tree))


class TestTheMode:
    @pytest.mark.asyncio
    async def test_managed_is_the_default(self, async_client: AsyncClient):
        response = await async_client.get("/api/v1/settings/")
        assert response.status_code == 200
        assert response.json()["library_storage_mode"] == "managed"
        assert response.json()["library_storage_path"] == ""

    @pytest.mark.asyncio
    async def test_switching_without_a_path_is_refused(self, async_client: AsyncClient):
        response = await async_client.put("/api/v1/settings/", json={"library_storage_mode": "directory"})
        assert response.status_code == 400
        assert "is not set" in response.json()["detail"]

    @pytest.mark.asyncio
    async def test_a_missing_path_names_itself(self, async_client: AsyncClient, tmp_path):
        response = await async_client.put(
            "/api/v1/settings/",
            json={"library_storage_mode": "directory", "library_storage_path": str(tmp_path / "gone")},
        )
        assert response.status_code == 400
        assert "does not exist" in response.json()["detail"]

    @pytest.mark.asyncio
    async def test_a_file_is_not_a_directory(self, async_client: AsyncClient, tmp_path):
        target = tmp_path / "not-a-dir.txt"
        target.write_text("x")
        response = await async_client.put(
            "/api/v1/settings/",
            json={"library_storage_mode": "directory", "library_storage_path": str(target)},
        )
        assert response.status_code == 400
        assert "is not a directory" in response.json()["detail"]

    @pytest.mark.asyncio
    async def test_a_relative_path_is_refused(self, async_client: AsyncClient):
        response = await async_client.put(
            "/api/v1/settings/",
            json={"library_storage_mode": "directory", "library_storage_path": "share/bambuddy"},
        )
        assert response.status_code == 400
        assert "absolute" in response.json()["detail"]

    @pytest.mark.asyncio
    async def test_a_usable_path_is_accepted_and_moves_nothing(self, async_client: AsyncClient, tree):
        response = await async_client.put(
            "/api/v1/settings/",
            json={"library_storage_mode": "directory", "library_storage_path": str(tree)},
        )
        assert response.status_code == 200
        assert response.json()["library_storage_mode"] == "directory"
        # The switch is not the migration: nothing was created in the tree.
        assert list(tree.iterdir()) == []


class TestFolders:
    @pytest.mark.asyncio
    async def test_create_makes_a_real_directory_and_a_row(self, async_client: AsyncClient, db_session, tree):
        await _directory_mode(db_session, tree)
        response = await async_client.post("/api/v1/library/folders", json={"name": "Kunden"})
        assert response.status_code == 200
        body = response.json()
        assert body["is_external"] is True
        assert body["external_path"] == str(tree / "Kunden")
        assert (tree / "Kunden").is_dir()

    @pytest.mark.asyncio
    async def test_a_child_lands_inside_its_parent(self, async_client: AsyncClient, db_session, tree):
        await _directory_mode(db_session, tree)
        parent = (await async_client.post("/api/v1/library/folders", json={"name": "Kunden"})).json()
        child = await async_client.post("/api/v1/library/folders", json={"name": "RAFI", "parent_id": parent["id"]})
        assert child.status_code == 200
        assert (tree / "Kunden" / "RAFI").is_dir()
        assert child.json()["external_path"] == str(tree / "Kunden" / "RAFI")

    @pytest.mark.asyncio
    async def test_an_existing_directory_is_adopted(self, async_client: AsyncClient, db_session, tree):
        """Somebody made the folder in Explorer first. That is the normal case."""
        await _directory_mode(db_session, tree)
        (tree / "Kunden").mkdir()
        (tree / "Kunden" / "already-there.3mf").write_bytes(b"x")
        response = await async_client.post("/api/v1/library/folders", json={"name": "Kunden"})
        assert response.status_code == 200
        assert response.json()["external_path"] == str(tree / "Kunden")
        # Adopted, not emptied.
        assert (tree / "Kunden" / "already-there.3mf").exists()

    @pytest.mark.asyncio
    async def test_a_file_in_the_way_is_a_conflict(self, async_client: AsyncClient, db_session, tree):
        await _directory_mode(db_session, tree)
        (tree / "Kunden").write_bytes(b"not a directory")
        response = await async_client.post("/api/v1/library/folders", json={"name": "Kunden"})
        assert response.status_code == 409

    @pytest.mark.asyncio
    async def test_a_name_with_separators_cannot_escape(self, async_client: AsyncClient, db_session, tree):
        await _directory_mode(db_session, tree)
        response = await async_client.post("/api/v1/library/folders", json={"name": "../escaped"})
        assert response.status_code == 200
        created = response.json()["external_path"]
        assert str(tree) in created
        assert not (tree.parent / "escaped").exists()

    @pytest.mark.asyncio
    async def test_rename_moves_the_directory_and_every_descendant_path(
        self, async_client: AsyncClient, db_session, tree
    ):
        await _directory_mode(db_session, tree)
        parent = (await async_client.post("/api/v1/library/folders", json={"name": "Kunden"})).json()
        child = (
            await async_client.post("/api/v1/library/folders", json={"name": "RAFI", "parent_id": parent["id"]})
        ).json()

        response = await async_client.put(f"/api/v1/library/folders/{parent['id']}", json={"name": "Auftraege"})
        assert response.status_code == 200
        assert (tree / "Auftraege" / "RAFI").is_dir()
        assert not (tree / "Kunden").exists()

        db_session.expire_all()
        child_row = (
            await db_session.execute(select(LibraryFolder).where(LibraryFolder.id == child["id"]))
        ).scalar_one()
        assert child_row.external_path == str(tree / "Auftraege" / "RAFI")

    @pytest.mark.asyncio
    async def test_a_name_already_on_the_share_is_refused(self, async_client: AsyncClient, db_session, tree):
        await _directory_mode(db_session, tree)
        folder = (await async_client.post("/api/v1/library/folders", json={"name": "Kunden"})).json()
        (tree / "Auftraege").mkdir()

        response = await async_client.put(f"/api/v1/library/folders/{folder['id']}", json={"name": "Auftraege"})
        assert response.status_code == 409
        # The row is untouched: the rename failed before anything was written.
        db_session.expire_all()
        row = (await db_session.execute(select(LibraryFolder).where(LibraryFolder.id == folder["id"]))).scalar_one()
        assert row.name == "Kunden"
        assert (tree / "Kunden").is_dir()

    @pytest.mark.asyncio
    async def test_delete_removes_an_empty_directory(self, async_client: AsyncClient, db_session, tree):
        await _directory_mode(db_session, tree)
        folder = (await async_client.post("/api/v1/library/folders", json={"name": "Leer"})).json()
        response = await async_client.delete(f"/api/v1/library/folders/{folder['id']}")
        assert response.status_code == 200
        assert not (tree / "Leer").exists()

    @pytest.mark.asyncio
    async def test_delete_keeps_a_directory_that_still_holds_files(self, async_client: AsyncClient, db_session, tree):
        await _directory_mode(db_session, tree)
        folder = (await async_client.post("/api/v1/library/folders", json={"name": "Kunden"})).json()
        (tree / "Kunden" / "drawing.3mf").write_bytes(b"x")

        response = await async_client.delete(f"/api/v1/library/folders/{folder['id']}")
        assert response.status_code == 200
        assert response.json()["directory_kept"] == str(tree / "Kunden")
        assert (tree / "Kunden" / "drawing.3mf").exists()


class TestWhereFilesLand:
    @pytest.mark.asyncio
    async def test_an_upload_keeps_its_name_in_the_folder(self, async_client: AsyncClient, db_session, tree):
        await _directory_mode(db_session, tree)
        folder = (await async_client.post("/api/v1/library/folders", json={"name": "Kunden"})).json()

        response = await async_client.post(
            "/api/v1/library/files",
            files={"file": ("bracket.stl", b"solid bracket\nfacet\n", "application/octet-stream")},
            params={"folder_id": folder["id"]},
        )
        assert response.status_code == 200, response.text
        assert (tree / "Kunden" / "bracket.stl").is_file()

        db_session.expire_all()
        row = (await db_session.execute(select(LibraryFile).where(LibraryFile.filename == "bracket.stl"))).scalar_one()
        assert row.is_external is True
        assert row.file_path == str(tree / "Kunden" / "bracket.stl")

    @pytest.mark.asyncio
    async def test_an_upload_with_no_folder_lands_in_the_root(self, async_client: AsyncClient, db_session, tree):
        await _directory_mode(db_session, tree)
        response = await async_client.post(
            "/api/v1/library/files",
            files={"file": ("loose.stl", b"solid loose\nfacet\n", "application/octet-stream")},
        )
        assert response.status_code == 200, response.text
        assert (tree / "loose.stl").is_file()

    @pytest.mark.asyncio
    async def test_managed_mode_is_untouched(self, async_client: AsyncClient, tree):
        """No mode set: the file goes to the flat store, as every install does."""
        response = await async_client.post(
            "/api/v1/library/files",
            files={"file": ("managed.stl", b"solid managed\nfacet\n", "application/octet-stream")},
        )
        assert response.status_code == 200, response.text
        assert not (tree / "managed.stl").exists()


class TestTheMigration:
    async def _managed_file(self, async_client: AsyncClient, filename: str, folder_id: int | None = None):
        params = {"folder_id": folder_id} if folder_id is not None else None
        response = await async_client.post(
            "/api/v1/library/files",
            files={"file": (filename, f"solid {filename}\nfacet\n".encode(), "application/octet-stream")},
            params=params,
        )
        assert response.status_code == 200, response.text
        return response.json()

    @pytest.mark.asyncio
    async def test_the_plan_reports_before_anything_moves(self, async_client: AsyncClient, db_session, tree):
        folder = (await async_client.post("/api/v1/library/folders", json={"name": "Kunden"})).json()
        await self._managed_file(async_client, "part.stl", folder["id"])
        await _set(db_session, "library_storage_path", str(tree))

        response = await async_client.get("/api/v1/library/storage/migration-plan")
        assert response.status_code == 200
        plan = response.json()
        assert plan["file_count"] == 1
        assert plan["blockers"] == []
        assert plan["moves"][0]["target"] == str(tree / "Kunden" / "part.stl")
        # A plan moves nothing.
        assert not (tree / "Kunden").exists()

    @pytest.mark.asyncio
    async def test_a_collision_refuses_the_whole_run(self, async_client: AsyncClient, db_session, tree):
        folder = (await async_client.post("/api/v1/library/folders", json={"name": "Kunden"})).json()
        await self._managed_file(async_client, "part.stl", folder["id"])
        await _set(db_session, "library_storage_path", str(tree))
        (tree / "Kunden").mkdir()
        (tree / "Kunden" / "part.stl").write_bytes(b"somebody else's part")

        response = await async_client.post("/api/v1/library/storage/migrate")
        assert response.status_code == 409
        assert "already exists" in str(response.json()["detail"]["blockers"])
        # Refused whole: the file on the share is the one that was there.
        assert (tree / "Kunden" / "part.stl").read_bytes() == b"somebody else's part"

    @pytest.mark.asyncio
    async def test_a_run_moves_the_library_and_a_second_one_is_a_no_op(
        self, async_client: AsyncClient, db_session, tree
    ):
        folder = (await async_client.post("/api/v1/library/folders", json={"name": "Kunden"})).json()
        created = await self._managed_file(async_client, "part.stl", folder["id"])
        await _set(db_session, "library_storage_path", str(tree))

        first = await async_client.post("/api/v1/library/storage/migrate")
        assert first.status_code == 200, first.text
        assert first.json()["moved"] == 1
        assert (tree / "Kunden" / "part.stl").is_file()

        db_session.expire_all()
        row = (await db_session.execute(select(LibraryFile).where(LibraryFile.id == created["id"]))).scalar_one()
        assert row.is_external is True
        assert row.file_path == str(tree / "Kunden" / "part.stl")
        # The folder is in the tree now, so the next upload goes there too.
        folder_row = (
            await db_session.execute(select(LibraryFolder).where(LibraryFolder.id == folder["id"]))
        ).scalar_one()
        assert folder_row.external_path == str(tree / "Kunden")

        second = await async_client.post("/api/v1/library/storage/migrate")
        assert second.status_code == 200
        assert second.json()["moved"] == 0

    @pytest.mark.asyncio
    async def test_a_folder_with_no_files_still_gets_its_directory(self, async_client: AsyncClient, db_session, tree):
        """Or the next upload into it has nowhere to write."""
        await async_client.post("/api/v1/library/folders", json={"name": "Leer"})
        await _set(db_session, "library_storage_path", str(tree))

        response = await async_client.post("/api/v1/library/storage/migrate")
        assert response.status_code == 200
        assert (tree / "Leer").is_dir()

    @pytest.mark.asyncio
    async def test_without_a_path_there_is_nothing_to_migrate_into(self, async_client: AsyncClient):
        response = await async_client.post("/api/v1/library/storage/migrate")
        assert response.status_code == 400
        assert "is not set" in response.json()["detail"]


class TestThePathGuard:
    def test_a_stored_path_outside_the_root_is_not_trusted(self, tmp_path):
        """The column is a string. A restored backup can name anything."""
        root = tmp_path / "tree"
        root.mkdir()
        assert library_storage.is_inside_tree(root, root / "Kunden") is True
        assert library_storage.is_inside_tree(root, tmp_path / "elsewhere") is False
        assert library_storage.is_inside_tree(None, root / "Kunden") is False

    def test_a_component_is_reduced_to_one_directory_name(self):
        assert library_storage.directory_component("a/b", None) == "a-b"
        assert library_storage.directory_component("..", None, fallback_id=7) == "folder-7"
        assert library_storage.directory_component("", "4021") == "4021"
