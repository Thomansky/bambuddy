"""A read-only WebDAV projection of the File Manager library (#3152).

On disk the managed library is flat and renamed (``archive/library/files/
<uuid>.3mf``) — right for hashing and de-duplication, useless from outside.
This router serves the same rows the File Manager tree is built from, in the
shape the user sees, so a workstation can map a drive and open a job folder in
Explorer instead of downloading one file at a time.

Read-only, deliberately: ``OPTIONS``, ``PROPFIND`` (Depth 0 and 1), ``HEAD``
and ``GET``. Every write method answers 405 with an ``Allow`` header. Writing
raises questions this feature does not answer — which folder owns a new file,
what hash, which project, what happens to the trash — and a half-answered
write path can damage a library.

The projection is virtual: nothing here touches the layout on disk. A path is
resolved by walking the tree one level at a time from the root, which is also
what makes the permission gate work — a file the caller may not read is not in
the listing its parent produces, so no path leads to it.
"""

from __future__ import annotations

import base64
import binascii
import mimetypes
import os
import xml.etree.ElementTree as ET
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import format_datetime
from pathlib import Path
from stat import S_ISREG
from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import FileResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.api.routes.library import (
    _ensure_library_file_visible,
    _unique_zip_name,
    _zip_entry_name,
    to_absolute_path,
)
from backend.app.api.routes.settings import get_setting, setting_is_true
from backend.app.core.auth import authenticate_user
from backend.app.core.database import get_db
from backend.app.core.permissions import Permission
from backend.app.models.library import LibraryFile, LibraryFolder
from backend.app.models.user import User

# include_in_schema=False for the whole router: PROPFIND is not an OpenAPI
# operation, and a /webdav entry in the API docs would suggest a REST surface
# that callers should use instead of /api/v1/library.
router = APIRouter(prefix="/webdav", tags=["webdav"], include_in_schema=False)

WEBDAV_PREFIX = "/webdav"

# Exactly what OPTIONS advertises and what a 405 names. Order is the one a
# reader expects, not alphabetical.
SUPPORTED_METHODS = ("OPTIONS", "PROPFIND", "HEAD", "GET")
ALLOW_HEADER = ", ".join(SUPPORTED_METHODS)

# Refused with 405 rather than left to the framework, so a client gets the
# Allow header that tells it what this share does support.
UNSUPPORTED_METHODS = ["PUT", "DELETE", "MKCOL", "MOVE", "COPY", "PROPPATCH", "LOCK", "UNLOCK"]

DAV_NS = "DAV:"
ET.register_namespace("D", DAV_NS)

# The two top-level collections. Managed and external folders stay apart here
# the way they do in the File Manager: one share, two buckets, so a path can
# never be ambiguous about which side of the #124 boundary it came from.
BUCKET_MANAGED = "Files"
BUCKET_EXTERNAL = "External"

# Timestamp for the root and the two buckets. They are synthetic containers
# with no row behind them, and a value derived from their contents would move
# under a client that caches by getlastmodified. A constant is stable and
# honest about there being nothing to date.
SYNTHETIC_TIMESTAMP = datetime(1970, 1, 1, tzinfo=timezone.utc)

_BASIC_CHALLENGE = {"WWW-Authenticate": 'Basic realm="Bambuddy"'}


@dataclass(frozen=True)
class _Entry:
    """One node of the projected tree.

    ``name`` is the disambiguated path segment, not the raw stored name: two
    files called ``part.3mf`` in one folder are ``part.3mf`` and
    ``part (2).3mf`` here, and that is the name every request has to use.
    """

    name: str
    is_collection: bool
    size: int
    created_at: datetime
    modified_at: datetime
    folder: LibraryFolder | None = None
    file: LibraryFile | None = None
    bucket: str | None = None


def _unauthorized() -> HTTPException:
    return HTTPException(status_code=401, detail="WebDAV requires credentials", headers=dict(_BASIC_CHALLENGE))


def _not_found() -> HTTPException:
    return HTTPException(status_code=404, detail="Not found")


def _method_not_allowed() -> HTTPException:
    return HTTPException(status_code=405, detail="Method not allowed", headers={"Allow": ALLOW_HEADER})


async def webdav_principal(
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> tuple[User, bool]:
    """Resolve the caller, or refuse the request.

    Three gates, in this order:

    1. ``webdav_enabled`` off → 404 for everything, so an install that has not
       turned the feature on is indistinguishable from one that never had it.
       This has to precede authentication: a 401 would announce the endpoint.
    2. HTTP Basic against the existing users. No OS WebDAV client carries the
       app's session cookie or an ``Authorization: Bearer`` header, so Basic is
       the only credential this protocol can present. Credentials are required
       even when authentication is disabled for the web UI — the alternative is
       serving a whole library anonymously on a second protocol, which is not a
       trade an operator should make by leaving a checkbox at its default.
    3. The library read permissions, the same pair the REST read routes use.
       ``can_read_all`` false means the caller sees only their own files.

    Returns ``(user, can_read_all)``.
    """
    if not setting_is_true(await get_setting(db, "webdav_enabled")):
        raise _not_found()

    header = request.headers.get("Authorization") or ""
    scheme, _, payload = header.partition(" ")
    if scheme.lower() != "basic" or not payload:
        raise _unauthorized()
    try:
        username, sep, password = base64.b64decode(payload, validate=True).decode("utf-8").partition(":")
    except (binascii.Error, UnicodeDecodeError, ValueError):
        raise _unauthorized()
    if not sep:
        raise _unauthorized()

    user = await authenticate_user(db, username, password)
    if user is None:
        raise _unauthorized()

    if user.has_permission(Permission.LIBRARY_READ_ALL.value):
        return user, True
    if user.has_permission(Permission.LIBRARY_READ_OWN.value):
        return user, False
    raise HTTPException(status_code=403, detail="Missing permission: library:read_own or library:read_all")


def _as_utc(value: datetime | None) -> datetime:
    """Read a library timestamp column as an aware UTC datetime.

    Bambuddy's ``DateTime`` columns are naive and hold UTC (see
    ``utils/local_time``). WebDAV dates carry a zone, so the offset has to be
    put back on before formatting or every date in the listing is wrong by the
    server's offset.
    """
    if value is None:
        return SYNTHETIC_TIMESTAMP
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _label(raw: str, fallback: str, taken: set[str]) -> str:
    """The path segment a stored name gets, disambiguated within its parent.

    ``_zip_entry_name`` reduces the name to exactly one component — a scanned
    external name can carry a separator or a ``..`` — and ``_unique_zip_name``
    is the ZIP export's collision rule, reused rather than reimplemented so a
    folder browsed over WebDAV and the same folder downloaded as a ZIP name
    their files identically.
    """
    return _unique_zip_name(_zip_entry_name(raw, fallback=fallback), taken)


def _folder_entry(folder: LibraryFolder, name: str) -> _Entry:
    return _Entry(
        name=name,
        is_collection=True,
        size=0,
        created_at=_as_utc(folder.created_at),
        modified_at=_as_utc(folder.fs_modified_at or folder.updated_at),
        folder=folder,
    )


def _file_entry(file: LibraryFile, name: str) -> _Entry:
    return _Entry(
        name=name,
        is_collection=False,
        size=file.file_size or 0,
        created_at=_as_utc(file.created_at),
        modified_at=_as_utc(file.fs_modified_at or file.updated_at),
        file=file,
    )


def _bucket_entry(bucket: str) -> _Entry:
    return _Entry(
        name=bucket,
        is_collection=True,
        size=0,
        created_at=SYNTHETIC_TIMESTAMP,
        modified_at=SYNTHETIC_TIMESTAMP,
        bucket=bucket,
    )


def _root_entry() -> _Entry:
    return _Entry(
        name="",
        is_collection=True,
        size=0,
        created_at=SYNTHETIC_TIMESTAMP,
        modified_at=SYNTHETIC_TIMESTAMP,
        bucket="",
    )


async def _has_external_content(db: AsyncSession) -> bool:
    """Whether the ``External`` bucket is worth showing at all.

    Either an external folder at the top level, or a file in no folder that
    came from an external scan — the File Manager counts those separately
    (``unfoldered_external_files``) and so must this, or an install whose only
    external content is loose files would have no path to it.
    """
    folder = await db.execute(
        select(LibraryFolder.id).where(LibraryFolder.parent_id.is_(None), LibraryFolder.is_external.is_(True)).limit(1)
    )
    if folder.scalar_one_or_none() is not None:
        return True
    loose = await db.execute(
        LibraryFile.active().where(LibraryFile.folder_id.is_(None), LibraryFile.is_external.is_(True)).limit(1)
    )
    return loose.scalars().first() is not None


async def _visible_files(
    db: AsyncSession,
    folder_id: int | None,
    user: User,
    can_read_all: bool,
    *,
    external: bool | None = None,
) -> list[LibraryFile]:
    """Non-trashed files of one folder the caller is allowed to see.

    ``_ensure_library_file_visible`` is the same gate ``download_file`` runs,
    called here for its verdict rather than its exception so an unreadable row
    simply is not in the listing. A user who cannot read a file must not learn
    it exists from the directory it sits in.
    """
    query = LibraryFile.active().where(LibraryFile.folder_id == folder_id).order_by(LibraryFile.id)
    if external is not None:
        query = query.where(LibraryFile.is_external.is_(external))
    rows = (await db.execute(query)).scalars().all()
    visible: list[LibraryFile] = []
    for row in rows:
        try:
            _ensure_library_file_visible(row, user, can_read_all)
        except HTTPException:
            continue
        visible.append(row)
    return visible


async def _subfolders(db: AsyncSession, parent_id: int | None, *, external: bool | None = None):
    query = select(LibraryFolder).where(LibraryFolder.parent_id == parent_id).order_by(LibraryFolder.id)
    if external is not None:
        query = query.where(LibraryFolder.is_external.is_(external))
    return (await db.execute(query)).scalars().all()


async def _children(db: AsyncSession, entry: _Entry, user: User, can_read_all: bool) -> list[_Entry]:
    """The direct children of a collection, with their final path segments.

    Ordered by id and disambiguated in that order, so a name a client saw in
    one request is the same name in the next. Ordering by anything the database
    chooses would let a file move under Explorer's feet.
    """
    if not entry.is_collection:
        return []

    taken: set[str] = set()
    children: list[_Entry] = []

    if entry.bucket == "":
        children.append(_bucket_entry(BUCKET_MANAGED))
        if await _has_external_content(db):
            children.append(_bucket_entry(BUCKET_EXTERNAL))
        return children

    if entry.bucket in (BUCKET_MANAGED, BUCKET_EXTERNAL):
        is_external = entry.bucket == BUCKET_EXTERNAL
        for folder in await _subfolders(db, None, external=is_external):
            children.append(_folder_entry(folder, _label(folder.name, f"folder-{folder.id}", taken)))
        # Files belonging to no folder would otherwise have no path at all.
        # They sit directly in the bucket rather than behind the File Manager's
        # synthetic "No folder" entry, which is a UI affordance and not a
        # directory anyone would want to type.
        for file in await _visible_files(db, None, user, can_read_all, external=is_external):
            children.append(_file_entry(file, _label(file.filename, f"file-{file.id}", taken)))
        return children

    if entry.folder is None:  # pragma: no cover - every collection is a bucket or a folder
        return children
    for folder in await _subfolders(db, entry.folder.id):
        children.append(_folder_entry(folder, _label(folder.name, f"folder-{folder.id}", taken)))
    for file in await _visible_files(db, entry.folder.id, user, can_read_all):
        children.append(_file_entry(file, _label(file.filename, f"file-{file.id}", taken)))
    return children


def _split_path(dav_path: str) -> list[str]:
    return [segment for segment in dav_path.split("/") if segment]


async def _resolve(db: AsyncSession, segments: Sequence[str], user: User, can_read_all: bool) -> _Entry:
    """Walk the projection from the root to *segments*, or raise 404.

    Level by level rather than by a path lookup, because each level's listing
    is the thing that applies the permission and trash filters — resolving a
    path any other way would let a request reach a row the listing hides.
    """
    entry = _root_entry()
    for segment in segments:
        if not entry.is_collection:
            raise _not_found()
        match = next((child for child in await _children(db, entry, user, can_read_all) if child.name == segment), None)
        if match is None:
            raise _not_found()
        entry = match
    return entry


def _href(root_path: str, segments: Sequence[str], is_collection: bool) -> str:
    """The href for a projected path, percent-encoded one segment at a time.

    ``safe=""`` so nothing structural survives into the URL: a folder called
    ``Kunden & Co`` and a file called ``Stübe V60 -H2D.3mf`` both carry
    characters that change what a URL means if they are left as they are, and
    a wrong encoding here is the single likeliest reason a mapped drive shows
    up empty.
    """
    href = f"{root_path}{WEBDAV_PREFIX}"
    encoded = "/".join(quote(segment, safe="") for segment in segments)
    if encoded:
        href = f"{href}/{encoded}"
    if is_collection:
        href = f"{href}/"
    return href


def _response_element(parent: ET.Element, href: str, entry: _Entry) -> None:
    response = ET.SubElement(parent, f"{{{DAV_NS}}}response")
    ET.SubElement(response, f"{{{DAV_NS}}}href").text = href
    propstat = ET.SubElement(response, f"{{{DAV_NS}}}propstat")
    prop = ET.SubElement(propstat, f"{{{DAV_NS}}}prop")

    # The root contributes no path segment, so it has no name of its own; a
    # client that renders an empty displayname shows a blank row for the share.
    ET.SubElement(prop, f"{{{DAV_NS}}}displayname").text = entry.name or "Bambuddy"
    resourcetype = ET.SubElement(prop, f"{{{DAV_NS}}}resourcetype")
    if entry.is_collection:
        ET.SubElement(resourcetype, f"{{{DAV_NS}}}collection")
    ET.SubElement(prop, f"{{{DAV_NS}}}getcontentlength").text = str(entry.size)
    ET.SubElement(prop, f"{{{DAV_NS}}}getlastmodified").text = format_datetime(entry.modified_at, usegmt=True)
    ET.SubElement(prop, f"{{{DAV_NS}}}creationdate").text = entry.created_at.strftime("%Y-%m-%dT%H:%M:%S") + "Z"
    if not entry.is_collection and entry.file is not None:
        ET.SubElement(prop, f"{{{DAV_NS}}}getcontenttype").text = _media_type(entry.file.filename)

    ET.SubElement(propstat, f"{{{DAV_NS}}}status").text = "HTTP/1.1 200 OK"


def _multistatus(rows: Sequence[tuple[str, _Entry]]) -> Response:
    multistatus = ET.Element(f"{{{DAV_NS}}}multistatus")
    for href, entry in rows:
        _response_element(multistatus, href, entry)
    # xml_declaration=False explicitly: whether ``encoding="utf-8"`` alone emits
    # a declaration has changed between Python versions, and a body carrying two
    # of them is not XML at all — every client would show an empty drive.
    body = b'<?xml version="1.0" encoding="utf-8"?>\n' + ET.tostring(
        multistatus, encoding="utf-8", xml_declaration=False
    )
    return Response(content=body, status_code=207, media_type='application/xml; charset="utf-8"')


def _media_type(filename: str) -> str:
    guessed, _ = mimetypes.guess_type(filename)
    return guessed or "application/octet-stream"


def _resolved_bytes(file: LibraryFile) -> tuple[Path, os.stat_result]:
    """Absolute path plus stat for a file row, or 404 when the bytes are gone.

    A row whose file has been moved out from under Bambuddy, and a legacy row
    whose relative path escapes ``base_dir``, are both "not there" from a
    client's point of view — neither is a server error.
    """
    try:
        abs_path = to_absolute_path(file.file_path)
    except ValueError:
        raise _not_found()
    if abs_path is None:
        raise _not_found()
    try:
        info = abs_path.stat()
    except OSError:
        raise _not_found()
    # One stat answers both "is this a file" and "how big", and is handed to
    # FileResponse so the bytes are not stat-ed a second time.
    if not S_ISREG(info.st_mode):
        raise _not_found()
    return abs_path, info


@router.api_route("", methods=["OPTIONS"])
@router.api_route("/{dav_path:path}", methods=["OPTIONS"])
async def webdav_options(
    dav_path: str = "",
    principal: tuple[User, bool] = Depends(webdav_principal),
) -> Response:
    """Announce the protocol level and the four methods this share answers.

    ``MS-Author-Via`` is what the Windows WebClient looks for before it will
    treat the URL as a DAV share at all; without it a mapped drive fails with
    an unhelpful "folder is invalid".
    """
    return Response(
        status_code=200,
        headers={
            "DAV": "1",
            "MS-Author-Via": "DAV",
            "Allow": ALLOW_HEADER,
            "Content-Length": "0",
        },
    )


@router.api_route("", methods=["PROPFIND"])
@router.api_route("/{dav_path:path}", methods=["PROPFIND"])
async def webdav_propfind(
    request: Request,
    dav_path: str = "",
    db: AsyncSession = Depends(get_db),
    principal: tuple[User, bool] = Depends(webdav_principal),
) -> Response:
    """List an entry (Depth 0) or an entry and its children (Depth 1).

    The requested property set in the body is ignored: this projection has
    exactly one set of properties and returns all of them, which every client
    tested tolerates and keeps the parser out of the request path.

    ``Depth: infinity`` is refused with the ``propfind-finite-depth``
    precondition the RFC provides for it — the library is a whole NAS in one
    response otherwise. A request with no ``Depth`` at all is served as Depth 1
    rather than refused: no client omits it, and a mapped drive that shows one
    level is better than one that shows an error.
    """
    user, can_read_all = principal
    depth = (request.headers.get("Depth") or "1").strip().lower()
    if depth not in ("0", "1"):
        body = b'<?xml version="1.0" encoding="utf-8"?>\n<D:error xmlns:D="DAV:"><D:propfind-finite-depth/></D:error>'
        return Response(content=body, status_code=403, media_type='application/xml; charset="utf-8"')

    segments = _split_path(dav_path)
    entry = await _resolve(db, segments, user, can_read_all)

    root_path = request.scope.get("root_path", "")
    rows: list[tuple[str, _Entry]] = [(_href(root_path, segments, entry.is_collection), entry)]
    if depth == "1" and entry.is_collection:
        for child in await _children(db, entry, user, can_read_all):
            rows.append((_href(root_path, [*segments, child.name], child.is_collection), child))
    return _multistatus(rows)


@router.api_route("", methods=["GET", "HEAD"])
@router.api_route("/{dav_path:path}", methods=["GET", "HEAD"])
async def webdav_get(
    dav_path: str = "",
    db: AsyncSession = Depends(get_db),
    principal: tuple[User, bool] = Depends(webdav_principal),
):
    """Stream a file's bytes.

    ``FileResponse`` carries the single-``Range`` handling (206 plus
    ``Content-Range``) that slicers and Explorer's preview both use, and
    answers a HEAD with the headers alone. A collection has no bytes, so it
    answers 405 rather than inventing a directory listing.
    """
    user, can_read_all = principal
    entry = await _resolve(db, _split_path(dav_path), user, can_read_all)
    if entry.is_collection or entry.file is None:
        raise _method_not_allowed()

    abs_path, info = _resolved_bytes(entry.file)
    return FileResponse(
        abs_path,
        media_type=_media_type(entry.file.filename),
        stat_result=info,
    )


@router.api_route("", methods=UNSUPPORTED_METHODS)
@router.api_route("/{dav_path:path}", methods=UNSUPPORTED_METHODS)
async def webdav_unsupported(
    dav_path: str = "",
    principal: tuple[User, bool] = Depends(webdav_principal),
) -> Response:
    """Refuse every write method, naming what the share does support.

    Answered here rather than left to the framework so the 405 carries an
    ``Allow`` header; a client that sees the header stops retrying and reports
    a read-only share instead of a broken one.
    """
    raise _method_not_allowed()
