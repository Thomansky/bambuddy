"""Integration tests for the read-only WebDAV projection of the library (#3152).

A WebDAV client either speaks the protocol exactly or shows an empty drive, so
these tests go at the wire: they send real PROPFINDs and parse the XML that
comes back rather than trusting a Python-level shape. The href encoding in
particular is pinned with the names that break it in the field — a space, an
umlaut and an ``&``.
"""

from __future__ import annotations

import base64
import xml.etree.ElementTree as ET
from datetime import datetime
from pathlib import Path
from urllib.parse import unquote

import pytest
from httpx import AsyncClient
from sqlalchemy import select

from backend.app.core.config import settings as app_settings

pytestmark = [pytest.mark.asyncio, pytest.mark.integration]

DAV = "{DAV:}"
WEBDAV = "/webdav"


def _basic(username: str, password: str) -> dict[str, str]:
    token = base64.b64encode(f"{username}:{password}".encode()).decode()
    return {"Authorization": f"Basic {token}"}


def _responses(body: bytes) -> dict[str, ET.Element]:
    """Map every ``<D:response>`` in a multistatus by its (decoded) href."""
    root = ET.fromstring(body)
    assert root.tag == f"{DAV}multistatus", root.tag
    out: dict[str, ET.Element] = {}
    for response in root.findall(f"{DAV}response"):
        href = response.findtext(f"{DAV}href")
        assert href is not None
        out[unquote(href)] = response
    return out


def _hrefs(body: bytes) -> list[str]:
    root = ET.fromstring(body)
    return [href.text or "" for href in root.iter(f"{DAV}href")]


def _prop(response: ET.Element, name: str) -> ET.Element | None:
    return response.find(f"{DAV}propstat/{DAV}prop/{DAV}{name}")


def _text(response: ET.Element, name: str) -> str | None:
    element = _prop(response, name)
    return None if element is None else element.text


def _is_collection(response: ET.Element) -> bool:
    resourcetype = _prop(response, "resourcetype")
    assert resourcetype is not None
    return resourcetype.find(f"{DAV}collection") is not None


@pytest.fixture
def library_root(monkeypatch, tmp_path) -> Path:
    """Point the library's path helpers at a throwaway data dir."""
    monkeypatch.setattr(app_settings, "base_dir", tmp_path)
    return tmp_path


@pytest.fixture
async def set_webdav_mode(db_session):
    """Put the share in one of its three modes."""

    async def _set(mode: str):
        from sqlalchemy import select

        from backend.app.models.settings import Settings

        row = (await db_session.execute(select(Settings).where(Settings.key == "webdav_mode"))).scalar_one_or_none()
        if row is None:
            db_session.add(Settings(key="webdav_mode", value=mode))
        else:
            row.value = mode
        await db_session.commit()

    return _set


@pytest.fixture
async def enable_webdav(set_webdav_mode):
    """The read-only share, which is what the read half is about."""
    await set_webdav_mode("read")


@pytest.fixture
async def writable_webdav(set_webdav_mode):
    await set_webdav_mode("readwrite")


@pytest.fixture
async def user_factory(db_session):
    """Create a user with a real password hash, optionally in a permission group."""

    async def _create(username: str, *, permissions: list[str] | None = None, is_admin: bool = False):
        from backend.app.core.auth import get_password_hash
        from backend.app.models.group import Group
        from backend.app.models.user import User

        # ``is_admin`` is a derived property (legacy role or Administrators
        # membership), so the role is what a test can actually set.
        user = User(
            username=username,
            password_hash=get_password_hash("DavPass1!"),
            role="admin" if is_admin else "user",
        )
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
async def admin(user_factory):
    return await user_factory("davadmin", is_admin=True)


@pytest.fixture
def admin_auth(admin) -> dict[str, str]:
    return _basic("davadmin", "DavPass1!")


@pytest.fixture
def folder_factory(db_session):
    async def _create(name: str, parent_id: int | None = None, *, is_external: bool = False):
        from backend.app.models.library import LibraryFolder

        folder = LibraryFolder(
            name=name,
            parent_id=parent_id,
            is_external=is_external,
            external_path="/mnt/share" if is_external else None,
        )
        db_session.add(folder)
        await db_session.commit()
        await db_session.refresh(folder)
        return folder

    return _create


@pytest.fixture
def file_factory(db_session, library_root):
    """Create a LibraryFile row, by default with real bytes behind it."""
    counter = [0]

    async def _create(
        filename: str,
        content: bytes = b"payload",
        *,
        folder_id: int | None = None,
        created_by_id: int | None = None,
        on_disk: bool = True,
        is_external: bool = False,
        deleted_at=None,
    ):
        from backend.app.models.library import LibraryFile

        counter[0] += 1
        relative = f"library/files/dav-{counter[0]}.bin"
        if on_disk:
            target = library_root / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(content)

        row = LibraryFile(
            filename=filename,
            file_path=relative,
            file_type=filename.rsplit(".", 1)[-1][:10],
            file_size=len(content),
            folder_id=folder_id,
            created_by_id=created_by_id,
            is_external=is_external,
            deleted_at=deleted_at,
        )
        db_session.add(row)
        await db_session.commit()
        await db_session.refresh(row)
        return row

    return _create


class TestProtocolSurface:
    """OPTIONS, the method allowlist, and what a write attempt gets."""

    async def test_options_advertises_dav_1_and_exactly_four_methods(
        self, async_client: AsyncClient, enable_webdav, admin_auth
    ):
        response = await async_client.request("OPTIONS", WEBDAV, headers=admin_auth)

        assert response.status_code == 200, response.text
        assert response.headers["DAV"] == "1"
        assert response.headers["MS-Author-Via"] == "DAV"
        allowed = {method.strip() for method in response.headers["Allow"].split(",")}
        assert allowed == {"OPTIONS", "PROPFIND", "HEAD", "GET"}

    @pytest.mark.parametrize(
        "method",
        ["PUT", "DELETE", "MKCOL", "MOVE", "COPY", "PROPPATCH", "LOCK", "UNLOCK"],
    )
    async def test_write_methods_answer_405_with_allow(
        self, async_client: AsyncClient, enable_webdav, admin_auth, method
    ):
        response = await async_client.request(method, f"{WEBDAV}/Files", headers=admin_auth)

        assert response.status_code == 405, response.text
        allowed = {value.strip() for value in response.headers["Allow"].split(",")}
        assert allowed == {"OPTIONS", "PROPFIND", "HEAD", "GET"}

    @pytest.mark.parametrize("method", ["POST", "PATCH", "REPORT", "SEARCH", "MKCALENDAR", "XYZZY"])
    @pytest.mark.parametrize("path", [WEBDAV, f"{WEBDAV}/Files"])
    async def test_a_method_the_router_never_named_gets_the_same_405(
        self, async_client: AsyncClient, enable_webdav, admin_auth, method, path
    ):
        """Not only the eight write verbs.

        Left to the framework these answer a 405 built from the first route
        whose path matched, i.e. ``Allow: OPTIONS`` — which tells a versioning
        client probing with REPORT that the share supports neither PROPFIND nor
        GET.
        """
        response = await async_client.request(method, path, headers=admin_auth)

        assert response.status_code == 405, response.text
        allowed = {value.strip() for value in response.headers["Allow"].split(",")}
        assert allowed == {"OPTIONS", "PROPFIND", "HEAD", "GET"}

    async def test_the_schema_still_builds_and_says_nothing_about_webdav(self, async_client: AsyncClient):
        """PROPFIND is not an OpenAPI operation, and the builder is fragile.

        The route that catches an unnamed method has to keep a declared method
        list, because FastAPI's schema builder asserts on it before it ever
        looks at ``include_in_schema`` — clearing the list instead of
        overriding the match takes ``/openapi.json`` down with a 500.
        """
        response = await async_client.get("/openapi.json")

        assert response.status_code == 200, response.text
        assert [path for path in response.json()["paths"] if path.startswith("/webdav")] == []

    async def test_depth_infinity_is_refused_with_the_finite_depth_precondition(
        self, async_client: AsyncClient, enable_webdav, admin_auth
    ):
        response = await async_client.request("PROPFIND", f"{WEBDAV}/", headers={**admin_auth, "Depth": "infinity"})

        assert response.status_code == 403, response.text
        error = ET.fromstring(response.content)
        assert error.tag == f"{DAV}error"
        assert error.find(f"{DAV}propfind-finite-depth") is not None


class TestPropfindTree:
    """What the projection lists, and whether the XML carries every property."""

    async def test_depth_0_on_root_describes_only_the_root(
        self, async_client: AsyncClient, enable_webdav, admin_auth, folder_factory
    ):
        await folder_factory("Kunden")

        response = await async_client.request("PROPFIND", f"{WEBDAV}/", headers={**admin_auth, "Depth": "0"})

        assert response.status_code == 207, response.text
        assert response.headers["content-type"].startswith("application/xml")
        rows = _responses(response.content)
        assert list(rows) == ["/webdav/"]
        assert _is_collection(rows["/webdav/"])

    async def test_depth_1_on_root_lists_the_managed_bucket(
        self, async_client: AsyncClient, enable_webdav, admin_auth, folder_factory
    ):
        await folder_factory("Kunden")

        response = await async_client.request("PROPFIND", f"{WEBDAV}/", headers={**admin_auth, "Depth": "1"})

        rows = _responses(response.content)
        assert set(rows) == {"/webdav/", "/webdav/Files/"}
        assert _text(rows["/webdav/Files/"], "displayname") == "Files"

    async def test_external_bucket_appears_only_when_an_external_folder_exists(
        self, async_client: AsyncClient, enable_webdav, admin_auth, folder_factory
    ):
        before = await async_client.request("PROPFIND", f"{WEBDAV}/", headers={**admin_auth, "Depth": "1"})
        assert "/webdav/External/" not in _responses(before.content)

        await folder_factory("NAS", is_external=True)

        after = await async_client.request("PROPFIND", f"{WEBDAV}/", headers={**admin_auth, "Depth": "1"})
        rows = _responses(after.content)
        assert set(rows) == {"/webdav/", "/webdav/Files/", "/webdav/External/"}
        assert _is_collection(rows["/webdav/External/"])

    async def test_the_two_buckets_keep_managed_and_external_apart(
        self, async_client: AsyncClient, enable_webdav, admin_auth, folder_factory, file_factory
    ):
        """Including the loose files, which the File Manager counts per bucket too."""
        await folder_factory("Kunden")
        await folder_factory("NAS", is_external=True)
        await file_factory("managed-loose.3mf")
        await file_factory("scanned-loose.3mf", is_external=True)

        managed = await async_client.request("PROPFIND", f"{WEBDAV}/Files", headers={**admin_auth, "Depth": "1"})
        external = await async_client.request("PROPFIND", f"{WEBDAV}/External", headers={**admin_auth, "Depth": "1"})

        assert set(_responses(managed.content)) == {
            "/webdav/Files/",
            "/webdav/Files/Kunden/",
            "/webdav/Files/managed-loose.3mf",
        }
        assert set(_responses(external.content)) == {
            "/webdav/External/",
            "/webdav/External/NAS/",
            "/webdav/External/scanned-loose.3mf",
        }

    async def test_loose_external_files_alone_still_open_the_external_bucket(
        self, async_client: AsyncClient, enable_webdav, admin_auth, file_factory
    ):
        """With no external folder at all, those files would have no path."""
        await file_factory("scanned-loose.3mf", is_external=True)

        response = await async_client.request("PROPFIND", f"{WEBDAV}/", headers={**admin_auth, "Depth": "1"})

        assert "/webdav/External/" in _responses(response.content)

    async def test_depth_1_on_a_folder_lists_subfolders_and_files(
        self, async_client: AsyncClient, enable_webdav, admin_auth, folder_factory, file_factory
    ):
        parent = await folder_factory("Kunden")
        await folder_factory("Angebote", parent_id=parent.id)
        await file_factory("part.3mf", b"3mf-bytes", folder_id=parent.id)

        response = await async_client.request(
            "PROPFIND", f"{WEBDAV}/Files/Kunden", headers={**admin_auth, "Depth": "1"}
        )

        rows = _responses(response.content)
        assert set(rows) == {
            "/webdav/Files/Kunden/",
            "/webdav/Files/Kunden/Angebote/",
            "/webdav/Files/Kunden/part.3mf",
        }
        assert _is_collection(rows["/webdav/Files/Kunden/Angebote/"])
        assert not _is_collection(rows["/webdav/Files/Kunden/part.3mf"])

    async def test_every_required_property_is_present_and_well_formed(
        self, async_client: AsyncClient, enable_webdav, admin_auth, folder_factory, file_factory
    ):
        folder = await folder_factory("Kunden")
        await file_factory("part.3mf", b"0123456789", folder_id=folder.id)

        response = await async_client.request(
            "PROPFIND", f"{WEBDAV}/Files/Kunden", headers={**admin_auth, "Depth": "1"}
        )

        rows = _responses(response.content)
        for href, entry in rows.items():
            assert _text(entry, "displayname") is not None, href
            assert _prop(entry, "resourcetype") is not None, href
            assert _text(entry, "getcontentlength") is not None, href
            # RFC 1123, GMT — Explorer parses nothing else.
            assert (_text(entry, "getlastmodified") or "").endswith(" GMT"), href
            assert (_text(entry, "creationdate") or "").endswith("Z"), href
            status = entry.findtext(f"{DAV}propstat/{DAV}status")
            assert status == "HTTP/1.1 200 OK", href

        assert _text(rows["/webdav/Files/Kunden/part.3mf"], "getcontentlength") == "10"

    async def test_unfoldered_files_sit_directly_in_the_managed_bucket(
        self, async_client: AsyncClient, enable_webdav, admin_auth, file_factory
    ):
        await file_factory("loose.3mf", b"loose")

        response = await async_client.request("PROPFIND", f"{WEBDAV}/Files", headers={**admin_auth, "Depth": "1"})

        assert "/webdav/Files/loose.3mf" in _responses(response.content)

    async def test_trashed_files_are_absent(
        self, async_client: AsyncClient, enable_webdav, admin_auth, folder_factory, file_factory
    ):
        folder = await folder_factory("Kunden")
        await file_factory("kept.3mf", folder_id=folder.id)
        trashed = await file_factory("gone.3mf", folder_id=folder.id, deleted_at=datetime(2026, 1, 1))

        response = await async_client.request(
            "PROPFIND", f"{WEBDAV}/Files/Kunden", headers={**admin_auth, "Depth": "1"}
        )

        rows = _responses(response.content)
        assert "/webdav/Files/Kunden/kept.3mf" in rows
        assert "/webdav/Files/Kunden/gone.3mf" not in rows

        # ...and its bytes are unreachable too, not merely unlisted.
        assert trashed.deleted_at is not None
        direct = await async_client.get(f"{WEBDAV}/Files/Kunden/gone.3mf", headers=admin_auth)
        assert direct.status_code == 404

    async def test_a_request_without_a_depth_header_is_served_as_depth_1(
        self, async_client: AsyncClient, enable_webdav, admin_auth, folder_factory
    ):
        """No client omits Depth, but a drive showing one level beats one showing an error."""
        await folder_factory("Kunden")

        response = await async_client.request("PROPFIND", f"{WEBDAV}/", headers=admin_auth)

        assert response.status_code == 207, response.text
        assert set(_responses(response.content)) == {"/webdav/", "/webdav/Files/"}

    async def test_the_root_carries_a_displayname(self, async_client: AsyncClient, enable_webdav, admin_auth):
        response = await async_client.request("PROPFIND", f"{WEBDAV}/", headers={**admin_auth, "Depth": "0"})

        assert _text(_responses(response.content)["/webdav/"], "displayname") == "Bambuddy"

    async def test_a_missing_path_is_404(self, async_client: AsyncClient, enable_webdav, admin_auth):
        response = await async_client.request("PROPFIND", f"{WEBDAV}/Files/Nope", headers={**admin_auth, "Depth": "1"})
        assert response.status_code == 404


class TestHrefEncoding:
    """The single likeliest reason Explorer shows an empty drive."""

    async def test_space_umlaut_and_ampersand_round_trip_through_the_href(
        self, async_client: AsyncClient, enable_webdav, admin_auth, folder_factory, file_factory
    ):
        folder = await folder_factory("Kunden & Co")
        await file_factory("Stübe V60 -H2D.3mf", b"awkward", folder_id=folder.id)

        listing = await async_client.request("PROPFIND", f"{WEBDAV}/Files", headers={**admin_auth, "Depth": "1"})
        folder_href = next(href for href in _hrefs(listing.content) if href != "/webdav/Files/")
        assert folder_href == "/webdav/Files/Kunden%20%26%20Co/"

        # The href a client would follow, used verbatim.
        inner = await async_client.request("PROPFIND", folder_href, headers={**admin_auth, "Depth": "1"})
        assert inner.status_code == 207, inner.text
        file_href = next(href for href in _hrefs(inner.content) if href != folder_href)
        assert file_href == "/webdav/Files/Kunden%20%26%20Co/St%C3%BCbe%20V60%20-H2D.3mf"

        bytes_response = await async_client.get(file_href, headers=admin_auth)
        assert bytes_response.status_code == 200, bytes_response.text
        assert bytes_response.content == b"awkward"
        assert _responses(inner.content)["/webdav/Files/Kunden & Co/Stübe V60 -H2D.3mf"] is not None


class TestBytes:
    """GET, HEAD and Range."""

    async def test_get_returns_the_bytes_with_the_right_length(
        self, async_client: AsyncClient, enable_webdav, admin_auth, file_factory
    ):
        await file_factory("part.3mf", b"0123456789")

        response = await async_client.get(f"{WEBDAV}/Files/part.3mf", headers=admin_auth)

        assert response.status_code == 200, response.text
        assert response.content == b"0123456789"
        assert response.headers["content-length"] == "10"

    async def test_head_answers_the_headers_without_the_body(
        self, async_client: AsyncClient, enable_webdav, admin_auth, file_factory
    ):
        await file_factory("part.3mf", b"0123456789")

        response = await async_client.head(f"{WEBDAV}/Files/part.3mf", headers=admin_auth)

        assert response.status_code == 200, response.text
        assert response.headers["content-length"] == "10"
        assert response.content == b""

    async def test_a_single_range_request_returns_206_and_the_right_slice(
        self, async_client: AsyncClient, enable_webdav, admin_auth, file_factory
    ):
        await file_factory("part.3mf", b"0123456789")

        response = await async_client.get(f"{WEBDAV}/Files/part.3mf", headers={**admin_auth, "Range": "bytes=2-5"})

        assert response.status_code == 206, response.text
        assert response.content == b"2345"
        assert response.headers["content-range"] == "bytes 2-5/10"

    async def test_a_row_whose_bytes_are_gone_is_404_not_500(
        self, async_client: AsyncClient, enable_webdav, admin_auth, file_factory
    ):
        await file_factory("ghost.3mf", on_disk=False)

        response = await async_client.get(f"{WEBDAV}/Files/ghost.3mf", headers=admin_auth)

        assert response.status_code == 404, response.text

    async def test_get_on_a_collection_is_405(
        self, async_client: AsyncClient, enable_webdav, admin_auth, folder_factory
    ):
        await folder_factory("Kunden")

        response = await async_client.get(f"{WEBDAV}/Files/Kunden", headers=admin_auth)

        assert response.status_code == 405, response.text

    @pytest.mark.parametrize("filename", ["pwn.html", "pwn.js", "pwn.svg"])
    async def test_a_renderable_upload_is_served_inert_not_as_its_extension(
        self, async_client: AsyncClient, enable_webdav, admin_auth, file_factory, filename
    ):
        """The library takes any extension, so the share must not render any.

        ``mimetypes`` would call these text/html, application/javascript and
        image/svg+xml, all of which run script on Bambuddy's own origin, where
        ``script-src 'self'`` allows them and the session token sits in web
        storage. ``download_file`` forces an octet-stream attachment for the
        same rows and so does this.
        """
        await file_factory(filename, b"<script>alert(1)</script>")

        response = await async_client.get(f"{WEBDAV}/Files/{filename}", headers=admin_auth)

        assert response.status_code == 200, response.text
        assert response.headers["content-type"] == "application/octet-stream"
        assert response.headers["content-disposition"].startswith("attachment")
        assert filename in response.headers["content-disposition"]
        assert response.headers["x-content-type-options"] == "nosniff"

    async def test_the_guessed_type_survives_as_a_propfind_property(
        self, async_client: AsyncClient, enable_webdav, admin_auth, file_factory
    ):
        """A property names the owning application; only a header executes."""
        await file_factory("part.3mf", b"bytes")

        listing = await async_client.request("PROPFIND", f"{WEBDAV}/Files", headers={**admin_auth, "Depth": "1"})

        assert _text(_responses(listing.content)["/webdav/Files/part.3mf"], "getcontenttype") == "model/3mf"

    async def test_propfind_describes_the_bytes_get_will_hand_over(
        self, async_client: AsyncClient, enable_webdav, admin_auth, file_factory, db_session
    ):
        """A scanned external row goes stale the moment the share is rewritten.

        rclone compares the PROPFIND size against the transfer and deletes a
        copy whose sizes differ; a client resuming a download offers the
        PROPFIND date as ``If-Range`` and silently restarts when it does not
        validate. Both need the listing and the transfer to agree.
        """
        row = await file_factory("part.3mf", b"0123456789", is_external=True)
        row.file_size = 3
        row.fs_modified_at = datetime(2020, 1, 1)
        db_session.add(row)
        await db_session.commit()

        listing = await async_client.request("PROPFIND", f"{WEBDAV}/External", headers={**admin_auth, "Depth": "1"})
        entry = _responses(listing.content)["/webdav/External/part.3mf"]
        transfer = await async_client.head(f"{WEBDAV}/External/part.3mf", headers=admin_auth)

        assert _text(entry, "getcontentlength") == transfer.headers["content-length"] == "10"
        assert _text(entry, "getlastmodified") == transfer.headers["last-modified"]

    async def test_a_row_with_no_bytes_still_lists_with_the_size_it_recorded(
        self, async_client: AsyncClient, enable_webdav, admin_auth, file_factory
    ):
        """Nothing to stat, so the row is all the listing has to go on."""
        await file_factory("ghost.3mf", b"0123456789", on_disk=False)

        listing = await async_client.request("PROPFIND", f"{WEBDAV}/Files", headers={**admin_auth, "Depth": "1"})

        assert _text(_responses(listing.content)["/webdav/Files/ghost.3mf"], "getcontentlength") == "10"


class TestStableNames:
    """Two files with one name must not swap places between requests."""

    async def test_same_named_files_get_stable_distinct_names(
        self, async_client: AsyncClient, enable_webdav, admin_auth, folder_factory, file_factory
    ):
        folder = await folder_factory("Kunden")
        first = await file_factory("part.3mf", b"first", folder_id=folder.id)
        second = await file_factory("part.3mf", b"second", folder_id=folder.id)
        assert first.id < second.id

        seen = []
        for _ in range(2):
            response = await async_client.request(
                "PROPFIND", f"{WEBDAV}/Files/Kunden", headers={**admin_auth, "Depth": "1"}
            )
            rows = _responses(response.content)
            seen.append({href for href in rows if not href.endswith("/")})

        assert seen[0] == seen[1]
        assert seen[0] == {"/webdav/Files/Kunden/part.3mf", "/webdav/Files/Kunden/part (2).3mf"}

        # The suffix is attached by id order, so the second row is the second name.
        original = await async_client.get(f"{WEBDAV}/Files/Kunden/part.3mf", headers=admin_auth)
        duplicate = await async_client.get(f"{WEBDAV}/Files/Kunden/part%20(2).3mf", headers=admin_auth)
        assert original.content == b"first"
        assert duplicate.content == b"second"

    async def test_a_later_folder_of_the_same_name_does_not_rename_the_file(
        self, async_client: AsyncClient, enable_webdav, admin_auth, folder_factory, file_factory, db_session
    ):
        """The newcomer takes the suffix, whichever kind it is.

        Disambiguating folders first would hand the bare name to the new folder
        and move the file to ``part (2).3mf`` without the file row changing at
        all — the path a slicer had open would start answering 405.
        """
        parent = await folder_factory("Kunden")
        await file_factory("part.3mf", b"the-model", folder_id=parent.id)

        collider = await folder_factory("part.3mf", parent_id=parent.id)
        collider.created_at = datetime(2099, 1, 1)
        db_session.add(collider)
        await db_session.commit()

        response = await async_client.request(
            "PROPFIND", f"{WEBDAV}/Files/Kunden", headers={**admin_auth, "Depth": "1"}
        )

        assert set(_responses(response.content)) == {
            "/webdav/Files/Kunden/",
            "/webdav/Files/Kunden/part.3mf",
            "/webdav/Files/Kunden/part (2).3mf/",
        }
        bytes_response = await async_client.get(f"{WEBDAV}/Files/Kunden/part.3mf", headers=admin_auth)
        assert bytes_response.status_code == 200, bytes_response.text
        assert bytes_response.content == b"the-model"


class TestAuthAndPermissions:
    """Authentication is the first requirement, not a detail."""

    async def test_unauthenticated_is_401_with_a_basic_challenge(self, async_client: AsyncClient, enable_webdav):
        response = await async_client.request("PROPFIND", f"{WEBDAV}/", headers={"Depth": "1"})

        assert response.status_code == 401, response.text
        assert response.headers["WWW-Authenticate"] == 'Basic realm="Bambuddy"'

    async def test_a_wrong_password_is_401(self, async_client: AsyncClient, enable_webdav, admin):
        response = await async_client.request(
            "PROPFIND", f"{WEBDAV}/", headers={**_basic("davadmin", "wrong"), "Depth": "1"}
        )
        assert response.status_code == 401, response.text

    async def test_a_user_without_library_read_is_403(self, async_client: AsyncClient, enable_webdav, user_factory):
        await user_factory("nolib", permissions=["printers:read"])

        response = await async_client.request(
            "PROPFIND", f"{WEBDAV}/", headers={**_basic("nolib", "DavPass1!"), "Depth": "1"}
        )

        assert response.status_code == 403, response.text

    async def test_read_own_sees_neither_a_foreign_entry_nor_its_bytes(
        self, async_client: AsyncClient, enable_webdav, user_factory, folder_factory, file_factory
    ):
        owner = await user_factory("owner", permissions=["library:read_own"])
        other = await user_factory("other", permissions=["library:read_own"])
        folder = await folder_factory("Kunden")
        await file_factory("mine.3mf", b"mine", folder_id=folder.id, created_by_id=owner.id)
        await file_factory("theirs.3mf", b"theirs", folder_id=folder.id, created_by_id=other.id)

        auth = _basic("owner", "DavPass1!")
        listing = await async_client.request("PROPFIND", f"{WEBDAV}/Files/Kunden", headers={**auth, "Depth": "1"})

        rows = _responses(listing.content)
        assert "/webdav/Files/Kunden/mine.3mf" in rows
        assert "/webdav/Files/Kunden/theirs.3mf" not in rows

        bytes_response = await async_client.get(f"{WEBDAV}/Files/Kunden/theirs.3mf", headers=auth)
        assert bytes_response.status_code == 404, bytes_response.text

    async def test_with_the_setting_off_everything_is_404(self, async_client: AsyncClient, admin_auth, folder_factory):
        """Everything, including the methods no route declares.

        A method that never reached the gate would answer 405 where the rest
        answer 404, which is how a scanner tells an install that has the
        feature switched off from one that does not have it.
        """
        await folder_factory("Kunden")

        for method, url in (
            ("OPTIONS", WEBDAV),
            ("PROPFIND", f"{WEBDAV}/"),
            ("GET", f"{WEBDAV}/Files"),
            ("PUT", f"{WEBDAV}/Files/x.3mf"),
            ("POST", WEBDAV),
            ("PATCH", f"{WEBDAV}/Files"),
            ("REPORT", f"{WEBDAV}/Files"),
            ("SEARCH", WEBDAV),
        ):
            response = await async_client.request(method, url, headers=admin_auth)
            assert response.status_code == 404, f"{method} {url} -> {response.status_code}"

    async def test_the_setting_off_hides_the_share_from_an_unauthenticated_caller_too(self, async_client: AsyncClient):
        """404 before 401 — a 401 would announce that the endpoint is there."""
        response = await async_client.request("PROPFIND", f"{WEBDAV}/", headers={"Depth": "1"})
        assert response.status_code == 404, response.text

    async def test_read_own_is_not_offered_an_external_bucket_it_cannot_look_into(
        self, async_client: AsyncClient, enable_webdav, user_factory, file_factory
    ):
        """The root's test has to ask the same question the bucket's listing answers.

        A looser one puts a permanently empty ``External/`` on the caller's
        mapped drive, and its presence alone says that external content exists
        somewhere — which is the fact the per-file gate withholds.
        """
        owner = await user_factory("extowner", permissions=["library:read_own"])
        await user_factory("stranger", permissions=["library:read_own"])
        await file_factory("scanned.3mf", is_external=True, created_by_id=owner.id)

        auth = _basic("stranger", "DavPass1!")
        root = await async_client.request("PROPFIND", f"{WEBDAV}/", headers={**auth, "Depth": "1"})

        assert set(_responses(root.content)) == {"/webdav/", "/webdav/Files/"}
        assert (await async_client.request("PROPFIND", f"{WEBDAV}/External", headers=auth)).status_code == 404

        # The owner does see it, so the bucket is hidden by permission, not by a
        # rule that lost track of loose files.
        owner_root = await async_client.request(
            "PROPFIND", f"{WEBDAV}/", headers={**_basic("extowner", "DavPass1!"), "Depth": "1"}
        )
        assert "/webdav/External/" in _responses(owner_root.content)


class TestCredentialGate:
    """What Basic auth here owes the login route it stands beside."""

    async def _fail(self, async_client: AsyncClient, username: str, times: int) -> None:
        for _ in range(times):
            response = await async_client.request(
                "PROPFIND", f"{WEBDAV}/", headers={**_basic(username, "wrong"), "Depth": "0"}
            )
            assert response.status_code == 401, response.text

    async def test_guesses_are_counted_in_the_login_routes_own_bucket(
        self, async_client: AsyncClient, enable_webdav, admin, db_session
    ):
        """The same bucket as the login route, not a second one.

        An attacker locked out of ``/auth/login`` must not get another ten
        guesses here, and the guesses spent here must lock that route in turn.
        """
        from backend.app.models.settings import Settings

        db_session.add(Settings(key="auth_enabled", value="true"))
        await db_session.commit()

        await self._fail(async_client, "davadmin", 10)

        # The eleventh is refused before the password is even looked at — this
        # one is correct.
        blocked = await async_client.request(
            "PROPFIND", f"{WEBDAV}/", headers={**_basic("davadmin", "DavPass1!"), "Depth": "0"}
        )
        assert blocked.status_code == 429, blocked.text

        shared = await async_client.post("/api/v1/auth/login", json={"username": "davadmin", "password": "DavPass1!"})
        assert shared.status_code == 429, shared.text

    async def test_a_correct_password_clears_the_failures_it_found(
        self, async_client: AsyncClient, enable_webdav, admin, admin_auth, db_session
    ):
        from sqlalchemy import select

        from backend.app.models.auth_ephemeral import AuthRateLimitEvent

        await self._fail(async_client, "davadmin", 3)
        opened = await async_client.request("PROPFIND", f"{WEBDAV}/", headers={**admin_auth, "Depth": "0"})
        assert opened.status_code == 207, opened.text

        remaining = (
            (await db_session.execute(select(AuthRateLimitEvent).where(AuthRateLimitEvent.username == "davadmin")))
            .scalars()
            .all()
        )
        assert remaining == []

    async def test_a_two_factor_account_is_refused_rather_than_served_on_the_password(
        self, async_client: AsyncClient, enable_webdav, admin, admin_auth, db_session
    ):
        """Basic has nowhere to put a challenge.

        Serving the library on the password alone would make that password
        worth more over WebDAV than it is in the browser, where auth.py hands
        back a pre-auth token instead of a session.
        """
        from backend.app.models.user_totp import UserTOTP

        totp = UserTOTP(user_id=admin.id, is_enabled=True)
        totp.secret = "JBSWY3DPEHPK3PXP"  # noqa: S105 - test fixture, not a real secret
        db_session.add(totp)
        await db_session.commit()

        response = await async_client.request("PROPFIND", f"{WEBDAV}/", headers={**admin_auth, "Depth": "0"})

        # 403, not another 401: a Basic challenge would re-prompt for a password
        # that is already correct, forever.
        assert response.status_code == 403, response.text
        assert "two-factor" in response.json()["detail"]

    async def test_an_email_otp_account_is_refused_too(
        self, async_client: AsyncClient, enable_webdav, admin, admin_auth, db_session
    ):
        from backend.app.models.settings import Settings

        admin.email = "davadmin@test.example"
        db_session.add(admin)
        db_session.add(Settings(key=f"user_{admin.id}_email_2fa_enabled", value="true"))
        await db_session.commit()

        response = await async_client.request("PROPFIND", f"{WEBDAV}/", headers={**admin_auth, "Depth": "0"})

        assert response.status_code == 403, response.text

    async def test_the_local_login_switch_closes_the_share_as_well(
        self, async_client: AsyncClient, enable_webdav, admin, admin_auth, db_session
    ):
        """#1589: an SSO-only install turned local passwords off deliberately."""
        from backend.app.models.settings import Settings

        db_session.add(Settings(key="local_login_enabled", value="false"))
        await db_session.commit()

        response = await async_client.request("PROPFIND", f"{WEBDAV}/", headers={**admin_auth, "Depth": "0"})

        assert response.status_code == 401, response.text

    async def test_a_directory_account_authenticates_through_ldap(
        self, async_client: AsyncClient, enable_webdav, db_session, folder_factory
    ):
        """``authenticate_user`` refuses every ldap/oidc row by design.

        Stopping there would leave a directory-backed install with a share no
        user can ever open, and a 401 indistinguishable from a typo.
        """
        from unittest.mock import patch

        from backend.app.models.settings import Settings
        from backend.app.models.user import User
        from backend.app.services.ldap_service import LDAPUserInfo

        for key, value in {
            "ldap_enabled": "true",
            "ldap_server_url": "ldaps://ldap.test.example:636",
            "ldap_bind_dn": "cn=admin,dc=test,dc=com",
            "ldap_bind_password": "x",  # pragma: allowlist secret — test fixture
            "ldap_search_base": "dc=test,dc=com",
            "ldap_user_filter": "(uid={username})",
            "ldap_security": "ldaps",
            "ldap_group_mapping": "{}",
            "ldap_auto_provision": "false",
        }.items():
            db_session.add(Settings(key=key, value=value))
        db_session.add(
            User(username="diruser", email="diruser@test.example", password_hash=None, role="admin", auth_source="ldap")
        )
        await db_session.commit()
        await folder_factory("Kunden")

        directory_answer = LDAPUserInfo(
            username="diruser", email="diruser@test.example", display_name="Dir User", groups=[]
        )
        with patch("backend.app.services.ldap_service.authenticate_ldap_user", return_value=directory_answer):
            response = await async_client.request(
                "PROPFIND", f"{WEBDAV}/", headers={**_basic("diruser", "directory-pass"), "Depth": "1"}
            )

        assert response.status_code == 207, response.text
        assert "/webdav/Files/" in _responses(response.content)


class TestEndToEndWalk:
    """Root → folder → file, the way a client actually does it."""

    async def test_a_client_walks_from_the_root_to_the_bytes(
        self, async_client: AsyncClient, enable_webdav, admin_auth, folder_factory, file_factory
    ):
        kunden = await folder_factory("Kunden & Co")
        job = await folder_factory("Auftrag 4711", parent_id=kunden.id)
        await file_factory("Stübe V60 -H2D.3mf", b"the-model", folder_id=job.id)

        options = await async_client.request("OPTIONS", WEBDAV, headers=admin_auth)
        assert options.headers["DAV"] == "1"

        current = "/webdav/"
        for expected in ("Files", "Kunden & Co", "Auftrag 4711"):
            listing = await async_client.request("PROPFIND", current, headers={**admin_auth, "Depth": "1"})
            assert listing.status_code == 207, listing.text
            rows = _responses(listing.content)
            nxt = [href for href in rows if href != unquote(current) and href.endswith(f"{expected}/")]
            assert nxt, f"{expected} not found in {list(rows)}"
            # Follow the encoded href the response carried, not a rebuilt path.
            current = next(href for href in _hrefs(listing.content) if unquote(href) == nxt[0])

        listing = await async_client.request("PROPFIND", current, headers={**admin_auth, "Depth": "1"})
        file_href = next(href for href in _hrefs(listing.content) if not href.endswith("/"))

        head = await async_client.head(file_href, headers=admin_auth)
        assert head.status_code == 200
        assert head.headers["content-length"] == "9"

        body = await async_client.get(file_href, headers=admin_auth)
        assert body.status_code == 200
        assert body.content == b"the-model"


class TestTheSetting:
    """``webdav_mode`` is the switch the whole feature hangs off."""

    async def test_it_defaults_to_off(self, async_client: AsyncClient):
        """An install that has never seen the setting exposes nothing."""
        response = await async_client.get("/api/v1/settings/")

        assert response.status_code == 200, response.text
        assert response.json()["webdav_mode"] == "off"

    @pytest.mark.parametrize("mode", ["read", "readwrite", "off"])
    async def test_each_mode_round_trips(self, async_client: AsyncClient, mode):
        patched = await async_client.patch("/api/v1/settings/", json={"webdav_mode": mode})
        assert patched.status_code == 200, patched.text
        assert patched.json()["webdav_mode"] == mode

        reread = await async_client.get("/api/v1/settings/")
        assert reread.json()["webdav_mode"] == mode

    async def test_a_value_that_is_not_a_mode_is_refused(self, async_client: AsyncClient):
        """Not coerced to something that serves files -- refused outright."""
        response = await async_client.patch("/api/v1/settings/", json={"webdav_mode": "yes"})

        assert response.status_code == 422, response.text

    async def test_an_unreadable_stored_value_reads_as_off(self, async_client: AsyncClient, db_session, admin_auth):
        """A row the schema cannot parse must fail closed, not fail open.

        The settings response would otherwise 500 on the whole payload (#2905),
        and the share must not decide to serve anything on the strength of a
        value nobody recognises.
        """
        from backend.app.models.settings import Settings

        db_session.add(Settings(key="webdav_mode", value="None"))
        await db_session.commit()

        assert (await async_client.get("/api/v1/settings/")).json()["webdav_mode"] == "off"
        assert (await async_client.request("PROPFIND", f"{WEBDAV}/", headers=admin_auth)).status_code == 404

    async def test_turning_it_on_through_the_api_opens_the_share(
        self, async_client: AsyncClient, admin_auth, folder_factory
    ):
        await folder_factory("Kunden")
        assert (await async_client.request("PROPFIND", f"{WEBDAV}/", headers=admin_auth)).status_code == 404

        await async_client.patch("/api/v1/settings/", json={"webdav_mode": "read"})

        opened = await async_client.request("PROPFIND", f"{WEBDAV}/", headers=admin_auth)
        assert opened.status_code == 207, opened.text


# ---------------------------------------------------------------------------
# The write half
# ---------------------------------------------------------------------------


def _three_mf(*, seconds: int = 3600, grams: float = 12.5, thumbnail: bytes = b"\x89PNG-plate") -> bytes:
    """A 3MF small enough to read and real enough for ThreeMFParser.

    Only the two entries the parser actually mines for a library row: the plate
    summary it reads print time and filament weight from, and the plate render
    it lifts the thumbnail out of.
    """
    import io
    import zipfile

    slice_info = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        "<config><plate>"
        '<metadata key="index" value="1"/>'
        f'<metadata key="prediction" value="{seconds}"/>'
        f'<metadata key="weight" value="{grams}"/>'
        "</plate></config>"
    )
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("3D/3dmodel.model", "<model/>")
        archive.writestr("Metadata/slice_info.config", slice_info)
        archive.writestr("Metadata/plate_1.png", thumbnail)
    return buffer.getvalue()


async def _row(db_session, file_id: int):
    """Re-read a row the app committed in its own session.

    ``populate_existing`` on every read here: the test session runs with
    ``expire_on_commit=False`` and is still holding its pre-write copy, while
    expiring the whole identity map would make the next attribute read on some
    other object fire a lazy load from async code.
    """
    from backend.app.models.library import LibraryFile

    return await db_session.get(LibraryFile, file_id, populate_existing=True)


async def _rows(db_session, **filters):
    from backend.app.models.library import LibraryFile

    query = select(LibraryFile)
    for column, value in filters.items():
        query = query.where(getattr(LibraryFile, column) == value)
    result = await db_session.execute(query.order_by(LibraryFile.id).execution_options(populate_existing=True))
    return list(result.scalars().all())


async def _only_row(db_session, **filters):
    rows = await _rows(db_session, **filters)
    assert len(rows) == 1, [(row.id, row.filename, row.deleted_at) for row in rows]
    return rows[0]


class TestModeGate:
    """What each of the three modes answers, and what OPTIONS promises."""

    async def test_read_mode_allows_exactly_the_four_read_methods(
        self, async_client: AsyncClient, enable_webdav, admin_auth
    ):
        response = await async_client.request("OPTIONS", WEBDAV, headers=admin_auth)

        assert response.headers["DAV"] == "1"
        assert {method.strip() for method in response.headers["Allow"].split(",")} == {
            "OPTIONS",
            "PROPFIND",
            "HEAD",
            "GET",
        }

    async def test_readwrite_advertises_dav_2_and_the_write_methods(
        self, async_client: AsyncClient, writable_webdav, admin_auth
    ):
        """Level 2 is locking, and a client that does not see it will not save.

        ``Allow`` alone is not enough: the Windows client decides the share is
        read-only from the ``DAV`` header before it looks at anything else.
        """
        response = await async_client.request("OPTIONS", WEBDAV, headers=admin_auth)

        assert response.headers["DAV"] == "1, 2"
        assert {method.strip() for method in response.headers["Allow"].split(",")} == {
            "OPTIONS",
            "PROPFIND",
            "HEAD",
            "GET",
            "PUT",
            "DELETE",
            "MKCOL",
            "MOVE",
            "COPY",
            "LOCK",
            "UNLOCK",
            "PROPPATCH",
        }

    @pytest.mark.parametrize("method", ["PUT", "DELETE", "MKCOL", "MOVE", "COPY", "LOCK", "UNLOCK", "PROPPATCH"])
    async def test_read_mode_refuses_every_write_and_writes_nothing(
        self, async_client: AsyncClient, enable_webdav, admin_auth, folder_factory, db_session, method
    ):
        folder = await folder_factory("Kunden")

        response = await async_client.request(
            method,
            f"{WEBDAV}/Files/Kunden/new.3mf",
            headers={**admin_auth, "Destination": f"http://testserver{WEBDAV}/Files/Kunden/other.3mf"},
            content=b"payload",
        )

        assert response.status_code == 405, response.text
        assert {value.strip() for value in response.headers["Allow"].split(",")} == {
            "OPTIONS",
            "PROPFIND",
            "HEAD",
            "GET",
        }
        assert await _rows(db_session, folder_id=folder.id) == []

    async def test_readwrite_still_reads(self, async_client: AsyncClient, writable_webdav, admin_auth, file_factory):
        await file_factory("part.3mf", b"the-model")

        listed = await async_client.request("PROPFIND", f"{WEBDAV}/Files", headers={**admin_auth, "Depth": "1"})
        assert "/webdav/Files/part.3mf" in _responses(listed.content)

        body = await async_client.get(f"{WEBDAV}/Files/part.3mf", headers=admin_auth)
        assert body.content == b"the-model"

    async def test_a_writable_share_advertises_supportedlock(
        self, async_client: AsyncClient, writable_webdav, admin_auth, file_factory
    ):
        await file_factory("part.3mf")

        response = await async_client.request("PROPFIND", f"{WEBDAV}/Files", headers={**admin_auth, "Depth": "1"})

        entry = _responses(response.content)["/webdav/Files/part.3mf"]
        assert _prop(entry, "supportedlock") is not None


class TestPut:
    """Creating and replacing files, and the client habits that shape it."""

    async def test_a_put_creates_a_file_the_file_manager_lists(
        self, async_client: AsyncClient, writable_webdav, admin_auth, folder_factory, db_session
    ):
        """Hash, type, metadata and thumbnail -- the same ingest an upload runs."""
        import hashlib

        folder = await folder_factory("Kunden")
        payload = _three_mf()

        response = await async_client.request(
            "PUT", f"{WEBDAV}/Files/Kunden/Sockel.3mf", headers=admin_auth, content=payload
        )

        assert response.status_code == 201, response.text
        row = await _only_row(db_session, folder_id=folder.id)
        assert row.filename == "Sockel.3mf"
        assert row.file_type == "3mf"
        assert row.file_size == len(payload)
        assert row.file_hash == hashlib.sha256(payload).hexdigest()
        assert row.ingest_pending is False
        assert row.file_metadata["print_time_seconds"] == 3600
        assert row.file_metadata["filament_used_grams"] == 12.5
        assert row.thumbnail_path

        listed = await async_client.get(f"/api/v1/library/files?folder_id={folder.id}")
        assert [entry["filename"] for entry in listed.json()] == ["Sockel.3mf"]
        assert listed.json()[0]["thumbnail_path"]

    async def test_the_bytes_come_back_through_the_share(self, async_client: AsyncClient, writable_webdav, admin_auth):
        await async_client.request("PUT", f"{WEBDAV}/Files/notes.txt", headers=admin_auth, content=b"hello drive")

        body = await async_client.get(f"{WEBDAV}/Files/notes.txt", headers=admin_auth)

        assert body.status_code == 200, body.text
        assert body.content == b"hello drive"

    async def test_the_empty_then_content_dance_ends_with_one_complete_row(
        self, async_client: AsyncClient, writable_webdav, admin_auth, db_session
    ):
        """What every application actually does when it saves a file.

        Create it empty, lock it, write the bytes, unlock. A naive ingest turns
        that into a 0-byte row with a pointless hash -- and since every empty
        file hashes to the same value, a library full of "duplicates".
        """
        created = await async_client.request("PUT", f"{WEBDAV}/Files/draft.3mf", headers=admin_auth, content=b"")
        assert created.status_code == 201, created.text

        pending = await _only_row(db_session, filename="draft.3mf")
        assert pending.ingest_pending is True
        assert pending.file_size == 0
        assert pending.file_hash is None

        payload = _three_mf()
        filled = await async_client.request("PUT", f"{WEBDAV}/Files/draft.3mf", headers=admin_auth, content=payload)
        assert filled.status_code == 204, filled.text

        row = await _only_row(db_session, filename="draft.3mf")
        assert row.id == pending.id
        assert row.ingest_pending is False
        assert row.file_size == len(payload)
        assert row.file_metadata["print_time_seconds"] == 3600

    async def test_a_pending_row_is_not_counted_as_a_duplicate(
        self, async_client: AsyncClient, writable_webdav, admin_auth
    ):
        """Two files created empty in the same second are not two copies."""
        await async_client.request("PUT", f"{WEBDAV}/Files/one.3mf", headers=admin_auth, content=b"")
        await async_client.request("PUT", f"{WEBDAV}/Files/two.3mf", headers=admin_auth, content=b"")

        listed = await async_client.get("/api/v1/library/files")

        assert sorted(entry["filename"] for entry in listed.json()) == ["one.3mf", "two.3mf"]
        assert {entry["duplicate_count"] for entry in listed.json()} == {0}

    async def test_an_empty_put_never_empties_a_file_that_has_content(
        self, async_client: AsyncClient, writable_webdav, admin_auth, db_session
    ):
        """A truncate and the first half of the create-then-write dance look
        identical on the wire, and only one of the two readings can lose data.
        """
        payload = _three_mf()
        await async_client.request("PUT", f"{WEBDAV}/Files/kept.3mf", headers=admin_auth, content=payload)

        response = await async_client.request("PUT", f"{WEBDAV}/Files/kept.3mf", headers=admin_auth, content=b"")

        assert response.status_code == 204, response.text
        row = await _only_row(db_session, filename="kept.3mf")
        assert row.file_size == len(payload)
        assert row.ingest_pending is False
        body = await async_client.get(f"{WEBDAV}/Files/kept.3mf", headers=admin_auth)
        assert body.content == payload

    async def test_an_overwrite_keeps_the_row_its_id_and_its_tags(
        self, async_client: AsyncClient, writable_webdav, admin_auth, db_session, file_factory
    ):
        """The owner's decision, and the right one for a drive: replace.

        No version, no variant -- the row a user tagged and wrote notes on is
        still that row after a save from Explorer.
        """
        from backend.app.models.library import LibraryFileTag, LibraryTag

        original = await file_factory("brief.txt", b"old-bytes")
        tag = LibraryTag(name="kid-safe", name_key="kid-safe")
        db_session.add(tag)
        await db_session.flush()
        db_session.add(LibraryFileTag(file_id=original.id, tag_id=tag.id))
        original.notes = "print at 0.2"
        await db_session.commit()

        response = await async_client.request(
            "PUT", f"{WEBDAV}/Files/brief.txt", headers=admin_auth, content=b"the-new-bytes"
        )

        assert response.status_code == 204, response.text
        row = await _only_row(db_session, filename="brief.txt")
        assert row.id == original.id
        assert row.notes == "print at 0.2"
        assert row.file_size == len(b"the-new-bytes")
        listed = (await async_client.get("/api/v1/library/files")).json()
        assert [entry["id"] for entry in listed] == [original.id]
        assert [chip["name"] for chip in listed[0]["tags"]] == ["kid-safe"]
        body = await async_client.get(f"{WEBDAV}/Files/brief.txt", headers=admin_auth)
        assert body.content == b"the-new-bytes"

    @pytest.mark.parametrize("junk", ["desktop.ini", "Thumbs.db", ".DS_Store", "._resource"])
    async def test_junk_files_are_accepted_and_vanish(
        self, async_client: AsyncClient, writable_webdav, admin_auth, folder_factory, db_session, junk
    ):
        """Explorer writes these into folders it merely looked at."""
        folder = await folder_factory("Kunden")

        response = await async_client.request(
            "PUT", f"{WEBDAV}/Files/Kunden/{junk}", headers=admin_auth, content=b"[.ShellClassInfo]"
        )

        assert response.status_code in (201, 204), response.text
        assert await _rows(db_session, folder_id=folder.id) == []
        listed = await async_client.request("PROPFIND", f"{WEBDAV}/Files/Kunden", headers={**admin_auth, "Depth": "1"})
        assert set(_responses(listed.content)) == {"/webdav/Files/Kunden/"}

    async def test_a_junk_row_an_external_scan_found_is_not_listed_either(
        self, async_client: AsyncClient, writable_webdav, admin_auth, folder_factory, file_factory
    ):
        """The rule is about what the share shows, not about who wrote it."""
        folder = await folder_factory("Kunden")
        await file_factory("Thumbs.db", folder_id=folder.id)
        await file_factory("part.3mf", folder_id=folder.id)

        listed = await async_client.request("PROPFIND", f"{WEBDAV}/Files/Kunden", headers={**admin_auth, "Depth": "1"})

        assert set(_responses(listed.content)) == {"/webdav/Files/Kunden/", "/webdav/Files/Kunden/part.3mf"}

    async def test_a_tmp_file_is_the_users_own_and_is_kept(
        self, async_client: AsyncClient, writable_webdav, admin_auth, db_session
    ):
        """The junk list is a list, not a pattern -- a .tmp may be real work."""
        await async_client.request("PUT", f"{WEBDAV}/Files/scan.tmp", headers=admin_auth, content=b"data")

        assert (await _only_row(db_session, filename="scan.tmp")).file_type == "tmp"

    async def test_a_put_into_a_folder_that_does_not_exist_is_409(
        self, async_client: AsyncClient, writable_webdav, admin_auth
    ):
        response = await async_client.request(
            "PUT", f"{WEBDAV}/Files/Nowhere/part.3mf", headers=admin_auth, content=b"x"
        )

        assert response.status_code == 409, response.text

    async def test_a_put_onto_a_folder_is_405(
        self, async_client: AsyncClient, writable_webdav, admin_auth, folder_factory
    ):
        await folder_factory("Kunden")

        response = await async_client.request("PUT", f"{WEBDAV}/Files/Kunden", headers=admin_auth, content=b"x")

        assert response.status_code == 405, response.text


class TestDelete:
    """A drive that empties the recycle bin for you is not what anyone expects."""

    async def test_delete_trashes_rather_than_erases(
        self, async_client: AsyncClient, writable_webdav, admin_auth, file_factory, db_session, library_root
    ):
        row = await file_factory("part.3mf", b"the-model")
        on_disk = library_root / row.file_path

        response = await async_client.request("DELETE", f"{WEBDAV}/Files/part.3mf", headers=admin_auth)

        assert response.status_code == 204, response.text
        trashed = await _row(db_session, row.id)
        assert trashed.deleted_at is not None
        assert on_disk.exists(), "the bytes belong to the trash sweeper, not to a DELETE"
        gone = await async_client.get(f"{WEBDAV}/Files/part.3mf", headers=admin_auth)
        assert gone.status_code == 404

    async def test_deleting_something_that_is_not_there_is_404(
        self, async_client: AsyncClient, writable_webdav, admin_auth
    ):
        response = await async_client.request("DELETE", f"{WEBDAV}/Files/ghost.3mf", headers=admin_auth)

        assert response.status_code == 404, response.text

    async def test_an_empty_folder_can_be_removed(
        self, async_client: AsyncClient, writable_webdav, admin_auth, folder_factory, db_session
    ):
        from backend.app.models.library import LibraryFolder

        folder = await folder_factory("Leer")

        response = await async_client.request("DELETE", f"{WEBDAV}/Files/Leer", headers=admin_auth)

        assert response.status_code == 204, response.text
        assert await db_session.get(LibraryFolder, folder.id, populate_existing=True) is None

    async def test_a_folder_with_files_in_it_is_refused(
        self, async_client: AsyncClient, writable_webdav, admin_auth, folder_factory, file_factory, db_session
    ):
        """The UI's folder delete is a cascade that hard-deletes the tree.

        Behind a mapped drive that is one stray keystroke from a print farm's
        whole library, with no trash to undo it from.
        """
        from backend.app.models.library import LibraryFolder

        folder = await folder_factory("Kunden")
        row = await file_factory("part.3mf", folder_id=folder.id)

        response = await async_client.request("DELETE", f"{WEBDAV}/Files/Kunden", headers=admin_auth)

        assert response.status_code == 403, response.text
        assert await db_session.get(LibraryFolder, folder.id, populate_existing=True) is not None
        assert (await _row(db_session, row.id)).deleted_at is None

    async def test_a_folder_holding_only_trashed_files_is_refused_too(
        self, async_client: AsyncClient, writable_webdav, admin_auth, folder_factory, file_factory
    ):
        """``folder_id`` cascades: the rows somebody can still restore would go."""
        folder = await folder_factory("Kunden")
        await file_factory("part.3mf", folder_id=folder.id, deleted_at=datetime(2026, 1, 1))

        response = await async_client.request("DELETE", f"{WEBDAV}/Files/Kunden", headers=admin_auth)

        assert response.status_code == 403, response.text

    @pytest.mark.parametrize("path", ["", "/Files", "/External"])
    async def test_the_share_and_its_buckets_cannot_be_deleted(
        self, async_client: AsyncClient, writable_webdav, admin_auth, folder_factory, path
    ):
        await folder_factory("NAS", is_external=True)

        response = await async_client.request("DELETE", f"{WEBDAV}{path}", headers=admin_auth)

        assert response.status_code == 403, response.text


class TestMkcol:
    async def test_mkcol_creates_a_folder_with_no_number(
        self, async_client: AsyncClient, writable_webdav, admin_auth, db_session
    ):
        """Even with the series on: there is no dialog here to ask, and a
        number consumed for a folder nobody numbered is a gap in the books.
        """
        from backend.app.models.library import LibraryFolder
        from backend.app.models.settings import Settings

        db_session.add(Settings(key="number_series_library_folder_enabled", value="true"))
        await db_session.commit()

        response = await async_client.request("MKCOL", f"{WEBDAV}/Files/Kunden%20%26%20Co", headers=admin_auth)

        assert response.status_code == 201, response.text
        folder = (
            await db_session.execute(
                select(LibraryFolder)
                .where(LibraryFolder.name == "Kunden & Co")
                .execution_options(populate_existing=True)
            )
        ).scalar_one()
        assert folder.number is None
        assert folder.parent_id is None

        listed = await async_client.request("PROPFIND", f"{WEBDAV}/Files", headers={**admin_auth, "Depth": "1"})
        assert "/webdav/Files/Kunden & Co/" in _responses(listed.content)

    async def test_mkcol_nests_under_an_existing_folder(
        self, async_client: AsyncClient, writable_webdav, admin_auth, folder_factory, db_session
    ):
        from backend.app.models.library import LibraryFolder

        parent = await folder_factory("Kunden")

        response = await async_client.request("MKCOL", f"{WEBDAV}/Files/Kunden/2026", headers=admin_auth)

        assert response.status_code == 201, response.text
        child = (
            await db_session.execute(
                select(LibraryFolder).where(LibraryFolder.name == "2026").execution_options(populate_existing=True)
            )
        ).scalar_one()
        assert child.parent_id == parent.id

    async def test_mkcol_onto_an_existing_name_is_405(
        self, async_client: AsyncClient, writable_webdav, admin_auth, folder_factory
    ):
        await folder_factory("Kunden")

        response = await async_client.request("MKCOL", f"{WEBDAV}/Files/Kunden", headers=admin_auth)

        assert response.status_code == 405, response.text

    async def test_mkcol_at_the_share_root_is_refused(self, async_client: AsyncClient, writable_webdav, admin_auth):
        """The root holds exactly the two buckets; it is not a directory."""
        response = await async_client.request("MKCOL", f"{WEBDAV}/Somewhere", headers=admin_auth)

        assert response.status_code == 403, response.text

    async def test_mkcol_with_a_body_is_415(self, async_client: AsyncClient, writable_webdav, admin_auth):
        response = await async_client.request("MKCOL", f"{WEBDAV}/Files/Kunden", headers=admin_auth, content=b"<x/>")

        assert response.status_code == 415, response.text


@pytest.fixture
def external_folder_factory(db_session, tmp_path):
    """A registered external folder with a real directory behind it."""

    async def _create(name: str, *, readonly: bool = False, parent_id: int | None = None):
        from backend.app.models.library import LibraryFolder

        directory = tmp_path / "share" / name
        directory.mkdir(parents=True, exist_ok=True)
        folder = LibraryFolder(
            name=name,
            parent_id=parent_id,
            is_external=True,
            external_path=str(directory),
            external_readonly=readonly,
        )
        db_session.add(folder)
        await db_session.commit()
        await db_session.refresh(folder)
        return folder, directory

    return _create


def _destination(path: str) -> str:
    return f"http://testserver{WEBDAV}{path}"


class TestMove:
    """Rename, reparent, and land a temporary name on its real one."""

    async def test_a_rename_that_changes_the_extension_re_runs_the_ingest(
        self, async_client: AsyncClient, writable_webdav, admin_auth, db_session
    ):
        """The write-temp-then-rename habit, which is the reason this matters.

        A client that writes ``foo.tmp`` and renames it onto ``foo.3mf`` has
        given the library a 3MF, and the classification, the parse and the
        thumbnail all have to run on the name it ended up with.
        """
        payload = _three_mf()
        await async_client.request("PUT", f"{WEBDAV}/Files/job.tmp", headers=admin_auth, content=payload)
        before = await _only_row(db_session, filename="job.tmp")
        assert before.file_type == "tmp"
        assert before.file_metadata is None

        response = await async_client.request(
            "MOVE",
            f"{WEBDAV}/Files/job.tmp",
            headers={**admin_auth, "Destination": _destination("/Files/job.3mf")},
        )

        assert response.status_code == 201, response.text
        row = await _only_row(db_session, filename="job.3mf")
        assert row.id == before.id
        assert row.file_type == "3mf"
        assert row.file_metadata["print_time_seconds"] == 3600
        assert row.thumbnail_path
        # The blob follows the name, because the STL and PDF renderers load by
        # suffix and a .3mf parked at <uuid>.tmp would silently never preview.
        assert row.file_path.endswith(".3mf")
        body = await async_client.get(f"{WEBDAV}/Files/job.3mf", headers=admin_auth)
        assert body.content == payload

    async def test_a_move_between_folders_keeps_the_row(
        self, async_client: AsyncClient, writable_webdav, admin_auth, folder_factory, db_session
    ):
        source = await folder_factory("Eingang")
        target = await folder_factory("Kunden")
        await async_client.request("PUT", f"{WEBDAV}/Files/Eingang/part.3mf", headers=admin_auth, content=_three_mf())
        before = await _only_row(db_session, folder_id=source.id)

        response = await async_client.request(
            "MOVE",
            f"{WEBDAV}/Files/Eingang/part.3mf",
            headers={**admin_auth, "Destination": _destination("/Files/Kunden/part.3mf")},
        )

        assert response.status_code == 201, response.text
        row = await _only_row(db_session, folder_id=target.id)
        assert row.id == before.id
        assert await _rows(db_session, folder_id=source.id) == []

    async def test_a_move_onto_a_pending_row_completes_it_without_a_second_row(
        self, async_client: AsyncClient, writable_webdav, admin_auth, db_session
    ):
        """Both client habits at once, which is how they actually arrive.

        The application creates ``report.3mf`` empty, writes its bytes into a
        temporary name and renames that over the empty file. The row the user
        will see has to be the one that existed first, complete.
        """
        payload = _three_mf()
        await async_client.request("PUT", f"{WEBDAV}/Files/report.3mf", headers=admin_auth, content=b"")
        pending = await _only_row(db_session, filename="report.3mf")
        await async_client.request("PUT", f"{WEBDAV}/Files/report.tmp", headers=admin_auth, content=payload)

        response = await async_client.request(
            "MOVE",
            f"{WEBDAV}/Files/report.tmp",
            headers={**admin_auth, "Destination": _destination("/Files/report.3mf")},
        )

        assert response.status_code == 204, response.text
        row = await _only_row(db_session, filename="report.3mf")
        assert row.id == pending.id, "the row the client created first is the one it keeps"
        assert row.ingest_pending is False
        assert row.file_type == "3mf"
        assert row.file_size == len(payload)
        assert row.file_metadata["print_time_seconds"] == 3600
        assert await _rows(db_session, filename="report.tmp") == []
        body = await async_client.get(f"{WEBDAV}/Files/report.3mf", headers=admin_auth)
        assert body.content == payload

    async def test_overwrite_f_onto_an_existing_name_is_412(
        self, async_client: AsyncClient, writable_webdav, admin_auth, db_session
    ):
        await async_client.request("PUT", f"{WEBDAV}/Files/a.txt", headers=admin_auth, content=b"first")
        await async_client.request("PUT", f"{WEBDAV}/Files/b.txt", headers=admin_auth, content=b"second")

        response = await async_client.request(
            "MOVE",
            f"{WEBDAV}/Files/a.txt",
            headers={**admin_auth, "Destination": _destination("/Files/b.txt"), "Overwrite": "F"},
        )

        assert response.status_code == 412, response.text
        assert (await _only_row(db_session, filename="b.txt")).file_size == len(b"second")
        assert await _rows(db_session, filename="a.txt")

    async def test_a_move_into_a_writable_external_folder_writes_the_share(
        self, async_client: AsyncClient, writable_webdav, admin_auth, external_folder_factory, db_session
    ):
        """Crossing the #124 boundary: the row follows the real file."""
        folder, directory = await external_folder_factory("NAS")
        await async_client.request("PUT", f"{WEBDAV}/Files/part.3mf", headers=admin_auth, content=_three_mf())

        response = await async_client.request(
            "MOVE",
            f"{WEBDAV}/Files/part.3mf",
            headers={**admin_auth, "Destination": _destination("/External/NAS/part.3mf")},
        )

        assert response.status_code == 201, response.text
        row = await _only_row(db_session, folder_id=folder.id)
        assert row.is_external is True
        assert Path(row.file_path) == directory / "part.3mf"
        assert (directory / "part.3mf").exists()

    async def test_a_managed_folder_can_be_renamed(
        self, async_client: AsyncClient, writable_webdav, admin_auth, folder_factory, db_session
    ):
        from backend.app.models.library import LibraryFolder

        folder = await folder_factory("Kunde A")

        response = await async_client.request(
            "MOVE", f"{WEBDAV}/Files/Kunde%20A", headers={**admin_auth, "Destination": _destination("/Files/Kunde%20B")}
        )

        assert response.status_code == 201, response.text
        renamed = await db_session.get(LibraryFolder, folder.id, populate_existing=True)
        assert renamed.name == "Kunde B"

    async def test_a_folder_cannot_be_moved_into_its_own_subtree(
        self, async_client: AsyncClient, writable_webdav, admin_auth, folder_factory
    ):
        parent = await folder_factory("Kunden")
        await folder_factory("2026", parent_id=parent.id)

        response = await async_client.request(
            "MOVE",
            f"{WEBDAV}/Files/Kunden",
            headers={**admin_auth, "Destination": _destination("/Files/Kunden/2026/Kunden")},
        )

        assert response.status_code == 409, response.text

    async def test_a_move_onto_a_folder_is_refused(
        self, async_client: AsyncClient, writable_webdav, admin_auth, folder_factory
    ):
        await folder_factory("Kunden")
        await async_client.request("PUT", f"{WEBDAV}/Files/part.3mf", headers=admin_auth, content=b"x")

        response = await async_client.request(
            "MOVE", f"{WEBDAV}/Files/part.3mf", headers={**admin_auth, "Destination": _destination("/Files/Kunden")}
        )

        assert response.status_code == 403, response.text

    async def test_a_move_with_no_destination_is_400(self, async_client: AsyncClient, writable_webdav, admin_auth):
        await async_client.request("PUT", f"{WEBDAV}/Files/part.3mf", headers=admin_auth, content=b"x")

        response = await async_client.request("MOVE", f"{WEBDAV}/Files/part.3mf", headers=admin_auth)

        assert response.status_code == 400, response.text

    async def test_a_move_onto_a_junk_name_is_refused_rather_than_swallowed(
        self, async_client: AsyncClient, writable_webdav, admin_auth, db_session
    ):
        """The name is one the share drops on the floor, so accepting the move
        would answer success while deleting the file.
        """
        await async_client.request("PUT", f"{WEBDAV}/Files/part.3mf", headers=admin_auth, content=b"x")

        response = await async_client.request(
            "MOVE", f"{WEBDAV}/Files/part.3mf", headers={**admin_auth, "Destination": _destination("/Files/Thumbs.db")}
        )

        assert response.status_code == 403, response.text
        assert await _rows(db_session, filename="part.3mf")


class TestCopy:
    async def test_a_copy_produces_a_second_hashed_row(
        self, async_client: AsyncClient, writable_webdav, admin_auth, folder_factory, db_session
    ):
        """Its own blob and its own derived columns.

        A copy that pointed at the original's bytes would vanish the moment the
        original was trashed and swept.
        """
        folder = await folder_factory("Kunden")
        payload = _three_mf()
        await async_client.request("PUT", f"{WEBDAV}/Files/part.3mf", headers=admin_auth, content=payload)

        response = await async_client.request(
            "COPY",
            f"{WEBDAV}/Files/part.3mf",
            headers={**admin_auth, "Destination": _destination("/Files/Kunden/part.3mf")},
        )

        assert response.status_code == 201, response.text
        original = await _only_row(db_session, folder_id=None)
        copy = await _only_row(db_session, folder_id=folder.id)
        assert copy.id != original.id
        assert copy.file_hash == original.file_hash
        assert copy.file_path != original.file_path
        assert copy.file_type == "3mf"
        assert copy.file_metadata["print_time_seconds"] == 3600
        assert copy.thumbnail_path and copy.thumbnail_path != original.thumbnail_path
        body = await async_client.get(f"{WEBDAV}/Files/Kunden/part.3mf", headers=admin_auth)
        assert body.content == payload

    async def test_a_copy_leaves_the_source_alone(self, async_client: AsyncClient, writable_webdav, admin_auth):
        await async_client.request("PUT", f"{WEBDAV}/Files/part.txt", headers=admin_auth, content=b"payload")

        await async_client.request(
            "COPY", f"{WEBDAV}/Files/part.txt", headers={**admin_auth, "Destination": _destination("/Files/dup.txt")}
        )

        source = await async_client.get(f"{WEBDAV}/Files/part.txt", headers=admin_auth)
        assert source.content == b"payload"

    async def test_copy_overwrite_f_is_412(self, async_client: AsyncClient, writable_webdav, admin_auth, db_session):
        await async_client.request("PUT", f"{WEBDAV}/Files/a.txt", headers=admin_auth, content=b"first")
        await async_client.request("PUT", f"{WEBDAV}/Files/b.txt", headers=admin_auth, content=b"second")

        response = await async_client.request(
            "COPY",
            f"{WEBDAV}/Files/a.txt",
            headers={**admin_auth, "Destination": _destination("/Files/b.txt"), "Overwrite": "F"},
        )

        assert response.status_code == 412, response.text
        assert (await _only_row(db_session, filename="b.txt")).file_size == len(b"second")

    async def test_copy_over_an_existing_name_replaces_that_row(
        self, async_client: AsyncClient, writable_webdav, admin_auth, db_session
    ):
        await async_client.request("PUT", f"{WEBDAV}/Files/a.txt", headers=admin_auth, content=b"the-source")
        await async_client.request("PUT", f"{WEBDAV}/Files/b.txt", headers=admin_auth, content=b"second")
        target = await _only_row(db_session, filename="b.txt")

        response = await async_client.request(
            "COPY", f"{WEBDAV}/Files/a.txt", headers={**admin_auth, "Destination": _destination("/Files/b.txt")}
        )

        assert response.status_code == 204, response.text
        row = await _only_row(db_session, filename="b.txt")
        assert row.id == target.id
        body = await async_client.get(f"{WEBDAV}/Files/b.txt", headers=admin_auth)
        assert body.content == b"the-source"

    async def test_copying_a_folder_is_refused(
        self, async_client: AsyncClient, writable_webdav, admin_auth, folder_factory
    ):
        await folder_factory("Kunden")

        response = await async_client.request(
            "COPY", f"{WEBDAV}/Files/Kunden", headers={**admin_auth, "Destination": _destination("/Files/Kopie")}
        )

        assert response.status_code == 403, response.text


class TestLocking:
    """Always granted, and deliberately so -- see ``webdav_lock``."""

    async def test_lock_returns_a_token_and_unlock_takes_it_back(
        self, async_client: AsyncClient, writable_webdav, admin_auth, file_factory
    ):
        await file_factory("part.3mf")

        locked = await async_client.request(
            "LOCK",
            f"{WEBDAV}/Files/part.3mf",
            headers=admin_auth,
            content=b'<?xml version="1.0"?><D:lockinfo xmlns:D="DAV:"><D:lockscope><D:exclusive/></D:lockscope>'
            b"<D:locktype><D:write/></D:locktype></D:lockinfo>",
        )

        assert locked.status_code == 200, locked.text
        token = locked.headers["Lock-Token"]
        assert token.startswith("<opaquelocktoken:")
        body = ET.fromstring(locked.content)
        assert body.findtext(f"{DAV}lockdiscovery/{DAV}activelock/{DAV}locktoken/{DAV}href") == token.strip("<>")
        assert body.find(f"{DAV}lockdiscovery/{DAV}activelock/{DAV}locktype/{DAV}write") is not None

        released = await async_client.request(
            "UNLOCK", f"{WEBDAV}/Files/part.3mf", headers={**admin_auth, "Lock-Token": token}
        )
        assert released.status_code == 204, released.text

    async def test_locking_a_file_that_does_not_exist_yet_is_201_and_creates_nothing(
        self, async_client: AsyncClient, writable_webdav, admin_auth, db_session
    ):
        """Windows locks the path before it writes it (RFC 4918 9.10.4)."""
        response = await async_client.request("LOCK", f"{WEBDAV}/Files/new.3mf", headers=admin_auth)

        assert response.status_code == 201, response.text
        assert response.headers["Lock-Token"]
        assert await _rows(db_session, filename="new.3mf") == []

    async def test_a_write_carrying_an_if_header_is_not_second_guessed(
        self, async_client: AsyncClient, writable_webdav, admin_auth
    ):
        """Nothing was recorded, so nothing can be checked -- by design."""
        locked = await async_client.request("LOCK", f"{WEBDAV}/Files/part.txt", headers=admin_auth)
        token = locked.headers["Lock-Token"]

        written = await async_client.request(
            "PUT",
            f"{WEBDAV}/Files/part.txt",
            headers={**admin_auth, "If": f"({token})"},
            content=b"payload",
        )

        assert written.status_code == 201, written.text


class TestProppatch:
    async def test_the_windows_timestamp_properties_are_answered_200(
        self, async_client: AsyncClient, writable_webdav, admin_auth, file_factory
    ):
        """Explorer sends this straight after the PUT and reads a 405 as failure.

        Bytes safely on disk, dialog saying the save did not work -- which is
        why these are accepted, and then ignored: the row's timestamps are
        Bambuddy's.
        """
        await file_factory("part.3mf")

        response = await async_client.request(
            "PROPPATCH",
            f"{WEBDAV}/Files/part.3mf",
            headers=admin_auth,
            content=b'<?xml version="1.0"?>'
            b'<D:propertyupdate xmlns:D="DAV:" xmlns:Z="urn:schemas-microsoft-com:">'
            b"<D:set><D:prop>"
            b"<Z:Win32CreationTime>Tue, 23 Sep 2026 10:00:00 GMT</Z:Win32CreationTime>"
            b"<Z:Win32LastModifiedTime>Tue, 23 Sep 2026 10:00:00 GMT</Z:Win32LastModifiedTime>"
            b"<Z:Win32FileAttributes>00000020</Z:Win32FileAttributes>"
            b"</D:prop></D:set></D:propertyupdate>",
        )

        assert response.status_code == 207, response.text
        root = ET.fromstring(response.content)
        propstats = root.findall(f"{DAV}response/{DAV}propstat")
        assert [stat.findtext(f"{DAV}status") for stat in propstats] == ["HTTP/1.1 200 OK"]
        named = {element.tag.rpartition("}")[2] for element in propstats[0].find(f"{DAV}prop")}
        assert named == {"Win32CreationTime", "Win32LastModifiedTime", "Win32FileAttributes"}

    async def test_a_property_the_projection_has_nowhere_to_keep_is_refused_per_property(
        self, async_client: AsyncClient, writable_webdav, admin_auth, file_factory
    ):
        await file_factory("part.3mf")

        response = await async_client.request(
            "PROPPATCH",
            f"{WEBDAV}/Files/part.3mf",
            headers=admin_auth,
            content=b'<?xml version="1.0"?>'
            b'<D:propertyupdate xmlns:D="DAV:" xmlns:Z="urn:schemas-microsoft-com:">'
            b"<D:set><D:prop>"
            b"<Z:Win32FileAttributes>00000020</Z:Win32FileAttributes>"
            b'<X:mood xmlns:X="urn:example:">cheerful</X:mood>'
            b"</D:prop></D:set></D:propertyupdate>",
        )

        assert response.status_code == 207, response.text
        root = ET.fromstring(response.content)
        statuses = [stat.findtext(f"{DAV}status") for stat in root.findall(f"{DAV}response/{DAV}propstat")]
        assert statuses == ["HTTP/1.1 200 OK", "HTTP/1.1 403 Forbidden"]

    async def test_proppatch_on_a_path_that_is_not_there_is_404(
        self, async_client: AsyncClient, writable_webdav, admin_auth
    ):
        response = await async_client.request(
            "PROPPATCH",
            f"{WEBDAV}/Files/ghost.3mf",
            headers=admin_auth,
            content=b'<D:propertyupdate xmlns:D="DAV:"><D:set><D:prop/></D:set></D:propertyupdate>',
        )

        assert response.status_code == 404, response.text


class TestWriteSafety:
    """The part that decides whether this feature can lose somebody's work."""

    @pytest.fixture
    async def reader(self, user_factory):
        await user_factory("davreader", permissions=["library:read_all"])
        return _basic("davreader", "DavPass1!")

    async def test_a_read_only_user_reads_and_is_refused_every_write(
        self, async_client: AsyncClient, writable_webdav, admin_auth, reader, file_factory, folder_factory, db_session
    ):
        """The drive is usable read-only for them, with no other change."""
        folder = await folder_factory("Kunden")
        await file_factory("part.3mf", b"the-model", folder_id=folder.id)

        listed = await async_client.request("PROPFIND", f"{WEBDAV}/Files/Kunden", headers={**reader, "Depth": "1"})
        assert listed.status_code == 207, listed.text
        body = await async_client.get(f"{WEBDAV}/Files/Kunden/part.3mf", headers=reader)
        assert body.content == b"the-model"

        attempts = {
            "PUT": (f"{WEBDAV}/Files/Kunden/new.3mf", {}),
            "DELETE": (f"{WEBDAV}/Files/Kunden/part.3mf", {}),
            "MKCOL": (f"{WEBDAV}/Files/Neu", {}),
            "MOVE": (f"{WEBDAV}/Files/Kunden/part.3mf", {"Destination": _destination("/Files/moved.3mf")}),
            "COPY": (f"{WEBDAV}/Files/Kunden/part.3mf", {"Destination": _destination("/Files/copy.3mf")}),
            "LOCK": (f"{WEBDAV}/Files/Kunden/part.3mf", {}),
            "UNLOCK": (f"{WEBDAV}/Files/Kunden/part.3mf", {}),
            "PROPPATCH": (f"{WEBDAV}/Files/Kunden/part.3mf", {}),
        }
        for method, (url, extra) in attempts.items():
            response = await async_client.request(method, url, headers={**reader, **extra})
            assert response.status_code == 403, f"{method} -> {response.status_code}: {response.text}"

        assert [row.filename for row in await _rows(db_session, folder_id=folder.id)] == ["part.3mf"]
        assert (await _rows(db_session, folder_id=folder.id))[0].deleted_at is None

    async def test_a_read_only_external_folder_refuses_every_write(
        self, async_client: AsyncClient, writable_webdav, admin_auth, external_folder_factory, db_session
    ):
        """The same 403 the REST routes give it, and the share stays untouched."""
        folder, directory = await external_folder_factory("Archiv", readonly=True)
        (directory / "part.3mf").write_bytes(b"not ours")
        from backend.app.models.library import LibraryFile

        db_session.add(
            LibraryFile(
                filename="part.3mf",
                file_path=str(directory / "part.3mf"),
                file_type="3mf",
                file_size=8,
                folder_id=folder.id,
                is_external=True,
            )
        )
        await db_session.commit()

        attempts = {
            "PUT": (f"{WEBDAV}/External/Archiv/new.3mf", {}),
            "DELETE": (f"{WEBDAV}/External/Archiv/part.3mf", {}),
            "MKCOL": (f"{WEBDAV}/External/Archiv/Neu", {}),
            "COPY": (f"{WEBDAV}/External/Archiv/part.3mf", {"Destination": _destination("/External/Archiv/c.3mf")}),
            "MOVE": (f"{WEBDAV}/External/Archiv/part.3mf", {"Destination": _destination("/External/Archiv/m.3mf")}),
        }
        for method, (url, extra) in attempts.items():
            response = await async_client.request(method, url, headers={**admin_auth, **extra})
            assert response.status_code == 403, f"{method} -> {response.status_code}: {response.text}"

        assert sorted(entry.name for entry in directory.iterdir()) == ["part.3mf"]
        assert (directory / "part.3mf").read_bytes() == b"not ours"
        assert (await _only_row(db_session, folder_id=folder.id)).deleted_at is None

    @pytest.mark.parametrize(
        "path",
        [
            # Percent-encoded, because httpx normalises a literal ".." out of
            # the URL before it is ever sent -- these are the forms a hostile
            # client would actually put on the wire.
            "/Files/%2E%2E/%2E%2E/escape.txt",
            "/Files/..%2F..%2Fescape.txt",
            "/Files/%2E%2E",
            "/%2E%2E/escape.txt",
        ],
    )
    async def test_a_put_whose_path_leaves_the_library_is_403(
        self, async_client: AsyncClient, writable_webdav, admin_auth, path, tmp_path
    ):
        response = await async_client.request("PUT", f"{WEBDAV}{path}", headers=admin_auth, content=b"pwned")

        assert response.status_code == 403, f"{path} -> {response.status_code}: {response.text}"
        assert not (tmp_path / "escape.txt").exists()

    @pytest.mark.parametrize("header", ["http://elsewhere.example/dav/x.3mf", "/other/x.3mf", "/webdavious/x.3mf"])
    async def test_a_destination_outside_the_share_is_403(
        self, async_client: AsyncClient, writable_webdav, admin_auth, header
    ):
        await async_client.request("PUT", f"{WEBDAV}/Files/part.txt", headers=admin_auth, content=b"x")

        response = await async_client.request(
            "MOVE", f"{WEBDAV}/Files/part.txt", headers={**admin_auth, "Destination": header}
        )

        assert response.status_code == 403, response.text

    async def test_a_name_the_library_cannot_store_is_refused(
        self, async_client: AsyncClient, writable_webdav, admin_auth, db_session
    ):
        """The SD-card rules the REST upload applies (#1540), here too."""
        response = await async_client.request("PUT", f"{WEBDAV}/Files/part%3Aone.3mf", headers=admin_auth, content=b"x")

        assert response.status_code == 400, response.text
        assert await _rows(db_session) == []

    async def test_a_name_the_projection_would_hand_back_differently_is_refused(
        self, async_client: AsyncClient, writable_webdav, admin_auth, db_session
    ):
        """A leading dot does not survive ``_label``, so the next PROPFIND
        would show the file under another name -- which reads as a write that
        went somewhere else.
        """
        response = await async_client.request("PUT", f"{WEBDAV}/Files/.gitignore", headers=admin_auth, content=b"x")

        assert response.status_code == 400, response.text
        assert await _rows(db_session) == []

    async def test_an_oversized_put_is_413_and_leaves_nothing_behind(
        self, async_client: AsyncClient, writable_webdav, admin_auth, db_session, monkeypatch, library_root
    ):
        """A mis-dragged folder must not be able to fill the SSD."""
        from backend.app.api.routes import webdav as webdav_module

        monkeypatch.setattr(webdav_module, "MAX_PUT_BYTES", 16)

        response = await async_client.request("PUT", f"{WEBDAV}/Files/big.3mf", headers=admin_auth, content=b"x" * 64)

        assert response.status_code == 413, response.text
        assert await _rows(db_session) == []
        files_dir = library_root / "library" / "files"
        assert not files_dir.exists() or list(files_dir.iterdir()) == []

    async def test_a_chunked_body_over_the_cap_is_413_too(
        self, async_client: AsyncClient, writable_webdav, admin_auth, db_session, monkeypatch, library_root
    ):
        """A chunked request declares no length, so the stream has to count."""
        from backend.app.api.routes import webdav as webdav_module

        monkeypatch.setattr(webdav_module, "MAX_PUT_BYTES", 16)

        async def _chunks():
            for _ in range(8):
                yield b"xxxxxxxx"

        response = await async_client.request("PUT", f"{WEBDAV}/Files/big.3mf", headers=admin_auth, content=_chunks())

        assert response.status_code == 413, response.text
        assert await _rows(db_session) == []
        files_dir = library_root / "library" / "files"
        assert not files_dir.exists() or list(files_dir.iterdir()) == []

    async def test_a_loose_file_cannot_be_written_into_the_external_bucket(
        self, async_client: AsyncClient, writable_webdav, admin_auth, external_folder_factory
    ):
        """``External`` lists registered folders; it is not a directory itself."""
        await external_folder_factory("NAS")

        response = await async_client.request("PUT", f"{WEBDAV}/External/loose.3mf", headers=admin_auth, content=b"x")

        assert response.status_code == 403, response.text

    async def test_a_write_at_the_share_root_is_refused(self, async_client: AsyncClient, writable_webdav, admin_auth):
        response = await async_client.request("PUT", f"{WEBDAV}/part.3mf", headers=admin_auth, content=b"x")

        assert response.status_code == 403, response.text


class TestWriteEndToEnd:
    """The sequence a client actually performs, in the order it performs it."""

    async def test_a_client_locks_creates_writes_stamps_and_reads_back(
        self, async_client: AsyncClient, writable_webdav, admin_auth, folder_factory, db_session
    ):
        await folder_factory("Kunden")
        payload = _three_mf()

        created = await async_client.request("MKCOL", f"{WEBDAV}/Files/Kunden/2026", headers=admin_auth)
        assert created.status_code == 201, created.text

        locked = await async_client.request("LOCK", f"{WEBDAV}/Files/Kunden/2026/job.3mf", headers=admin_auth)
        assert locked.status_code == 201, locked.text
        token = locked.headers["Lock-Token"]

        empty = await async_client.request(
            "PUT", f"{WEBDAV}/Files/Kunden/2026/job.3mf", headers={**admin_auth, "If": f"({token})"}, content=b""
        )
        assert empty.status_code == 201, empty.text

        filled = await async_client.request(
            "PUT", f"{WEBDAV}/Files/Kunden/2026/job.3mf", headers={**admin_auth, "If": f"({token})"}, content=payload
        )
        assert filled.status_code == 204, filled.text

        stamped = await async_client.request(
            "PROPPATCH",
            f"{WEBDAV}/Files/Kunden/2026/job.3mf",
            headers=admin_auth,
            content=b'<D:propertyupdate xmlns:D="DAV:" xmlns:Z="urn:schemas-microsoft-com:"><D:set><D:prop>'
            b"<Z:Win32LastModifiedTime>Tue, 23 Sep 2026 10:00:00 GMT</Z:Win32LastModifiedTime>"
            b"</D:prop></D:set></D:propertyupdate>",
        )
        assert stamped.status_code == 207, stamped.text

        released = await async_client.request(
            "UNLOCK", f"{WEBDAV}/Files/Kunden/2026/job.3mf", headers={**admin_auth, "Lock-Token": token}
        )
        assert released.status_code == 204, released.text

        listed = await async_client.request(
            "PROPFIND", f"{WEBDAV}/Files/Kunden/2026", headers={**admin_auth, "Depth": "1"}
        )
        entry = _responses(listed.content)["/webdav/Files/Kunden/2026/job.3mf"]
        assert _text(entry, "getcontentlength") == str(len(payload))

        body = await async_client.get(f"{WEBDAV}/Files/Kunden/2026/job.3mf", headers=admin_auth)
        assert body.content == payload

        row = await _only_row(db_session, filename="job.3mf")
        assert row.ingest_pending is False
        assert row.file_metadata["print_time_seconds"] == 3600
