"""WebDAV follows the File Manager's folder visibility and write rules (#3201).

A ``library:read_own`` user sees their own folders, shared ones, folders
holding one of their files and the parents leading there, and adds only to
their own and shared folders. Over the share a hidden folder is simply not
in the listing, so every path through it answers like a missing one; a
folder they can see but not add to refuses a new entry with 403.
"""

from __future__ import annotations

import base64
import xml.etree.ElementTree as ET
from pathlib import Path
from urllib.parse import unquote

import pytest
from httpx import AsyncClient
from sqlalchemy import select

from backend.app.core.config import settings as app_settings

pytestmark = [pytest.mark.asyncio, pytest.mark.integration]

WEBDAV = "/webdav"
PASSWORD = "DavPass1!"
WRITER_PERMISSIONS = ["library:read_own", "library:upload", "library:update_own", "library:delete_own"]


def _basic(username: str) -> dict[str, str]:
    token = base64.b64encode(f"{username}:{PASSWORD}".encode()).decode()
    return {"Authorization": f"Basic {token}"}


def _destination(path: str) -> str:
    return f"http://testserver{WEBDAV}{path}"


def _hrefs(body: bytes) -> set[str]:
    root = ET.fromstring(body)
    return {unquote(href.text or "") for href in root.iter("{DAV:}href")}


@pytest.fixture(autouse=True)
def library_root(monkeypatch, tmp_path) -> Path:
    """Keep every write of this file under tmp_path, as test_webdav_api does."""
    monkeypatch.setattr(app_settings, "base_dir", tmp_path)
    monkeypatch.setattr(app_settings, "archive_dir", tmp_path / "archive")
    return tmp_path


@pytest.fixture(autouse=True)
async def writable_webdav(db_session):
    from backend.app.models.settings import Settings

    db_session.add(Settings(key="webdav_mode", value="readwrite"))
    await db_session.commit()


@pytest.fixture
async def user_factory(db_session):
    async def _create(username: str, *, permissions: list[str] | None = None, is_admin: bool = False):
        from backend.app.core.auth import get_password_hash
        from backend.app.models.group import Group
        from backend.app.models.user import User

        user = User(username=username, password_hash=get_password_hash(PASSWORD), role="admin" if is_admin else "user")
        if permissions is not None:
            group = Group(name=f"grp_{username}", permissions=permissions)
            db_session.add(group)
            await db_session.flush()
            user.groups.append(group)
        db_session.add(user)
        await db_session.commit()
        await db_session.refresh(user)
        return user

    return _create


@pytest.fixture
def folder_factory(db_session):
    async def _create(
        name: str,
        *,
        owner=None,
        shared: bool = False,
        parent_id: int | None = None,
        external_path: Path | None = None,
    ):
        from backend.app.models.library import LibraryFolder

        folder = LibraryFolder(
            name=name,
            parent_id=parent_id,
            created_by_id=owner.id if owner is not None else None,
            shared=shared,
            is_external=external_path is not None,
            external_path=str(external_path) if external_path is not None else None,
        )
        db_session.add(folder)
        await db_session.commit()
        await db_session.refresh(folder)
        return folder

    return _create


@pytest.fixture
def file_factory(db_session, library_root):
    """A LibraryFile row with real bytes behind it, so MOVE and COPY can run."""
    counter = [0]

    async def _create(filename: str, *, owner, folder_id: int | None = None):
        from backend.app.models.library import LibraryFile

        counter[0] += 1
        relative = f"library/files/dav3201-{counter[0]}.txt"
        target = library_root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"payload")
        row = LibraryFile(
            filename=filename,
            file_path=relative,
            file_type="txt",
            file_size=7,
            folder_id=folder_id,
            created_by_id=owner.id,
        )
        db_session.add(row)
        await db_session.commit()
        await db_session.refresh(row)
        return row

    return _create


async def _file_rows(db_session, filename: str):
    """The rows as the requests left them.

    ``populate_existing`` rather than ``expire_all``: the test session never
    sees the route's commits on its own, and expiring everything would make the
    next ``folder.id`` a lazy load outside the async context.
    """
    from backend.app.models.library import LibraryFile

    query = select(LibraryFile).where(LibraryFile.filename == filename).execution_options(populate_existing=True)
    return (await db_session.execute(query)).scalars().all()


async def _folder_rows(db_session, name: str):
    from backend.app.models.library import LibraryFolder

    query = select(LibraryFolder).where(LibraryFolder.name == name).execution_options(populate_existing=True)
    return (await db_session.execute(query)).scalars().all()


class TestWebdavFolderAccess3201:
    @pytest.fixture
    async def scene(self, user_factory, folder_factory):
        """B's private folder, and C, who may add files but only reads their own."""
        owner = await user_factory("davowner", permissions=WRITER_PERMISSIONS)
        other = await user_factory("davother", permissions=WRITER_PERMISSIONS)
        private = await folder_factory("Kunde X", owner=owner)
        return owner, other, private

    # ---- visibility ---------------------------------------------------------

    async def test_another_users_private_folder_is_not_listed_and_404s(
        self, async_client: AsyncClient, scene, file_factory
    ):
        owner, _, private = scene
        await file_factory("theirs.txt", owner=owner, folder_id=private.id)
        auth = _basic("davother")

        listing = await async_client.request("PROPFIND", f"{WEBDAV}/Files", headers={**auth, "Depth": "1"})
        assert listing.status_code == 207, listing.text
        assert "/webdav/Files/Kunde X/" not in _hrefs(listing.content)

        for method, path in (
            ("PROPFIND", "/Files/Kunde X"),
            ("GET", "/Files/Kunde X/theirs.txt"),
            ("PROPPATCH", "/Files/Kunde X"),
        ):
            response = await async_client.request(method, f"{WEBDAV}{path}", headers={**auth, "Depth": "0"})
            assert response.status_code == 404, f"{method} {path} -> {response.status_code}"

        # Hidden by the rule, not lost: the owner still sees it.
        mine = await async_client.request("PROPFIND", f"{WEBDAV}/Files", headers={**_basic("davowner"), "Depth": "1"})
        assert "/webdav/Files/Kunde X/" in _hrefs(mine.content)

    async def test_a_hidden_top_level_external_folder_does_not_conjure_the_external_bucket(
        self, async_client: AsyncClient, scene, folder_factory, tmp_path, user_factory
    ):
        owner, _, _ = scene
        await folder_factory("NAS", owner=owner, external_path=tmp_path / "nas")
        await user_factory("davadmin", is_admin=True)

        root = await async_client.request("PROPFIND", f"{WEBDAV}/", headers={**_basic("davother"), "Depth": "1"})
        assert _hrefs(root.content) == {"/webdav/", "/webdav/Files/"}

        admin_root = await async_client.request("PROPFIND", f"{WEBDAV}/", headers={**_basic("davadmin"), "Depth": "1"})
        assert "/webdav/External/" in _hrefs(admin_root.content)

    # ---- writes into a hidden folder ----------------------------------------

    async def test_writes_into_a_hidden_folder_answer_like_a_missing_one(
        self, async_client: AsyncClient, scene, file_factory, db_session
    ):
        """409 "parent does not exist", exactly what a folder that is not there gets."""
        _, other, _ = scene
        loose = await file_factory("loose.txt", owner=other)
        auth = _basic("davother")

        for folder_name in ("Kunde X", "Nirgendwo"):
            put = await async_client.request(
                "PUT", f"{WEBDAV}/Files/{folder_name}/new.txt", headers=auth, content=b"mine"
            )
            assert put.status_code == 409, f"PUT into {folder_name}: {put.status_code}"

            mkcol = await async_client.request("MKCOL", f"{WEBDAV}/Files/{folder_name}/Sub", headers=auth)
            assert mkcol.status_code == 409, f"MKCOL into {folder_name}: {mkcol.status_code}"

            for method in ("MOVE", "COPY"):
                response = await async_client.request(
                    method,
                    f"{WEBDAV}/Files/loose.txt",
                    headers={**auth, "Destination": _destination(f"/Files/{folder_name}/loose.txt")},
                )
                assert response.status_code == 409, f"{method} into {folder_name}: {response.status_code}"

        assert await _file_rows(db_session, "new.txt") == []
        assert await _folder_rows(db_session, "Sub") == []
        rows = await _file_rows(db_session, "loose.txt")
        assert [(row.id, row.folder_id) for row in rows] == [(loose.id, None)]

    # ---- a folder that is visible but not writable --------------------------

    async def test_a_folder_seen_only_through_ones_own_file_refuses_new_entries(
        self, async_client: AsyncClient, scene, file_factory, db_session
    ):
        """Seen because C has a file in it, but still B's: no new entries, as in the File Manager."""
        _, other, private = scene
        await file_factory("inside.txt", owner=other, folder_id=private.id)
        await file_factory("loose.txt", owner=other)
        auth = _basic("davother")

        listing = await async_client.request("PROPFIND", f"{WEBDAV}/Files", headers={**auth, "Depth": "1"})
        assert "/webdav/Files/Kunde X/" in _hrefs(listing.content)

        put = await async_client.request("PUT", f"{WEBDAV}/Files/Kunde X/new.txt", headers=auth, content=b"mine")
        assert put.status_code == 403, put.text
        assert "your own folders" in put.json()["detail"]

        mkcol = await async_client.request("MKCOL", f"{WEBDAV}/Files/Kunde X/Sub", headers=auth)
        assert mkcol.status_code == 403, mkcol.text

        move_in = await async_client.request(
            "MOVE",
            f"{WEBDAV}/Files/loose.txt",
            headers={**auth, "Destination": _destination("/Files/Kunde X/loose.txt")},
        )
        assert move_in.status_code == 403, move_in.text

        copy_beside = await async_client.request(
            "COPY",
            f"{WEBDAV}/Files/Kunde X/inside.txt",
            headers={**auth, "Destination": _destination("/Files/Kunde X/copy.txt")},
        )
        assert copy_beside.status_code == 403, copy_beside.text

        assert await _file_rows(db_session, "new.txt") == []
        assert await _file_rows(db_session, "copy.txt") == []
        assert await _folder_rows(db_session, "Sub") == []
        assert [row.folder_id for row in await _file_rows(db_session, "loose.txt")] == [None]

        # Renaming their own file where it already is adds nothing to the
        # folder, and the File Manager's rename does not ask about it either.
        rename = await async_client.request(
            "MOVE",
            f"{WEBDAV}/Files/Kunde X/inside.txt",
            headers={**auth, "Destination": _destination("/Files/Kunde X/renamed.txt")},
        )
        assert rename.status_code == 201, rename.text
        assert [row.folder_id for row in await _file_rows(db_session, "renamed.txt")] == [private.id]

    async def test_replacing_ones_own_file_there_adds_nothing_and_is_allowed(
        self, async_client: AsyncClient, scene, file_factory, db_session
    ):
        """A PUT, COPY or MOVE onto C's existing file in B's folder only changes that row.

        The File Manager lets C update their own file wherever it sits, and a
        save that writes a temporary file elsewhere and lands it on the real
        name is the same update arriving as a MOVE.
        """
        _, other, private = scene
        inside = await file_factory("inside.txt", owner=other, folder_id=private.id)
        await file_factory("draft.txt", owner=other)
        await file_factory("final.txt", owner=other)
        auth = _basic("davother")
        target = _destination("/Files/Kunde X/inside.txt")

        put = await async_client.request("PUT", f"{WEBDAV}/Files/Kunde X/inside.txt", headers=auth, content=b"v2")
        assert put.status_code == 204, put.text

        copy = await async_client.request("COPY", f"{WEBDAV}/Files/draft.txt", headers={**auth, "Destination": target})
        assert copy.status_code == 204, copy.text

        move = await async_client.request("MOVE", f"{WEBDAV}/Files/final.txt", headers={**auth, "Destination": target})
        assert move.status_code == 204, move.text

        rows = await _file_rows(db_session, "inside.txt")
        assert [(row.id, row.folder_id) for row in rows] == [(inside.id, private.id)]
        assert await _file_rows(db_session, "final.txt") == []

    async def test_a_folder_moves_only_into_a_folder_its_mover_may_add_to(
        self, async_client: AsyncClient, scene, user_factory, folder_factory, file_factory, db_session
    ):
        """The collection MOVE runs the same gate, ``library:update_all`` or not."""
        owner, _, private = scene
        mover = await user_factory("davmover", permissions=[*WRITER_PERMISSIONS, "library:update_all"])
        await file_factory("inside.txt", owner=mover, folder_id=private.id)
        shared = await folder_factory("Gemeinsam", owner=owner, shared=True)
        mine = await folder_factory("Meins", owner=mover)
        auth = _basic("davmover")

        refused = await async_client.request(
            "MOVE", f"{WEBDAV}/Files/Meins", headers={**auth, "Destination": _destination("/Files/Kunde X/Meins")}
        )
        assert refused.status_code == 403, refused.text
        assert [row.parent_id for row in await _folder_rows(db_session, "Meins")] == [None]

        moved = await async_client.request(
            "MOVE", f"{WEBDAV}/Files/Meins", headers={**auth, "Destination": _destination("/Files/Gemeinsam/Meins")}
        )
        assert moved.status_code == 201, moved.text
        rows = await _folder_rows(db_session, "Meins")
        assert [(row.id, row.parent_id) for row in rows] == [(mine.id, shared.id)]

    # ---- admins, shared folders and one's own -------------------------------

    async def test_an_admin_still_sees_and_writes_into_any_folder(
        self, async_client: AsyncClient, scene, user_factory, db_session
    ):
        _, _, private = scene
        await user_factory("davadmin", is_admin=True)
        auth = _basic("davadmin")

        listing = await async_client.request("PROPFIND", f"{WEBDAV}/Files", headers={**auth, "Depth": "1"})
        assert "/webdav/Files/Kunde X/" in _hrefs(listing.content)

        put = await async_client.request("PUT", f"{WEBDAV}/Files/Kunde X/new.txt", headers=auth, content=b"admin")
        assert put.status_code == 201, put.text
        mkcol = await async_client.request("MKCOL", f"{WEBDAV}/Files/Kunde X/Sub", headers=auth)
        assert mkcol.status_code == 201, mkcol.text

        assert [row.folder_id for row in await _file_rows(db_session, "new.txt")] == [private.id]
        assert [row.parent_id for row in await _folder_rows(db_session, "Sub")] == [private.id]

    async def test_a_shared_folder_stays_visible_and_writable(
        self, async_client: AsyncClient, scene, folder_factory, db_session
    ):
        owner, _, private = scene
        shared = await folder_factory("Gemeinsam", owner=owner, shared=True)
        # Shared, but inside B's private folder: the parent becomes visible for
        # navigation only.
        nested = await folder_factory("Vorlagen", owner=owner, shared=True, parent_id=private.id)
        auth = _basic("davother")

        listing = await async_client.request("PROPFIND", f"{WEBDAV}/Files", headers={**auth, "Depth": "1"})
        hrefs = _hrefs(listing.content)
        assert {"/webdav/Files/Gemeinsam/", "/webdav/Files/Kunde X/"} <= hrefs

        inside = await async_client.request("PROPFIND", f"{WEBDAV}/Files/Kunde X", headers={**auth, "Depth": "1"})
        assert "/webdav/Files/Kunde X/Vorlagen/" in _hrefs(inside.content)

        for folder, path in ((shared, "/Files/Gemeinsam"), (nested, "/Files/Kunde X/Vorlagen")):
            put = await async_client.request("PUT", f"{WEBDAV}{path}/c-{folder.id}.txt", headers=auth, content=b"c")
            assert put.status_code == 201, f"PUT into {path}: {put.text}"
            mkcol = await async_client.request("MKCOL", f"{WEBDAV}{path}/Sub-{folder.id}", headers=auth)
            assert mkcol.status_code == 201, f"MKCOL into {path}: {mkcol.text}"
            assert [row.folder_id for row in await _file_rows(db_session, f"c-{folder.id}.txt")] == [folder.id]

        # The navigation-only parent itself still takes nothing new.
        refused = await async_client.request("PUT", f"{WEBDAV}/Files/Kunde X/c.txt", headers=auth, content=b"c")
        assert refused.status_code == 403, refused.text

    async def test_a_folder_made_over_the_share_is_the_makers_to_fill(
        self, async_client: AsyncClient, scene, db_session
    ):
        auth = _basic("davother")

        mkcol = await async_client.request("MKCOL", f"{WEBDAV}/Files/Meins", headers=auth)
        assert mkcol.status_code == 201, mkcol.text
        put = await async_client.request("PUT", f"{WEBDAV}/Files/Meins/a.txt", headers=auth, content=b"a")
        assert put.status_code == 201, put.text

        # And it is private to its maker, so B does not see it.
        theirs = await async_client.request("PROPFIND", f"{WEBDAV}/Files", headers={**_basic("davowner"), "Depth": "1"})
        assert "/webdav/Files/Meins/" not in _hrefs(theirs.content)

    async def test_a_name_a_hidden_row_claims_in_a_shared_external_folder_is_refused(
        self, async_client: AsyncClient, scene, folder_factory, db_session, tmp_path
    ):
        """test_webdav_api's hidden-row case, in a folder the caller may write to.

        There the folder has no owner and is not shared, so since #3201 the
        caller cannot reach it at all; here it is shared, and the only thing
        between the caller and somebody else's bytes is the row they cannot see.
        """
        from backend.app.models.library import LibraryFile

        owner, _, _ = scene
        directory = tmp_path / "share" / "NAS"
        directory.mkdir(parents=True)
        folder = await folder_factory("NAS", owner=owner, shared=True, external_path=directory)
        (directory / "part.3mf").write_bytes(b"someone else's")
        db_session.add(
            LibraryFile(
                filename="part.3mf",
                file_path=str(directory / "part.3mf"),
                file_type="3mf",
                file_size=14,
                folder_id=folder.id,
                is_external=True,
                created_by_id=owner.id,
            )
        )
        await db_session.commit()

        response = await async_client.request(
            "PUT", f"{WEBDAV}/External/NAS/part.3mf", headers=_basic("davother"), content=b"mine now"
        )

        assert response.status_code == 409, response.text
        assert (directory / "part.3mf").read_bytes() == b"someone else's"
