"""The library living in a real directory tree (#3160).

The point of the mode is that a folder is a directory and a file keeps its
name, so almost every assertion here is about the filesystem rather than about
a response body: the share is what the owner opens in Explorer, and a row that
says the right thing about a directory nobody made is exactly the failure this
feature has to not have.
"""

import os
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from httpx import AsyncClient
from sqlalchemy import select

from backend.app.models.library import LibraryFile, LibraryFolder
from backend.app.models.settings import Settings
from backend.app.services import library_storage

pytestmark = pytest.mark.integration

TRASH = library_storage.TRASH_DIR_NAME


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


async def _row(db_session, file_id: int) -> LibraryFile | None:
    """The file's row as the database has it now, not as this session cached it."""
    db_session.expire_all()
    return (await db_session.execute(select(LibraryFile).where(LibraryFile.id == file_id))).scalar_one_or_none()


def _walk(nodes):
    for node in nodes:
        yield node
        yield from _walk(node.get("children") or [])


def _trash_entries(tree: Path) -> list[Path]:
    """The entries of the tree's trash; none when it was never made."""
    trash = tree / TRASH
    return sorted(trash.iterdir()) if trash.is_dir() else []


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
    async def test_a_numbered_folder_carries_its_number_in_front_on_the_share(
        self, async_client: AsyncClient, db_session, tree
    ):
        """Explorer shows the folder the way Bambuddy does: "001 EBZ"."""
        await _directory_mode(db_session, tree)
        response = await async_client.post("/api/v1/library/folders", json={"name": "EBZ", "number": "001"})
        assert response.status_code == 200, response.text
        body = response.json()
        assert (body["name"], body["number"]) == ("EBZ", "001")
        assert body["external_path"] == str(tree / "001 EBZ")
        assert (tree / "001 EBZ").is_dir()

    @pytest.mark.asyncio
    async def test_numbering_a_folder_renames_its_directory_and_everything_below(
        self, async_client: AsyncClient, db_session, tree
    ):
        await _directory_mode(db_session, tree)
        parent = (await async_client.post("/api/v1/library/folders", json={"name": "EBZ"})).json()
        child = (
            await async_client.post("/api/v1/library/folders", json={"name": "7200090094", "parent_id": parent["id"]})
        ).json()

        numbered = await async_client.put(f"/api/v1/library/folders/{parent['id']}", json={"number": "001"})
        assert numbered.status_code == 200, numbered.text
        child_numbered = await async_client.put(f"/api/v1/library/folders/{child['id']}", json={"number": "4024"})
        assert child_numbered.status_code == 200, child_numbered.text

        assert (tree / "001 EBZ" / "4024 7200090094").is_dir()
        assert not (tree / "EBZ").exists()
        db_session.expire_all()
        child_row = (
            await db_session.execute(select(LibraryFolder).where(LibraryFolder.id == child["id"]))
        ).scalar_one()
        assert child_row.external_path == str(tree / "001 EBZ" / "4024 7200090094")
        assert (child_row.name, child_row.number) == ("7200090094", "4024")

    @pytest.mark.asyncio
    async def test_a_new_folder_cannot_take_over_a_numbered_folders_directory(
        self, async_client: AsyncClient, db_session, tree
    ):
        """ "001 EBZ" next to EBZ filed under 001 would share its directory."""
        await _directory_mode(db_session, tree)
        await async_client.post("/api/v1/library/folders", json={"name": "EBZ", "number": "001"})

        response = await async_client.post("/api/v1/library/folders", json={"name": "001 EBZ"})

        assert response.status_code == 409, response.text
        rows = (await db_session.execute(select(LibraryFolder))).scalars().all()
        assert [(r.name, r.number) for r in rows] == [("EBZ", "001")]

    @pytest.mark.asyncio
    async def test_the_claim_on_a_directory_ignores_case_like_the_share(
        self, async_client: AsyncClient, db_session, tree
    ):
        """On the SMB share "001 ebz" is the same directory as "001 EBZ"."""
        await _directory_mode(db_session, tree)
        await async_client.post("/api/v1/library/folders", json={"name": "EBZ", "number": "001"})

        response = await async_client.post("/api/v1/library/folders", json={"name": "001 ebz"})

        assert response.status_code == 409, response.text

    @pytest.mark.asyncio
    async def test_a_zip_of_a_numbered_folder_unpacks_into_that_folder(
        self, async_client: AsyncClient, db_session, tree
    ):
        """The share's "001 EBZ", zipped in Explorer and uploaded at the top."""
        import io
        import zipfile

        await _directory_mode(db_session, tree)
        ebz = (await async_client.post("/api/v1/library/folders", json={"name": "EBZ", "number": "001"})).json()
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr("001 EBZ/neu.stl", b"solid neu\n")
        buf.seek(0)

        response = await async_client.post(
            "/api/v1/library/files/extract-zip?preserve_structure=true",
            files={"file": ("ebz.zip", buf, "application/zip")},
        )

        assert response.status_code == 200, response.text
        assert response.json()["folders_created"] == 0
        assert (tree / "001 EBZ" / "neu.stl").is_file()
        listed = (await async_client.get(f"/api/v1/library/files?folder_id={ebz['id']}")).json()
        assert [f["filename"] for f in listed] == ["neu.stl"]

    @pytest.mark.asyncio
    async def test_linking_a_project_leaves_the_directory_alone(self, async_client: AsyncClient, db_session, tree):
        """Only name, number and place move a folder on the share.

        A folder numbered while the number stayed off the share ("EBZ" under
        001) is renamed by the one-time alignment; a project link must not be
        refused because that rename cannot happen right now.
        """
        await _directory_mode(db_session, tree)
        folder = (await async_client.post("/api/v1/library/folders", json={"name": "EBZ"})).json()
        db_session.expire_all()
        row = (await db_session.execute(select(LibraryFolder).where(LibraryFolder.id == folder["id"]))).scalar_one()
        row.number = "001"
        await db_session.commit()
        (tree / "001 EBZ").mkdir()  # the rename would be refused
        project = (await async_client.post("/api/v1/projects/", json={"name": "RAFI Group"})).json()

        response = await async_client.put(f"/api/v1/library/folders/{folder['id']}", json={"project_id": project["id"]})

        assert response.status_code == 200, response.text
        assert response.json()["external_path"] == str(tree / "EBZ")
        assert (tree / "EBZ").is_dir()

    @pytest.mark.asyncio
    async def test_taking_the_number_away_takes_it_off_the_directory(self, async_client: AsyncClient, db_session, tree):
        await _directory_mode(db_session, tree)
        folder = (await async_client.post("/api/v1/library/folders", json={"name": "RAFI", "number": "002"})).json()

        response = await async_client.put(f"/api/v1/library/folders/{folder['id']}", json={"number": None})

        assert response.status_code == 200, response.text
        assert (tree / "RAFI").is_dir()
        assert not (tree / "002 RAFI").exists()

    @pytest.mark.asyncio
    async def test_a_name_that_already_starts_with_its_number_keeps_it_once(
        self, async_client: AsyncClient, db_session, tree
    ):
        """Made in Explorer as "4026 Gehäuse", numbered in Bambuddy afterwards."""
        await _directory_mode(db_session, tree)
        folder = (await async_client.post("/api/v1/library/folders", json={"name": "4026 Gehäuse"})).json()

        response = await async_client.put(f"/api/v1/library/folders/{folder['id']}", json={"number": "4026"})

        assert response.status_code == 200, response.text
        assert (tree / "4026 Gehäuse").is_dir()
        assert response.json()["external_path"] == str(tree / "4026 Gehäuse")

    @pytest.mark.asyncio
    async def test_delete_removes_an_empty_directory(self, async_client: AsyncClient, db_session, tree):
        await _directory_mode(db_session, tree)
        folder = (await async_client.post("/api/v1/library/folders", json={"name": "Leer"})).json()
        response = await async_client.delete(f"/api/v1/library/folders/{folder['id']}")
        assert response.status_code == 200
        assert not (tree / "Leer").exists()
        # Nothing in it to restore, so nothing of it waits in the trash either.
        assert _trash_entries(tree) == []

    @pytest.mark.asyncio
    async def test_delete_moves_a_directory_that_still_holds_files_into_the_trash(
        self, async_client: AsyncClient, db_session, tree
    ):
        """Left on the share, the next scan would hand the whole folder back.

        A file nobody scanned yet is somebody's file all the same: it goes to
        the trash with its folder, never away.
        """
        await _directory_mode(db_session, tree)
        folder = (await async_client.post("/api/v1/library/folders", json={"name": "Kunden"})).json()
        (tree / "Kunden" / "drawing.3mf").write_bytes(b"x")

        response = await async_client.delete(f"/api/v1/library/folders/{folder['id']}")
        assert response.status_code == 200
        assert "directory_kept" not in response.json()
        assert not (tree / "Kunden").exists()
        [entry] = _trash_entries(tree)
        assert (entry / "Kunden" / "drawing.3mf").read_bytes() == b"x"


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
    async def test_a_second_run_leaves_a_numbered_folder_where_it_is(self, async_client: AsyncClient, db_session, tree):
        """Numbered after the first run, its directory still "EBZ": no empty "001 EBZ"."""
        folder = (await async_client.post("/api/v1/library/folders", json={"name": "EBZ"})).json()
        await self._managed_file(async_client, "part.stl", folder["id"])
        await _set(db_session, "library_storage_path", str(tree))
        assert (await async_client.post("/api/v1/library/storage/migrate")).status_code == 200
        db_session.expire_all()
        row = (await db_session.execute(select(LibraryFolder).where(LibraryFolder.id == folder["id"]))).scalar_one()
        row.number = "001"
        await db_session.commit()
        # Something new in the managed store, which is what a second run is for.
        await async_client.post("/api/v1/library/folders", json={"name": "Neu"})

        second = await async_client.post("/api/v1/library/storage/migrate")

        assert second.status_code == 200, second.text
        assert not (tree / "001 EBZ").exists()
        assert (tree / "EBZ" / "part.stl").is_file()
        assert (tree / "Neu").is_dir()
        db_session.expire_all()
        row = (await db_session.execute(select(LibraryFolder).where(LibraryFolder.id == folder["id"]))).scalar_one()
        assert row.external_path == str(tree / "EBZ")

    @pytest.mark.asyncio
    async def test_a_folder_with_no_files_still_gets_its_directory(self, async_client: AsyncClient, db_session, tree):
        """Or the next upload into it has nowhere to write."""
        await async_client.post("/api/v1/library/folders", json={"name": "Leer"})
        await _set(db_session, "library_storage_path", str(tree))

        response = await async_client.post("/api/v1/library/storage/migrate")
        assert response.status_code == 200
        assert (tree / "Leer").is_dir()

    @pytest.mark.asyncio
    async def test_a_trashed_file_stays_in_the_managed_store(self, async_client: AsyncClient, db_session, tree):
        """Or throwing the old copy away would not answer a name collision.

        A file usually lands in the trash *because* it was replaced, so letting
        it claim the name in the tree would block the live file behind it.
        """
        folder = (await async_client.post("/api/v1/library/folders", json={"name": "Kunden"})).json()
        doomed = await self._managed_file(async_client, "old.stl", folder["id"])
        assert (await async_client.delete(f"/api/v1/library/files/{doomed['id']}")).status_code == 200
        await self._managed_file(async_client, "part.stl", folder["id"])
        await _set(db_session, "library_storage_path", str(tree))

        plan = (await async_client.get("/api/v1/library/storage/migration-plan")).json()
        assert [move["filename"] for move in plan["moves"]] == ["part.stl"]

        assert (await async_client.post("/api/v1/library/storage/migrate")).status_code == 200
        assert (tree / "Kunden" / "part.stl").is_file()
        assert not (tree / "Kunden" / "old.stl").exists()

    @pytest.mark.asyncio
    async def test_a_collision_names_its_parts_for_the_ui(self, async_client: AsyncClient, db_session, tree):
        """The UI renders the names in the user's language, not this sentence."""
        folder = (await async_client.post("/api/v1/library/folders", json={"name": "Kunden"})).json()
        await self._managed_file(async_client, "part.stl", folder["id"])
        await _set(db_session, "library_storage_path", str(tree))
        (tree / "Kunden").mkdir()
        (tree / "Kunden" / "part.stl").write_bytes(b"already here")

        plan = (await async_client.get("/api/v1/library/storage/migration-plan")).json()
        # The sentence stays a string, so a browser still on the previous build
        # renders it instead of crashing on an object it cannot print.
        assert isinstance(plan["blockers"][0], str)
        blocker = plan["blocker_details"][0]
        assert blocker["kind"] == "exists"
        assert blocker["target"] == str(tree / "Kunden" / "part.stl")
        assert blocker["names"][0].startswith("part.stl")
        assert "already exists" in blocker["message"]

    @pytest.mark.asyncio
    async def test_the_storage_breakdown_counts_the_tree(self, async_client: AsyncClient, db_session, tree):
        """Files on the share are still library files, wherever they are."""
        await _directory_mode(db_session, tree)
        (tree / "Kunden").mkdir()
        (tree / "Kunden" / "part.stl").write_bytes(b"x" * 4096)

        usage = (await async_client.get("/api/v1/system/storage-usage?refresh=true")).json()
        assert usage["library_tree"] == str(tree)
        by_key = {category["key"]: category["bytes"] for category in usage["categories"]}
        assert by_key.get("library_files", 0) >= 4096
        assert usage["library_tree_disk"]["total_bytes"] > 0

    @pytest.mark.asyncio
    async def test_without_a_path_there_is_nothing_to_migrate_into(self, async_client: AsyncClient):
        response = await async_client.post("/api/v1/library/storage/migrate")
        assert response.status_code == 400
        assert "is not set" in response.json()["detail"]


class TestWhatTheShareBringsWithIt:
    @pytest.mark.asyncio
    async def test_a_scan_ignores_the_nas_system_directories(self, async_client: AsyncClient, tree):
        """@Recycle is where the NAS keeps what somebody deleted.

        It is not hidden by name, so the "show hidden" setting does not cover
        it, and indexing it would hand every deleted print back as a folder.
        """
        (tree / "@Recycle").mkdir()
        (tree / "@Recycle" / "geloescht.3mf").write_bytes(b"deleted once")
        (tree / "@Recently-Snapshot").mkdir()
        (tree / "Auftrag").mkdir()
        (tree / "Auftrag" / "teil.3mf").write_bytes(b"a real file")

        created = await async_client.post(
            "/api/v1/library/folders/external",
            json={"name": "Freigabe", "external_path": str(tree), "readonly": False, "show_hidden": True},
        )
        assert created.status_code == 200, created.text

        scan = await async_client.post(f"/api/v1/library/folders/{created.json()['id']}/scan")
        assert scan.status_code == 200, scan.text

        folders = (await async_client.get("/api/v1/library/folders")).json()

        def walk(nodes):
            for node in nodes:
                yield node
                yield from walk(node.get("children") or [])

        by_name = {node["name"]: node for node in walk(folders)}
        assert "Auftrag" in by_name
        assert not {"@Recycle", "@Recently-Snapshot"} & set(by_name)

        # The real file is indexed; the deleted one inside @Recycle is not.
        listed = (await async_client.get(f"/api/v1/library/files?folder_id={by_name['Auftrag']['id']}")).json()
        assert [f["filename"] for f in listed] == ["teil.3mf"]


class TestKeepingUpWithTheShare:
    """The auto-scan: the same reconciliation, without six buttons (#3160)."""

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
    async def test_a_file_dropped_in_by_hand_is_found(self, async_client: AsyncClient, db_session, tree):
        from backend.app.services.library_autoscan import autoscan_once

        await _directory_mode(db_session, tree)
        folder = (await async_client.post("/api/v1/library/folders", json={"name": "Kunden"})).json()
        (tree / "Kunden" / "von-hand.3mf").write_bytes(b"dropped in via Explorer")

        result = await autoscan_once(db_session)
        assert result["skipped"] is None
        assert result["added"] == 1

        listed = (await async_client.get(f"/api/v1/library/files?folder_id={folder['id']}")).json()
        assert [f["filename"] for f in listed] == ["von-hand.3mf"]

    @pytest.mark.asyncio
    async def test_managed_mode_has_nothing_to_reconcile(self, async_client: AsyncClient, db_session):
        from backend.app.services.library_autoscan import autoscan_once

        result = await autoscan_once(db_session)
        assert result["scanned"] == 0
        assert result["skipped"] == "not a directory library"

    @pytest.mark.asyncio
    async def test_an_unreachable_share_is_skipped_not_an_error(self, async_client: AsyncClient, db_session, tmp_path):
        """A NAS rebooting must not fill the log with tracebacks every minute."""
        from backend.app.services.library_autoscan import autoscan_once

        gone = tmp_path / "unterwegs"
        gone.mkdir()
        await _directory_mode(db_session, gone)
        gone.rmdir()

        result = await autoscan_once(db_session)
        assert result["scanned"] == 0
        assert "does not exist" in result["skipped"]

    @pytest.mark.asyncio
    async def test_an_empty_tree_is_never_reconciled(self, async_client: AsyncClient, db_session, tree):
        """A share that dropped off looks exactly like "everything was deleted".

        The bind-mount target stays behind as an empty directory, and a scan of
        that would remove every row — with the tags, the project links and the
        print history — while the files sit on a NAS nobody can reach.
        """
        from backend.app.services.library_autoscan import autoscan_once

        await _directory_mode(db_session, tree)
        folder = (await async_client.post("/api/v1/library/folders", json={"name": "Kunden"})).json()
        await self._managed_file(async_client, "teil.stl", folder["id"])
        # The share goes away: the mount point is left, empty.
        for child in sorted(tree.rglob("*"), key=lambda p: -len(p.parts)):
            child.rmdir() if child.is_dir() else child.unlink()

        result = await autoscan_once(db_session)
        assert result["skipped"] == "the library directory is empty"
        assert result["removed"] == 0
        listed = (await async_client.get(f"/api/v1/library/files?folder_id={folder['id']}")).json()
        assert [f["filename"] for f in listed] == ["teil.stl"]

    @pytest.mark.asyncio
    async def test_the_endpoint_scans_the_whole_tree(self, async_client: AsyncClient, db_session, tree):
        await _directory_mode(db_session, tree)
        await async_client.post("/api/v1/library/folders", json={"name": "Kunden"})
        await async_client.post("/api/v1/library/folders", json={"name": "Intern"})
        (tree / "Kunden" / "eins.3mf").write_bytes(b"one")
        (tree / "Intern" / "zwei.3mf").write_bytes(b"two")

        response = await async_client.post("/api/v1/library/storage/scan")
        assert response.status_code == 200, response.text
        assert response.json()["added"] == 2

    @pytest.mark.asyncio
    async def test_a_numbered_folder_renamed_in_explorer_to_its_number_and_name_is_found_again(
        self, async_client: AsyncClient, db_session, tree
    ):
        """Renamed by hand from "7200090094" to "4024 7200090094": the same folder.

        Not a second folder beside it, and not deleted as gone because its old
        directory no longer exists — even while it is still empty — and the
        row now names the directory where it is, so the next subfolder made
        in it lands there instead of recreating the old one.
        """
        from backend.app.services.library_autoscan import autoscan_once

        await _directory_mode(db_session, tree)
        parent = (await async_client.post("/api/v1/library/folders", json={"name": "EBZ"})).json()
        child = (
            await async_client.post("/api/v1/library/folders", json={"name": "7200090094", "parent_id": parent["id"]})
        ).json()
        # Numbered while the directory was left alone — as the rows look on a
        # library numbered before the number went onto the share.
        db_session.expire_all()
        row = (await db_session.execute(select(LibraryFolder).where(LibraryFolder.id == child["id"]))).scalar_one()
        row.number = "4024"
        await db_session.commit()
        os.rename(tree / "EBZ" / "7200090094", tree / "EBZ" / "4024 7200090094")

        result = await autoscan_once(db_session)
        assert result["skipped"] is None

        db_session.expire_all()
        children = (
            (await db_session.execute(select(LibraryFolder).where(LibraryFolder.parent_id == parent["id"])))
            .scalars()
            .all()
        )
        assert [(c.id, c.name, c.number) for c in children] == [(child["id"], "7200090094", "4024")]
        assert children[0].external_path == str(tree / "EBZ" / "4024 7200090094")

        sub = await async_client.post(
            "/api/v1/library/folders", json={"name": "B.70925843 - 10", "parent_id": child["id"]}
        )
        assert sub.status_code == 200, sub.text
        assert (tree / "EBZ" / "4024 7200090094" / "B.70925843 - 10").is_dir()
        assert not (tree / "EBZ" / "7200090094").exists()

    @pytest.mark.asyncio
    async def test_files_keep_their_rows_when_their_folder_is_renamed_in_explorer(
        self, async_client: AsyncClient, db_session, tree
    ):
        """Same ids, so tags, photos and print history stay with the files."""
        from backend.app.services.library_autoscan import autoscan_once

        await _directory_mode(db_session, tree)
        parent = (await async_client.post("/api/v1/library/folders", json={"name": "EBZ"})).json()
        order = (
            await async_client.post(
                "/api/v1/library/folders", json={"name": "7200090094", "number": "4024", "parent_id": parent["id"]}
            )
        ).json()
        inner = (
            await async_client.post("/api/v1/library/folders", json={"name": "Teile", "parent_id": order["id"]})
        ).json()
        (tree / "EBZ" / "4024 7200090094" / "deckel.3mf").write_bytes(b"lid")
        (tree / "EBZ" / "4024 7200090094" / "Teile" / "boden.3mf").write_bytes(b"base")
        assert (await autoscan_once(db_session))["added"] == 2
        db_session.expire_all()
        before = {f.filename: f.id for f in (await db_session.execute(select(LibraryFile))).scalars().all()}
        os.rename(tree / "EBZ" / "4024 7200090094", tree / "EBZ" / "7200090094")

        result = await autoscan_once(db_session)

        assert (result["added"], result["removed"]) == (0, 0)
        db_session.expire_all()
        after = {
            f.filename: (f.id, f.file_path) for f in (await db_session.execute(select(LibraryFile))).scalars().all()
        }
        assert after == {
            "deckel.3mf": (before["deckel.3mf"], str(tree / "EBZ" / "7200090094" / "deckel.3mf")),
            "boden.3mf": (before["boden.3mf"], str(tree / "EBZ" / "7200090094" / "Teile" / "boden.3mf")),
        }
        folders = {f.id: f.external_path for f in (await db_session.execute(select(LibraryFolder))).scalars().all()}
        assert folders[order["id"]] == str(tree / "EBZ" / "7200090094")
        assert folders[inner["id"]] == str(tree / "EBZ" / "7200090094" / "Teile")
        assert len(folders) == 3

    @pytest.mark.asyncio
    async def test_a_new_directory_with_a_numbered_folders_bare_name_is_a_folder_of_its_own(
        self, async_client: AsyncClient, db_session, tree
    ):
        """ "4031 Halter" lives on; "Halter" made in Explorer is another job."""
        from backend.app.services.library_autoscan import autoscan_once

        await _directory_mode(db_session, tree)
        parent = (await async_client.post("/api/v1/library/folders", json={"name": "EBZ"})).json()
        numbered = (
            await async_client.post(
                "/api/v1/library/folders", json={"name": "Halter", "number": "4031", "parent_id": parent["id"]}
            )
        ).json()
        (tree / "EBZ" / "Halter").mkdir()
        (tree / "EBZ" / "Halter" / "x.3mf").write_bytes(b"x")

        await autoscan_once(db_session)

        db_session.expire_all()
        children = (
            (await db_session.execute(select(LibraryFolder).where(LibraryFolder.parent_id == parent["id"])))
            .scalars()
            .all()
        )
        by_path = {c.external_path: (c.id, c.number) for c in children}
        assert by_path[str(tree / "EBZ" / "4031 Halter")] == (numbered["id"], "4031")
        new_id, new_number = by_path[str(tree / "EBZ" / "Halter")]
        assert new_id != numbered["id"] and new_number is None
        listed = (await async_client.get(f"/api/v1/library/files?folder_id={new_id}")).json()
        assert [f["filename"] for f in listed] == ["x.3mf"]

    @pytest.mark.asyncio
    async def test_same_name_numbered_siblings_are_told_apart(self, async_client: AsyncClient, db_session, tree):
        """Two orders for the same part number; one renamed back to the bare name."""
        from backend.app.services.library_autoscan import autoscan_once

        await _directory_mode(db_session, tree)
        parent = (await async_client.post("/api/v1/library/folders", json={"name": "EBZ"})).json()
        first = (
            await async_client.post(
                "/api/v1/library/folders", json={"name": "7200090094", "number": "4024", "parent_id": parent["id"]}
            )
        ).json()
        second = (
            await async_client.post(
                "/api/v1/library/folders", json={"name": "7200090094", "number": "4040", "parent_id": parent["id"]}
            )
        ).json()
        os.rename(tree / "EBZ" / "4040 7200090094", tree / "EBZ" / "7200090094")

        await autoscan_once(db_session)

        db_session.expire_all()
        children = {
            c.id: c.external_path
            for c in (await db_session.execute(select(LibraryFolder).where(LibraryFolder.parent_id == parent["id"])))
            .scalars()
            .all()
        }
        assert children == {
            first["id"]: str(tree / "EBZ" / "4024 7200090094"),
            second["id"]: str(tree / "EBZ" / "7200090094"),
        }

    @pytest.mark.asyncio
    async def test_nothing_is_removed_below_a_directory_the_walk_could_not_read(
        self, async_client: AsyncClient, db_session, tree, monkeypatch
    ):
        """Not seen is not gone: a directory renamed or locked while the walk
        passed it keeps its files' rows until a walk can read it again."""
        import backend.app.api.routes.library as library_routes
        from backend.app.services.library_autoscan import autoscan_once

        await _directory_mode(db_session, tree)
        parent = (await async_client.post("/api/v1/library/folders", json={"name": "EBZ"})).json()
        order = (
            await async_client.post("/api/v1/library/folders", json={"name": "4016", "parent_id": parent["id"]})
        ).json()
        (tree / "EBZ" / "4016" / "teil.3mf").write_bytes(b"x")
        assert (await autoscan_once(db_session))["added"] == 1

        real_walk = os.walk

        def walk_losing_4016(top, onerror=None, **kwargs):
            for dirpath, dirnames, filenames in real_walk(top, onerror=onerror, **kwargs):
                if "4016" in dirnames:
                    dirnames.remove("4016")
                    onerror(PermissionError(13, "Access is denied", str(Path(dirpath) / "4016")))
                yield dirpath, dirnames, filenames

        monkeypatch.setattr(library_routes.os, "walk", walk_losing_4016)
        result = await autoscan_once(db_session)

        assert result["removed"] == 0
        listed = (await async_client.get(f"/api/v1/library/files?folder_id={order['id']}")).json()
        assert [f["filename"] for f in listed] == ["teil.3mf"]

    @pytest.mark.asyncio
    async def test_the_interval_is_off_by_default(self, async_client: AsyncClient):
        """A walk of a mounted share is real network IO; nobody pays for it unasked."""
        settings = (await async_client.get("/api/v1/settings/")).json()
        assert settings["library_autoscan_minutes"] == 0


class TestNumbersOntoTheShare:
    """Once after the update: folders numbered while the number stayed off the
    share get "<number> <name>" — the edit dialog only saves a change, so
    nothing else would ever rename them."""

    async def _numbered_without_the_number_on_disk(self, async_client, db_session, name, number, parent=None):
        body = {"name": name, **({"parent_id": parent["id"]} if parent else {})}
        created = (await async_client.post("/api/v1/library/folders", json=body)).json()
        db_session.expire_all()
        row = (await db_session.execute(select(LibraryFolder).where(LibraryFolder.id == created["id"]))).scalar_one()
        row.number = number
        await db_session.commit()
        return created

    @pytest.mark.asyncio
    async def test_numbered_folders_get_their_number_in_front_parents_first(
        self, async_client: AsyncClient, db_session, tree
    ):
        await _directory_mode(db_session, tree)
        ebz = await self._numbered_without_the_number_on_disk(async_client, db_session, "EBZ", "001")
        order = await self._numbered_without_the_number_on_disk(async_client, db_session, "7200090094", "4024", ebz)
        inner = (
            await async_client.post(
                "/api/v1/library/folders", json={"name": "EBZ-7200090094", "parent_id": order["id"]}
            )
        ).json()
        (tree / "EBZ" / "7200090094" / "EBZ-7200090094" / "teil.3mf").write_bytes(b"x")
        plain = (await async_client.post("/api/v1/library/folders", json={"name": "Muster"})).json()

        result = await library_storage.align_numbered_directories(db_session)

        assert result == {"renamed": 2, "failed": 0, "skipped": None}
        assert (tree / "001 EBZ" / "4024 7200090094" / "EBZ-7200090094" / "teil.3mf").read_bytes() == b"x"
        assert not (tree / "EBZ").exists()
        assert (tree / "Muster").is_dir()
        db_session.expire_all()
        paths = {row.id: row.external_path for row in (await db_session.execute(select(LibraryFolder))).scalars().all()}
        assert paths[ebz["id"]] == str(tree / "001 EBZ")
        assert paths[order["id"]] == str(tree / "001 EBZ" / "4024 7200090094")
        assert paths[inner["id"]] == str(tree / "001 EBZ" / "4024 7200090094" / "EBZ-7200090094")
        assert paths[plain["id"]] == str(tree / "Muster")

        # Once: a folder renamed back by hand afterwards is left as it is.
        again = await library_storage.align_numbered_directories(db_session)
        assert again["skipped"] == "already done"

    @pytest.mark.asyncio
    async def test_a_folder_that_cannot_be_renamed_is_skipped_and_the_rest_go_on(
        self, async_client: AsyncClient, db_session, tree
    ):
        await _directory_mode(db_session, tree)
        await self._numbered_without_the_number_on_disk(async_client, db_session, "RAFI", "002")
        await self._numbered_without_the_number_on_disk(async_client, db_session, "LTS3D intern", "100")
        (tree / "002 RAFI").mkdir()  # the name is already taken on the share

        result = await library_storage.align_numbered_directories(db_session)

        assert result == {"renamed": 1, "failed": 1, "skipped": None}
        assert (tree / "RAFI").is_dir()
        assert (tree / "100 LTS3D intern").is_dir()

    @pytest.mark.asyncio
    async def test_a_directory_open_in_explorer_is_tried_again_on_the_next_start(
        self, async_client: AsyncClient, db_session, tree, monkeypatch
    ):
        await _directory_mode(db_session, tree)
        await self._numbered_without_the_number_on_disk(async_client, db_session, "EBZ", "001")
        real_rename = os.rename
        calls = {"n": 0}

        def busy_once(src, dst):
            calls["n"] += 1
            if calls["n"] == 1:
                raise PermissionError(13, "The process cannot access the file because it is being used")
            return real_rename(src, dst)

        monkeypatch.setattr(library_storage.os, "rename", busy_once)

        first = await library_storage.align_numbered_directories(db_session)
        assert first == {"renamed": 0, "failed": 1, "skipped": None}
        assert (tree / "EBZ").is_dir()

        second = await library_storage.align_numbered_directories(db_session)
        assert second == {"renamed": 1, "failed": 0, "skipped": None}
        assert (tree / "001 EBZ").is_dir()

    @pytest.mark.asyncio
    async def test_a_rename_whose_rows_were_not_written_is_put_back(
        self, async_client: AsyncClient, db_session, tree, monkeypatch
    ):
        """The share and the rows must say the same, or every file is unopenable."""
        await _directory_mode(db_session, tree)
        await self._numbered_without_the_number_on_disk(async_client, db_session, "EBZ", "001")
        real_rewrite = library_storage.rewrite_subtree_paths

        async def database_locked(*args, **kwargs):
            raise RuntimeError("database is locked")

        monkeypatch.setattr(library_storage, "rewrite_subtree_paths", database_locked)
        first = await library_storage.align_numbered_directories(db_session)
        assert first == {"renamed": 0, "failed": 1, "skipped": None}
        assert (tree / "EBZ").is_dir()
        assert not (tree / "001 EBZ").exists()

        monkeypatch.setattr(library_storage, "rewrite_subtree_paths", real_rewrite)
        second = await library_storage.align_numbered_directories(db_session)
        assert second == {"renamed": 1, "failed": 0, "skipped": None}
        assert (tree / "001 EBZ").is_dir()

    @pytest.mark.asyncio
    async def test_a_rename_that_reached_the_share_alone_is_caught_up(
        self, async_client: AsyncClient, db_session, tree
    ):
        """Renamed by a pass that crashed before its rows were written."""
        await _directory_mode(db_session, tree)
        ebz = await self._numbered_without_the_number_on_disk(async_client, db_session, "EBZ", "001")
        inner = (
            await async_client.post("/api/v1/library/folders", json={"name": "4016", "parent_id": ebz["id"]})
        ).json()
        os.rename(tree / "EBZ", tree / "001 EBZ")

        result = await library_storage.align_numbered_directories(db_session)

        assert result == {"renamed": 1, "failed": 0, "skipped": None}
        db_session.expire_all()
        paths = {r.id: r.external_path for r in (await db_session.execute(select(LibraryFolder))).scalars().all()}
        assert paths == {ebz["id"]: str(tree / "001 EBZ"), inner["id"]: str(tree / "001 EBZ" / "4016")}

    @pytest.mark.asyncio
    async def test_nothing_is_marked_done_while_the_share_is_away(
        self, async_client: AsyncClient, db_session, tmp_path
    ):
        gone = tmp_path / "unterwegs"
        gone.mkdir()
        await _directory_mode(db_session, gone)
        gone.rmdir()

        result = await library_storage.align_numbered_directories(db_session)

        assert "does not exist" in result["skipped"]
        retry = await library_storage.align_numbered_directories(db_session)
        assert retry["skipped"] != "already done"


class TestRefreshOnOpen:
    """Opening a folder brings it up to date — the alternative to the interval.

    Only the folder being looked at, only when it is looked at: nothing walks
    the share while nobody is using Bambuddy.
    """

    @pytest.fixture(autouse=True)
    def _forget_throttle(self):
        from backend.app.services import library_autoscan

        library_autoscan._last_refresh.clear()
        yield
        library_autoscan._last_refresh.clear()

    @pytest.mark.asyncio
    async def test_a_file_put_there_in_explorer_shows_up_on_opening(self, async_client: AsyncClient, db_session, tree):
        await _directory_mode(db_session, tree)
        folder = (await async_client.post("/api/v1/library/folders", json={"name": "Kunden"})).json()
        (tree / "Kunden" / "aus-dem-explorer.3mf").write_bytes(b"dropped in by hand")

        response = await async_client.post(f"/api/v1/library/folders/{folder['id']}/refresh")
        assert response.status_code == 200, response.text
        assert response.json() == {"added": 1, "removed": 0, "skipped": None}

        listed = (await async_client.get(f"/api/v1/library/files?folder_id={folder['id']}")).json()
        assert [f["filename"] for f in listed] == ["aus-dem-explorer.3mf"]

    @pytest.mark.asyncio
    async def test_clicking_back_and_forth_does_not_walk_it_again(self, async_client: AsyncClient, db_session, tree):
        await _directory_mode(db_session, tree)
        folder = (await async_client.post("/api/v1/library/folders", json={"name": "Kunden"})).json()

        first = (await async_client.post(f"/api/v1/library/folders/{folder['id']}/refresh")).json()
        second = (await async_client.post(f"/api/v1/library/folders/{folder['id']}/refresh")).json()
        assert first["skipped"] is None
        assert second["skipped"] == "recently refreshed"

    @pytest.mark.asyncio
    async def test_it_does_nothing_in_managed_mode(self, async_client: AsyncClient, db_session):
        folder = (await async_client.post("/api/v1/library/folders", json={"name": "Kunden"})).json()

        response = await async_client.post(f"/api/v1/library/folders/{folder['id']}/refresh")
        assert response.json()["skipped"] == "not a directory library"

    @pytest.mark.asyncio
    async def test_opening_a_folder_while_the_tree_is_being_renamed_waits_for_nothing(
        self, async_client: AsyncClient, db_session, tree
    ):
        """A walk during a rename would take the renamed folder's files for gone."""
        await _directory_mode(db_session, tree)
        folder = (await async_client.post("/api/v1/library/folders", json={"name": "Kunden"})).json()
        (tree / "Kunden" / "neu.3mf").write_bytes(b"x")

        async with library_storage.tree_lock():
            busy = (await async_client.post(f"/api/v1/library/folders/{folder['id']}/refresh")).json()
        assert busy == {"added": 0, "removed": 0, "skipped": "library being renamed"}

        later = (await async_client.post(f"/api/v1/library/folders/{folder['id']}/refresh")).json()
        assert later == {"added": 1, "removed": 0, "skipped": None}

    @pytest.mark.asyncio
    async def test_it_can_be_switched_off(self, async_client: AsyncClient, db_session, tree):
        await _directory_mode(db_session, tree)
        await _set(db_session, "library_scan_on_open", "false")
        folder = (await async_client.post("/api/v1/library/folders", json={"name": "Kunden"})).json()
        (tree / "Kunden" / "neu.3mf").write_bytes(b"x")

        response = await async_client.post(f"/api/v1/library/folders/{folder['id']}/refresh")
        assert response.json()["skipped"] == "switched off"

    @pytest.mark.asyncio
    async def test_a_share_that_dropped_off_is_left_alone(self, async_client: AsyncClient, db_session, tree):
        await _directory_mode(db_session, tree)
        folder = (await async_client.post("/api/v1/library/folders", json={"name": "Kunden"})).json()
        (tree / "Kunden" / "teil.3mf").write_bytes(b"x")
        first = (await async_client.post(f"/api/v1/library/folders/{folder['id']}/refresh")).json()
        assert first["added"] == 1

        from backend.app.services import library_autoscan

        library_autoscan._last_refresh.clear()
        # The mount goes away and leaves an empty directory behind.
        (tree / "Kunden" / "teil.3mf").unlink()
        (tree / "Kunden").rmdir()

        second = (await async_client.post(f"/api/v1/library/folders/{folder['id']}/refresh")).json()
        assert second["removed"] == 0
        assert second["skipped"] == "library directory unavailable"

    @pytest.mark.asyncio
    async def test_it_is_on_by_default(self, async_client: AsyncClient):
        settings = (await async_client.get("/api/v1/settings/")).json()
        assert settings["library_scan_on_open"] is True


def _in_use(source, target):
    """What a share answers for a file that is open on a workstation."""
    raise PermissionError(13, "The file is in use by another process")


class TestTheTrashOnTheShare:
    """A delete in the tree moves the bytes into ``.bambuddy-trash`` (#3160).

    Dropping only the row, which is what #124's external folders do, left the
    file where it was — and the next scan filed it straight back into the
    library. So everything here is asserted on the share first: the share is
    what the next scan believes.
    """

    @pytest.fixture(autouse=True)
    def _forget_throttle(self):
        from backend.app.services import library_autoscan

        library_autoscan._last_refresh.clear()
        yield
        library_autoscan._last_refresh.clear()

    async def _scanned(self, async_client: AsyncClient, folder_id: int) -> list[dict]:
        scan = await async_client.post(f"/api/v1/library/folders/{folder_id}/scan")
        assert scan.status_code == 200, scan.text
        return (await async_client.get(f"/api/v1/library/files?folder_id={folder_id}")).json()

    async def _folder_with_file(self, async_client: AsyncClient, db_session, tree) -> tuple[dict, dict]:
        await _directory_mode(db_session, tree)
        folder = (await async_client.post("/api/v1/library/folders", json={"name": "Kunden"})).json()
        (tree / "Kunden" / "teil.3mf").write_bytes(b"the real part")
        [file] = await self._scanned(async_client, folder["id"])
        return folder, file

    async def _trashed_path(self, async_client: AsyncClient, db_session, file_id: int) -> Path:
        response = await async_client.delete(f"/api/v1/library/files/{file_id}")
        assert response.status_code == 200, response.text
        return Path((await _row(db_session, file_id)).file_path)

    @pytest.mark.asyncio
    async def test_a_deleted_file_moves_into_the_trash_and_stays_gone(
        self, async_client: AsyncClient, db_session, tree
    ):
        folder, file = await self._folder_with_file(async_client, db_session, tree)

        response = await async_client.delete(f"/api/v1/library/files/{file['id']}")
        assert response.status_code == 200, response.text
        assert response.json()["trashed"] is True
        assert not (tree / "Kunden" / "teil.3mf").exists()

        row = await _row(db_session, file["id"])
        assert row.deleted_at is not None
        # The folder stays on the row: it is where a restore puts the file.
        assert row.folder_id == folder["id"]
        trashed = Path(row.file_path)
        assert trashed.read_bytes() == b"the real part"
        assert trashed.parent.parent == (tree / TRASH).resolve()
        assert re.fullmatch(rf"\d{{8}}-\d{{6}}-f{file['id']}", trashed.parent.name)

        # Neither the Scan button nor opening the folder brings it back.
        assert await self._scanned(async_client, folder["id"]) == []
        refresh = await async_client.post(f"/api/v1/library/folders/{folder['id']}/refresh")
        assert refresh.json() == {"added": 0, "removed": 0, "skipped": None}
        trash = (await async_client.get("/api/v1/library/trash")).json()
        assert [item["id"] for item in trash["items"]] == [file["id"]]

    @pytest.mark.asyncio
    async def test_a_scan_never_indexes_the_trash_even_showing_hidden_files(self, async_client: AsyncClient, tree):
        """A dot-directory, but "show hidden" is about the user's files, not this."""
        deleted = tree / TRASH / "20260101-000000-d7" / "Kunden"
        deleted.mkdir(parents=True)
        (deleted / "geloescht.3mf").write_bytes(b"deleted once")
        (tree / "Auftrag").mkdir()
        (tree / "Auftrag" / "teil.3mf").write_bytes(b"a real file")

        created = await async_client.post(
            "/api/v1/library/folders/external",
            json={"name": "Freigabe", "external_path": str(tree), "readonly": False, "show_hidden": True},
        )
        assert created.status_code == 200, created.text
        scan = await async_client.post(f"/api/v1/library/folders/{created.json()['id']}/scan")
        assert scan.status_code == 200, scan.text
        assert scan.json()["added"] == 1

        folders = (await async_client.get("/api/v1/library/folders")).json()
        names = {node["name"] for node in _walk(folders)}
        assert "Auftrag" in names
        assert not {TRASH, "20260101-000000-d7", "Kunden"} & names

    @pytest.mark.asyncio
    async def test_restore_puts_the_file_back_where_it_was(self, async_client: AsyncClient, db_session, tree):
        folder, file = await self._folder_with_file(async_client, db_session, tree)
        trashed = await self._trashed_path(async_client, db_session, file["id"])

        response = await async_client.post(f"/api/v1/library/trash/{file['id']}/restore")
        assert response.status_code == 200, response.text
        assert (tree / "Kunden" / "teil.3mf").read_bytes() == b"the real part"
        row = await _row(db_session, file["id"])
        assert row.deleted_at is None
        assert row.folder_id == folder["id"]
        assert Path(row.file_path) == (tree / "Kunden" / "teil.3mf").resolve()
        # The emptied entry goes with it; the trash directory itself stays.
        assert not trashed.parent.exists()
        assert _trash_entries(tree) == []
        # A library file again, and the same one: the scan finds no second row.
        assert [f["id"] for f in await self._scanned(async_client, folder["id"])] == [file["id"]]

    @pytest.mark.asyncio
    async def test_restore_onto_a_name_taken_since_is_refused_and_changes_nothing(
        self, async_client: AsyncClient, db_session, tree
    ):
        _, file = await self._folder_with_file(async_client, db_session, tree)
        trashed = await self._trashed_path(async_client, db_session, file["id"])
        (tree / "Kunden" / "teil.3mf").write_bytes(b"a newer part")

        response = await async_client.post(f"/api/v1/library/trash/{file['id']}/restore")
        assert response.status_code == 409, response.text
        assert "already exists" in response.json()["detail"]
        assert (tree / "Kunden" / "teil.3mf").read_bytes() == b"a newer part"
        assert trashed.read_bytes() == b"the real part"
        row = await _row(db_session, file["id"])
        assert row.deleted_at is not None
        assert Path(row.file_path) == trashed

    @pytest.mark.asyncio
    async def test_restore_with_the_share_gone_is_refused_and_changes_nothing(
        self, async_client: AsyncClient, db_session, tree
    ):
        _, file = await self._folder_with_file(async_client, db_session, tree)
        trashed = await self._trashed_path(async_client, db_session, file["id"])

        offline = tree.with_name("Bambuddy-offline")
        tree.rename(offline)
        try:
            response = await async_client.post(f"/api/v1/library/trash/{file['id']}/restore")
        finally:
            offline.rename(tree)

        assert response.status_code == 400, response.text
        assert "does not exist" in response.json()["detail"]
        row = await _row(db_session, file["id"])
        assert row.deleted_at is not None
        assert Path(row.file_path) == trashed
        assert trashed.exists()

    @pytest.mark.asyncio
    async def test_delete_now_unlinks_it_from_the_trash_and_nothing_else(
        self, async_client: AsyncClient, db_session, tree
    ):
        _, file = await self._folder_with_file(async_client, db_session, tree)
        (tree / "Kunden" / "nachbar.3mf").write_bytes(b"a neighbour")
        trashed = await self._trashed_path(async_client, db_session, file["id"])

        response = await async_client.delete(f"/api/v1/library/trash/{file['id']}")
        assert response.status_code == 200, response.text
        assert not trashed.exists()
        assert not trashed.parent.exists()
        assert (tree / TRASH).is_dir()
        assert (tree / "Kunden" / "nachbar.3mf").read_bytes() == b"a neighbour"
        assert await _row(db_session, file["id"]) is None

    @pytest.mark.asyncio
    async def test_a_purge_never_unlinks_a_live_file_on_the_share(self, async_client: AsyncClient, db_session, tree):
        """A trashed row is evidence of nothing: only the trash is ever emptied."""
        await _directory_mode(db_session, tree)
        (tree / "Kunden").mkdir()
        live = tree / "Kunden" / "live.3mf"
        live.write_bytes(b"in use")
        row = LibraryFile(
            filename="live.3mf",
            file_path=str(live),
            file_type="3mf",
            file_size=6,
            is_external=True,
            deleted_at=datetime.now(timezone.utc),
        )
        db_session.add(row)
        await db_session.commit()

        response = await async_client.delete(f"/api/v1/library/trash/{row.id}")
        assert response.status_code == 200, response.text
        assert live.read_bytes() == b"in use"
        assert await _row(db_session, row.id) is None

    @pytest.mark.asyncio
    async def test_the_sweeper_purges_it_from_the_trash_after_the_retention_window(
        self, async_client: AsyncClient, db_session, tree
    ):
        from backend.app.services.library_trash import library_trash_service

        _, file = await self._folder_with_file(async_client, db_session, tree)
        trashed = await self._trashed_path(async_client, db_session, file["id"])
        row = await _row(db_session, file["id"])
        row.deleted_at = datetime.now(timezone.utc) - timedelta(days=400)
        await db_session.commit()

        assert await library_trash_service._sweep(db_session) == 1
        assert not trashed.exists()
        assert not trashed.parent.exists()
        assert await _row(db_session, file["id"]) is None

    @pytest.mark.asyncio
    async def test_a_move_the_share_refuses_changes_nothing(
        self, async_client: AsyncClient, db_session, tree, monkeypatch
    ):
        """A file held open on a workstation cannot be moved: 400, rows as they were."""
        _, file = await self._folder_with_file(async_client, db_session, tree)
        before = (await _row(db_session, file["id"])).file_path

        with monkeypatch.context() as patched:
            patched.setattr(library_storage.os, "rename", _in_use)
            response = await async_client.delete(f"/api/v1/library/files/{file['id']}")

        assert response.status_code == 400, response.text
        assert "in use" in response.json()["detail"]
        assert (tree / "Kunden" / "teil.3mf").read_bytes() == b"the real part"
        row = await _row(db_session, file["id"])
        assert row.deleted_at is None
        assert row.file_path == before
        assert _trash_entries(tree) == []

    @pytest.mark.asyncio
    async def test_a_deleted_folder_moves_into_the_trash_whole(self, async_client: AsyncClient, db_session, tree):
        from backend.app.services.library_autoscan import autoscan_once

        await _directory_mode(db_session, tree)
        kunden = (await async_client.post("/api/v1/library/folders", json={"name": "Kunden"})).json()
        rafi = (
            await async_client.post("/api/v1/library/folders", json={"name": "RAFI", "parent_id": kunden["id"]})
        ).json()
        (tree / "Kunden" / "a.3mf").write_bytes(b"a")
        (tree / "Kunden" / "RAFI" / "b.3mf").write_bytes(b"b")
        (tree / "Kunden" / "notiz.txt").write_text("not a library file")
        scan = await async_client.post(f"/api/v1/library/folders/{kunden['id']}/scan")
        assert scan.json()["added"] == 2
        db_session.expire_all()
        ids = {row.filename: row.id for row in (await db_session.execute(select(LibraryFile))).scalars()}

        response = await async_client.delete(f"/api/v1/library/folders/{kunden['id']}")
        assert response.status_code == 200, response.text
        assert "directory_kept" not in response.json()
        assert not (tree / "Kunden").exists()

        [entry] = _trash_entries(tree)
        assert re.fullmatch(rf"\d{{8}}-\d{{6}}-d{kunden['id']}", entry.name)
        assert (entry / "Kunden" / "a.3mf").read_bytes() == b"a"
        assert (entry / "Kunden" / "RAFI" / "b.3mf").read_bytes() == b"b"
        assert (entry / "Kunden" / "notiz.txt").exists()

        # The rows survived the cascade: trashed, and out of the deleted folders.
        for name, parts in (("a.3mf", ("Kunden", "a.3mf")), ("b.3mf", ("Kunden", "RAFI", "b.3mf"))):
            row = await _row(db_session, ids[name])
            assert row is not None
            assert row.deleted_at is not None
            assert row.folder_id is None
            assert Path(row.file_path) == entry.resolve().joinpath(*parts)
        folder_ids = [kunden["id"], rafi["id"]]
        assert (await db_session.execute(select(LibraryFolder).where(LibraryFolder.id.in_(folder_ids)))).all() == []

        # Nothing comes back with the next scan, neither the folder nor a file.
        assert (await autoscan_once(db_session))["added"] == 0
        folders = (await async_client.get("/api/v1/library/folders")).json()
        assert not {"Kunden", "RAFI"} & {node["name"] for node in _walk(folders)}
        trash = (await async_client.get("/api/v1/library/trash")).json()
        assert {item["id"] for item in trash["items"]} == {ids["a.3mf"], ids["b.3mf"]}

    @pytest.mark.asyncio
    async def test_scanning_the_parent_does_not_bring_a_deleted_folder_back(
        self, async_client: AsyncClient, db_session, tree
    ):
        await _directory_mode(db_session, tree)
        kunden = (await async_client.post("/api/v1/library/folders", json={"name": "Kunden"})).json()
        rafi = (
            await async_client.post("/api/v1/library/folders", json={"name": "RAFI", "parent_id": kunden["id"]})
        ).json()
        (tree / "Kunden" / "RAFI" / "b.3mf").write_bytes(b"b")
        assert (await async_client.post(f"/api/v1/library/folders/{kunden['id']}/scan")).json()["added"] == 1

        response = await async_client.delete(f"/api/v1/library/folders/{rafi['id']}")
        assert response.status_code == 200, response.text
        assert (tree / "Kunden").is_dir()
        assert not (tree / "Kunden" / "RAFI").exists()

        scan = await async_client.post(f"/api/v1/library/folders/{kunden['id']}/scan")
        assert scan.json() == {"status": "success", "added": 0, "removed": 0}
        folders = (await async_client.get("/api/v1/library/folders")).json()
        names = {node["name"] for node in _walk(folders)}
        assert "Kunden" in names
        assert "RAFI" not in names

    @pytest.mark.asyncio
    async def test_a_file_of_a_deleted_folder_restores_into_the_root(self, async_client: AsyncClient, db_session, tree):
        folder, file = await self._folder_with_file(async_client, db_session, tree)
        assert (await async_client.delete(f"/api/v1/library/folders/{folder['id']}")).status_code == 200

        response = await async_client.post(f"/api/v1/library/trash/{file['id']}/restore")
        assert response.status_code == 200, response.text
        assert (tree / "teil.3mf").read_bytes() == b"the real part"
        row = await _row(db_session, file["id"])
        assert row.deleted_at is None
        assert row.folder_id is None
        assert Path(row.file_path) == (tree / "teil.3mf").resolve()
        # With its only file out, the folder's entry had nothing left and went.
        assert _trash_entries(tree) == []

    @pytest.mark.asyncio
    async def test_a_file_deleted_before_its_folder_keeps_its_restore(
        self, async_client: AsyncClient, db_session, tree
    ):
        """Delete the file, then the folder it left empty: the file is still in the trash."""
        folder, file = await self._folder_with_file(async_client, db_session, tree)
        trashed = await self._trashed_path(async_client, db_session, file["id"])

        response = await async_client.delete(f"/api/v1/library/folders/{folder['id']}")
        assert response.status_code == 200, response.text
        assert not (tree / "Kunden").exists()

        row = await _row(db_session, file["id"])
        assert row is not None
        assert row.deleted_at is not None
        assert row.folder_id is None
        assert Path(row.file_path) == trashed
        assert trashed.exists()

        restored = await async_client.post(f"/api/v1/library/trash/{file['id']}/restore")
        assert restored.status_code == 200, restored.text
        assert (tree / "teil.3mf").read_bytes() == b"the real part"

    @pytest.mark.asyncio
    async def test_a_folder_the_share_refuses_to_move_stays_whole(
        self, async_client: AsyncClient, db_session, tree, monkeypatch
    ):
        folder, file = await self._folder_with_file(async_client, db_session, tree)

        with monkeypatch.context() as patched:
            patched.setattr(library_storage.os, "rename", _in_use)
            response = await async_client.delete(f"/api/v1/library/folders/{folder['id']}")

        assert response.status_code == 400, response.text
        assert (tree / "Kunden" / "teil.3mf").read_bytes() == b"the real part"
        assert _trash_entries(tree) == []
        db_session.expire_all()
        still = (await db_session.execute(select(LibraryFolder).where(LibraryFolder.id == folder["id"]))).scalar_one()
        assert still.external_path == str(tree / "Kunden")
        row = await _row(db_session, file["id"])
        assert row.deleted_at is None
        assert row.folder_id == folder["id"]

    @pytest.mark.asyncio
    async def test_emptying_the_trash_takes_the_files_and_nothing_else(
        self, async_client: AsyncClient, db_session, tree
    ):
        folder, file = await self._folder_with_file(async_client, db_session, tree)
        (tree / "Kunden" / "notiz.txt").write_text("not a library file")
        assert (await async_client.delete(f"/api/v1/library/folders/{folder['id']}")).status_code == 200
        [entry] = _trash_entries(tree)

        response = await async_client.delete("/api/v1/library/trash")
        assert response.status_code == 200, response.text
        assert response.json()["deleted"] == 1
        assert not (entry / "Kunden" / "teil.3mf").exists()
        # The note never had a row; it waits, entry and all, for the sweeper.
        assert (entry / "Kunden" / "notiz.txt").exists()

    @pytest.mark.asyncio
    async def test_the_file_managers_multi_select_delete_trashes_them_too(
        self, async_client: AsyncClient, db_session, tree
    ):
        await _directory_mode(db_session, tree)
        folder = (await async_client.post("/api/v1/library/folders", json={"name": "Kunden"})).json()
        (tree / "Kunden" / "eins.3mf").write_bytes(b"1")
        (tree / "Kunden" / "zwei.3mf").write_bytes(b"2")
        ids = [f["id"] for f in await self._scanned(async_client, folder["id"])]

        response = await async_client.post("/api/v1/library/bulk-delete", json={"file_ids": ids})
        assert response.status_code == 200, response.text
        assert response.json()["deleted_files"] == 2
        assert list((tree / "Kunden").iterdir()) == []
        for file_id in ids:
            row = await _row(db_session, file_id)
            assert row.deleted_at is not None
            assert Path(row.file_path).exists()
        assert await self._scanned(async_client, folder["id"]) == []

    @pytest.mark.asyncio
    async def test_a_multi_select_delete_puts_back_what_it_moved_when_a_later_file_fails(
        self, async_client: AsyncClient, db_session, tree, monkeypatch
    ):
        """All or nothing: the share has to match the rows the rollback leaves."""
        await _directory_mode(db_session, tree)
        folder = (await async_client.post("/api/v1/library/folders", json={"name": "Kunden"})).json()
        (tree / "Kunden" / "eins.3mf").write_bytes(b"1")
        (tree / "Kunden" / "zwei.3mf").write_bytes(b"2")
        ids = [f["id"] for f in await self._scanned(async_client, folder["id"])]

        real_rename = os.rename
        renames: list[str] = []

        def the_second_one_is_in_use(source, target):
            renames.append(str(source))
            if len(renames) == 2:
                _in_use(source, target)
            real_rename(source, target)

        with monkeypatch.context() as patched:
            patched.setattr(library_storage.os, "rename", the_second_one_is_in_use)
            response = await async_client.post("/api/v1/library/bulk-delete", json={"file_ids": ids})

        assert response.status_code == 400, response.text
        # The first one went into the trash and came back out.
        assert len(renames) == 3
        assert sorted(p.name for p in (tree / "Kunden").iterdir()) == ["eins.3mf", "zwei.3mf"]
        assert _trash_entries(tree) == []
        for file_id in ids:
            row = await _row(db_session, file_id)
            assert row.deleted_at is None
            assert Path(row.file_path).parent == (tree / "Kunden").resolve()

    @pytest.mark.asyncio
    async def test_an_external_folder_outside_the_tree_keeps_its_files(
        self, async_client: AsyncClient, db_session, tree, tmp_path
    ):
        """#124's rule, unchanged: the row goes, the bytes are somebody else's."""
        await _directory_mode(db_session, tree)
        mount = tmp_path / "anderes-laufwerk"
        mount.mkdir()
        (mount / "fremd.3mf").write_bytes(b"not ours")
        (mount / "auch-fremd.3mf").write_bytes(b"not ours either")
        created = await async_client.post(
            "/api/v1/library/folders/external",
            json={"name": "Fremd", "external_path": str(mount), "readonly": False},
        )
        assert created.status_code == 200, created.text
        folder = created.json()
        files = {f["filename"]: f for f in await self._scanned(async_client, folder["id"])}

        response = await async_client.delete(f"/api/v1/library/files/{files['fremd.3mf']['id']}")
        assert response.status_code == 200, response.text
        assert response.json()["trashed"] is False
        assert await _row(db_session, files["fremd.3mf"]["id"]) is None
        assert (mount / "fremd.3mf").read_bytes() == b"not ours"

        response = await async_client.delete(f"/api/v1/library/folders/{folder['id']}")
        assert response.status_code == 200, response.text
        assert (mount / "auch-fremd.3mf").read_bytes() == b"not ours either"
        assert await _row(db_session, files["auch-fremd.3mf"]["id"]) is None
        assert not (tree / TRASH).exists()
        assert not (mount / TRASH).exists()

    @pytest.mark.asyncio
    async def test_a_read_only_folder_in_the_tree_keeps_its_files(self, async_client: AsyncClient, db_session, tree):
        await _directory_mode(db_session, tree)
        archive = tree / "Archiv"
        archive.mkdir()
        (archive / "alt.3mf").write_bytes(b"old")
        (archive / "uralt.3mf").write_bytes(b"older")
        created = await async_client.post(
            "/api/v1/library/folders/external",
            json={"name": "Archiv", "external_path": str(archive), "readonly": True},
        )
        assert created.status_code == 200, created.text
        folder = created.json()
        files = {f["filename"]: f for f in await self._scanned(async_client, folder["id"])}

        response = await async_client.delete(f"/api/v1/library/files/{files['alt.3mf']['id']}")
        assert response.status_code == 200, response.text
        assert response.json()["trashed"] is False
        assert await _row(db_session, files["alt.3mf"]["id"]) is None
        assert (archive / "alt.3mf").read_bytes() == b"old"

        response = await async_client.delete(f"/api/v1/library/folders/{folder['id']}")
        assert response.status_code == 200, response.text
        assert Path(response.json()["directory_kept"]) == archive.resolve()
        assert (archive / "uralt.3mf").read_bytes() == b"older"
        assert not (tree / TRASH).exists()

    @pytest.mark.asyncio
    async def test_managed_mode_keeps_the_trash_it_always_had(self, async_client: AsyncClient, db_session, tree):
        from backend.app.api.routes.library import to_absolute_path

        uploaded = await async_client.post(
            "/api/v1/library/files",
            files={"file": ("managed.stl", b"solid managed\nfacet\n", "application/octet-stream")},
        )
        assert uploaded.status_code == 200, uploaded.text
        file_id = uploaded.json()["id"]
        before = (await _row(db_session, file_id)).file_path

        response = await async_client.delete(f"/api/v1/library/files/{file_id}")
        assert response.json()["trashed"] is True
        row = await _row(db_session, file_id)
        assert row.deleted_at is not None
        assert row.is_external is False
        assert row.file_path == before
        assert to_absolute_path(before).is_file()

        restored = await async_client.post(f"/api/v1/library/trash/{file_id}/restore")
        assert restored.status_code == 200, restored.text
        row = await _row(db_session, file_id)
        assert row.deleted_at is None
        assert row.file_path == before
        assert not (tree / TRASH).exists()

    @pytest.mark.asyncio
    async def test_the_sweeper_removes_what_a_folder_delete_left_once_it_expired(
        self, async_client: AsyncClient, db_session, tree
    ):
        """A note the scan never indexed has no row, so no row sweep reaches it."""
        from backend.app.services.library_trash import library_trash_service

        await _directory_mode(db_session, tree)
        await library_trash_service.set_retention_days(db_session, 30)
        trash = tree / TRASH
        expired = trash / "20200101-000000-d7"
        (expired / "Kunden").mkdir(parents=True)
        (expired / "Kunden" / "notiz.txt").write_text("left behind by a folder delete")
        recent = trash / f"{datetime.now(timezone.utc) - timedelta(days=1):%Y%m%d-%H%M%S}-d8"
        (recent / "Kunden").mkdir(parents=True)
        (recent / "Kunden" / "notiz.txt").write_text("still within the window")
        claimed = trash / "20200101-000000-f9"
        claimed.mkdir()
        (claimed / "teil.3mf").write_bytes(b"a row still points here")
        db_session.add(
            LibraryFile(
                filename="teil.3mf",
                file_path=str(claimed / "teil.3mf"),
                file_type="3mf",
                file_size=23,
                is_external=True,
                deleted_at=datetime.now(timezone.utc) - timedelta(days=2),
            )
        )
        await db_session.commit()
        foreign = trash / "nicht-von-bambuddy"
        foreign.mkdir()

        assert await library_trash_service._sweep_share_trash(db_session) == 1
        assert not expired.exists()
        assert recent.exists()
        assert claimed.exists()
        assert foreign.exists()


class TestThePathGuard:
    def test_a_stored_path_outside_the_root_is_not_trusted(self, tmp_path):
        """The column is a string. A restored backup can name anything."""
        root = tmp_path / "tree"
        root.mkdir()
        assert library_storage.is_inside_tree(root, root / "Kunden") is True
        assert library_storage.is_inside_tree(root, tmp_path / "elsewhere") is False
        assert library_storage.is_inside_tree(None, root / "Kunden") is False

    def test_only_something_inside_an_entry_is_in_the_trash(self, tmp_path):
        """What a purge may unlink: below an entry, and nothing a ``..`` reaches."""
        root = tmp_path / "tree"
        entry = root / TRASH / "20260101-000000-f1"
        entry.mkdir(parents=True)
        assert library_storage.trash_entry_of(root, entry / "teil.3mf") == entry.resolve()
        assert library_storage.trash_entry_of(root, entry) is None
        assert library_storage.trash_entry_of(root, root / TRASH) is None
        assert library_storage.trash_entry_of(root, root / "Kunden" / "teil.3mf") is None
        assert library_storage.trash_entry_of(root, root / TRASH / ".." / "Kunden" / "teil.3mf") is None
        assert library_storage.trash_entry_of(None, entry / "teil.3mf") is None

    def test_a_component_is_reduced_to_one_directory_name(self):
        assert library_storage.directory_component("a/b", None) == "a-b"
        assert library_storage.directory_component("..", None, fallback_id=7) == "folder-7"
        assert library_storage.directory_component("", "4021") == "4021"

    def test_a_numbered_folder_is_named_number_first(self):
        assert library_storage.directory_component("EBZ", "001") == "001 EBZ"
        assert library_storage.directory_component("7200090094", "4024") == "4024 7200090094"
        assert library_storage.directory_component("EBZ", None) == "EBZ"
        # Already in front, with any separator: kept once.
        assert library_storage.directory_component("4026 Gehäuse", "4026") == "4026 Gehäuse"
        assert library_storage.directory_component("4026-Gehäuse", "4026") == "4026-Gehäuse"
        assert library_storage.directory_component("4026", "4026") == "4026"
        # A longer number that merely begins with the same digits is another number.
        assert library_storage.directory_component("40261 Teil", "4026") == "4026 40261 Teil"
        # The number is a path component too.
        assert library_storage.directory_component("Teil", "A/7") == "A-7 Teil"
