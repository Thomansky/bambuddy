"""Folder ownership on the library's fork routes (#3201 meets #3160).

Three gaps the merge of folder ownership left in routes the fork added: a
directory the scan finds inside a user's private subfolder took the owner and
the sharing of the top-level folder being walked, opening (refreshing) a
folder skipped the visibility check every other folder route makes, and the
list of files waiting for a browser-rendered preview named every user's files.
"""

import pytest
from httpx import AsyncClient
from sqlalchemy import select

from backend.app.models.library import LibraryFile, LibraryFolder
from backend.app.models.settings import Settings
from backend.tests.integration.test_ownership_permissions import TestOwnershipPermissionsSetup

pytestmark = pytest.mark.integration


@pytest.fixture(autouse=True)
def _permit_tmp_as_external_root(monkeypatch, tmp_path):
    """The tree lives under pytest's tmp_path, which is not an allowlisted root."""
    monkeypatch.setenv("BAMBUDDY_EXTERNAL_ROOTS", str(tmp_path.parent))


@pytest.fixture(autouse=True)
def _forget_throttle():
    from backend.app.services import library_autoscan

    library_autoscan._last_refresh.clear()
    yield
    library_autoscan._last_refresh.clear()


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


async def _folder_row(db_session, **where) -> LibraryFolder:
    """The folder as the database has it now, not as this session cached it.

    Refreshes only the row it loads: expiring the whole session would leave
    rows an earlier call returned to lazy-load outside the event loop.
    """
    query = select(LibraryFolder).execution_options(populate_existing=True)
    for column, value in where.items():
        query = query.where(getattr(LibraryFolder, column) == value)
    return (await db_session.execute(query)).scalar_one()


async def _share(db_session, folder_id: int) -> None:
    folder = await _folder_row(db_session, id=folder_id)
    folder.shared = True
    await db_session.commit()


def _h(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _ids(nodes: list[dict]) -> set[int]:
    out: set[int] = set()
    for node in nodes:
        out.add(node["id"])
        out |= _ids(node.get("children") or [])
    return out


class TestScannedSubfolderOwnership(TestOwnershipPermissionsSetup):
    """A directory made in Explorer belongs with the folder it was made in."""

    async def _create(self, async_client: AsyncClient, token: str, **body) -> dict:
        response = await async_client.post("/api/v1/library/folders", json=body, headers=_h(token))
        assert response.status_code == 200, response.text
        return response.json()

    async def _scan(self, async_client: AsyncClient, token: str, folder_id: int) -> None:
        response = await async_client.post(f"/api/v1/library/folders/{folder_id}/scan", headers=_h(token))
        assert response.status_code == 200, response.text

    @pytest.mark.asyncio
    async def test_a_directory_in_a_private_subfolder_stays_private(
        self, async_client: AsyncClient, auth_setup, db_session, tree
    ):
        await _directory_mode(db_session, tree)
        admin = auth_setup["admin_user"]["id"]
        me = auth_setup["operator_user"]["id"]

        jobs = await self._create(async_client, auth_setup["admin_token"], name="Auftraege")
        await _share(db_session, jobs["id"])
        draft = await self._create(async_client, auth_setup["operator_token"], name="B-Entwurf", parent_id=jobs["id"])
        assert (tree / "Auftraege" / "B-Entwurf").is_dir()

        # Made in Explorer: one inside the private subfolder, one beside it.
        (tree / "Auftraege" / "B-Entwurf" / "Neu").mkdir()
        (tree / "Auftraege" / "Oben").mkdir()

        # The walk starts at the shared top-level folder, as autoscan does.
        await self._scan(async_client, auth_setup["admin_token"], jobs["id"])

        inner = await _folder_row(db_session, name="Neu")
        assert inner.parent_id == draft["id"]
        assert inner.created_by_id == me
        assert inner.shared is False
        beside = await _folder_row(db_session, name="Oben")
        assert beside.parent_id == jobs["id"]
        assert beside.created_by_id == admin
        assert beside.shared is True

        # Neither the new folder nor the private one it is in reaches another user.
        other_tree = (
            await async_client.get("/api/v1/library/folders", headers=_h(auth_setup["operator2_token"]))
        ).json()
        visible = _ids(other_tree)
        assert jobs["id"] in visible and beside.id in visible
        assert inner.id not in visible and draft["id"] not in visible

    @pytest.mark.asyncio
    async def test_a_directory_in_a_shared_subfolder_of_a_private_folder_is_shared(
        self, async_client: AsyncClient, auth_setup, db_session, tree
    ):
        await _directory_mode(db_session, tree)
        admin = auth_setup["admin_user"]["id"]

        private_top = await self._create(async_client, auth_setup["admin_token"], name="Intern")
        common = await self._create(
            async_client, auth_setup["admin_token"], name="Gemeinsam", parent_id=private_top["id"]
        )
        await _share(db_session, common["id"])
        (tree / "Intern" / "Gemeinsam" / "Vorlagen").mkdir()

        await self._scan(async_client, auth_setup["admin_token"], private_top["id"])

        made = await _folder_row(db_session, name="Vorlagen")
        assert made.parent_id == common["id"]
        assert made.created_by_id == admin
        assert made.shared is True
        assert (await _folder_row(db_session, id=private_top["id"])).shared is False

    @pytest.mark.asyncio
    async def test_a_nested_chain_takes_each_level_from_the_one_above(
        self, async_client: AsyncClient, auth_setup, db_session, tree
    ):
        """Folders created in one walk inherit from the row just made, not the root."""
        await _directory_mode(db_session, tree)
        me = auth_setup["operator_user"]["id"]

        jobs = await self._create(async_client, auth_setup["admin_token"], name="Auftraege")
        await _share(db_session, jobs["id"])
        await self._create(async_client, auth_setup["operator_token"], name="B-Entwurf", parent_id=jobs["id"])
        (tree / "Auftraege" / "B-Entwurf" / "Neu" / "Teile").mkdir(parents=True)

        await self._scan(async_client, auth_setup["admin_token"], jobs["id"])

        new = await _folder_row(db_session, name="Neu")
        deeper = await _folder_row(db_session, name="Teile")
        assert deeper.parent_id == new.id
        assert (new.created_by_id, new.shared) == (me, False)
        assert (deeper.created_by_id, deeper.shared) == (me, False)


class TestRefreshVisibility(TestOwnershipPermissionsSetup):
    """Opening a folder is a folder route like any other: unseen means 404."""

    @pytest.mark.asyncio
    async def test_another_users_private_folder_is_404_and_not_scanned(
        self, async_client: AsyncClient, auth_setup, db_session, tree
    ):
        await _directory_mode(db_session, tree)
        theirs = (
            await async_client.post(
                "/api/v1/library/folders",
                json={"name": "Kunde X"},
                headers=_h(auth_setup["operator2_token"]),
            )
        ).json()
        (tree / "Kunde X" / "angebot.3mf").write_bytes(b"x")

        response = await async_client.post(
            f"/api/v1/library/folders/{theirs['id']}/refresh", headers=_h(auth_setup["operator_token"])
        )
        assert response.status_code == 404

        # Nothing was walked: no row for the file, and no throttle entry that
        # would hold the owner's own refresh back.
        db_session.expire_all()
        rows = (await db_session.execute(select(LibraryFile).where(LibraryFile.folder_id == theirs["id"]))).all()
        assert rows == []
        own = await async_client.post(
            f"/api/v1/library/folders/{theirs['id']}/refresh", headers=_h(auth_setup["operator2_token"])
        )
        assert own.status_code == 200, own.text
        assert own.json() == {"added": 1, "removed": 0, "skipped": None}

    @pytest.mark.asyncio
    async def test_a_missing_folder_answers_like_a_hidden_one(
        self, async_client: AsyncClient, auth_setup, db_session, tree
    ):
        await _directory_mode(db_session, tree)
        response = await async_client.post(
            "/api/v1/library/folders/999999/refresh", headers=_h(auth_setup["operator_token"])
        )
        assert response.status_code == 404

    @pytest.mark.asyncio
    async def test_visible_folders_still_refresh(self, async_client: AsyncClient, auth_setup, db_session, tree):
        await _directory_mode(db_session, tree)
        shared = (
            await async_client.post(
                "/api/v1/library/folders", json={"name": "Klasse"}, headers=_h(auth_setup["operator2_token"])
            )
        ).json()
        await _share(db_session, shared["id"])
        (tree / "Klasse" / "teil.3mf").write_bytes(b"x")

        response = await async_client.post(
            f"/api/v1/library/folders/{shared['id']}/refresh", headers=_h(auth_setup["operator_token"])
        )
        assert response.status_code == 200, response.text
        assert response.json()["added"] == 1

        # An admin sees everything and refreshes another user's private folder.
        private = (
            await async_client.post(
                "/api/v1/library/folders", json={"name": "Privat"}, headers=_h(auth_setup["operator2_token"])
            )
        ).json()
        admin = await async_client.post(
            f"/api/v1/library/folders/{private['id']}/refresh", headers=_h(auth_setup["admin_token"])
        )
        assert admin.status_code == 200, admin.text
        assert admin.json()["skipped"] is None


class TestPendingPreviewThumbnailsOwnership(TestOwnershipPermissionsSetup):
    """The browser-rendered preview queue lists only files the caller may open."""

    async def _pending(self, db_session, filename: str, file_type: str, owner: int | None) -> LibraryFile:
        row = LibraryFile(
            filename=filename,
            file_path=f"library/{filename}",
            file_type=file_type,
            file_size=1,
            created_by_id=owner,
        )
        db_session.add(row)
        await db_session.commit()
        await db_session.refresh(row)
        return row

    @pytest.mark.asyncio
    async def test_read_own_sees_only_their_own_pending_files(self, async_client: AsyncClient, auth_setup, db_session):
        me = auth_setup["operator_user"]["id"]
        other = auth_setup["operator2_user"]["id"]
        mine_step = await self._pending(db_session, "mein-teil.step", "step", me)
        mine_pdf = await self._pending(db_session, "meine-zeichnung.pdf", "pdf", me)
        theirs_step = await self._pending(db_session, "kunde-x.step", "step", other)
        theirs_pdf = await self._pending(db_session, "angebot-kunde-x.pdf", "pdf", other)
        ownerless = await self._pending(db_session, "alt.stp", "stp", None)

        response = await async_client.get(
            "/api/v1/library/files/pending-preview-thumbnails", headers=_h(auth_setup["operator_token"])
        )
        assert response.status_code == 200, response.text
        assert {f["id"] for f in response.json()} == {mine_step.id, mine_pdf.id}
        assert all(f["created_by_id"] == me for f in response.json())

        admin = await async_client.get(
            "/api/v1/library/files/pending-preview-thumbnails", headers=_h(auth_setup["admin_token"])
        )
        assert admin.status_code == 200, admin.text
        assert {f["id"] for f in admin.json()} == {
            mine_step.id,
            mine_pdf.id,
            theirs_step.id,
            theirs_pdf.id,
            ownerless.id,
        }

    @pytest.mark.asyncio
    async def test_other_users_rows_do_not_crowd_out_their_own(self, async_client: AsyncClient, auth_setup, db_session):
        """The limit applies after the owner filter, not before it."""
        me = auth_setup["operator_user"]["id"]
        other = auth_setup["operator2_user"]["id"]
        for n in range(3):
            await self._pending(db_session, f"fremd-{n}.step", "step", other)
        mine = await self._pending(db_session, "mein.step", "step", me)

        response = await async_client.get(
            "/api/v1/library/files/pending-preview-thumbnails?limit=1", headers=_h(auth_setup["operator_token"])
        )
        assert response.status_code == 200, response.text
        assert [f["id"] for f in response.json()] == [mine.id]
