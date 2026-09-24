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
from pathlib import Path
from urllib.parse import unquote

import pytest
from httpx import AsyncClient

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
async def enable_webdav(db_session):
    from backend.app.models.settings import Settings

    db_session.add(Settings(key="webdav_enabled", value="true"))
    await db_session.commit()


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
        from datetime import datetime

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
        await folder_factory("Kunden")

        for method, url in (
            ("OPTIONS", WEBDAV),
            ("PROPFIND", f"{WEBDAV}/"),
            ("GET", f"{WEBDAV}/Files"),
            ("PUT", f"{WEBDAV}/Files/x.3mf"),
        ):
            response = await async_client.request(method, url, headers=admin_auth)
            assert response.status_code == 404, f"{method} {url} -> {response.status_code}"

    async def test_the_setting_off_hides_the_share_from_an_unauthenticated_caller_too(self, async_client: AsyncClient):
        """404 before 401 — a 401 would announce that the endpoint is there."""
        response = await async_client.request("PROPFIND", f"{WEBDAV}/", headers={"Depth": "1"})
        assert response.status_code == 404, response.text


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
    """``webdav_enabled`` is the switch the whole feature hangs off."""

    async def test_it_defaults_to_off(self, async_client: AsyncClient):
        """An install that has never seen the setting exposes nothing."""
        response = await async_client.get("/api/v1/settings/")

        assert response.status_code == 200, response.text
        assert response.json()["webdav_enabled"] is False

    async def test_it_round_trips_as_a_boolean(self, async_client: AsyncClient):
        patched = await async_client.patch("/api/v1/settings/", json={"webdav_enabled": True})
        assert patched.status_code == 200, patched.text
        assert patched.json()["webdav_enabled"] is True

        reread = await async_client.get("/api/v1/settings/")
        assert reread.json()["webdav_enabled"] is True

    async def test_turning_it_on_through_the_api_opens_the_share(
        self, async_client: AsyncClient, admin_auth, folder_factory
    ):
        await folder_factory("Kunden")
        assert (await async_client.request("PROPFIND", f"{WEBDAV}/", headers=admin_auth)).status_code == 404

        await async_client.patch("/api/v1/settings/", json={"webdav_enabled": True})

        opened = await async_client.request("PROPFIND", f"{WEBDAV}/", headers=admin_auth)
        assert opened.status_code == 207, opened.text
