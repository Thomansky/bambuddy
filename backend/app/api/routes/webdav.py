"""A WebDAV projection of the File Manager library (#3152).

On disk the managed library is flat and renamed (``archive/library/files/
<uuid>.3mf``) — right for hashing and de-duplication, useless from outside.
This router serves the same rows the File Manager tree is built from, in the
shape the user sees, so a workstation can map a drive and open a job folder in
Explorer instead of downloading one file at a time.

Three modes, and the setting decides which: ``off`` answers 404 for everything,
``read`` serves ``OPTIONS``, ``PROPFIND`` (Depth 0 and 1), ``HEAD`` and ``GET``
and refuses the rest with 405, and ``readwrite`` adds ``PUT``, ``DELETE``,
``MKCOL``, ``MOVE``, ``COPY``, ``LOCK``, ``UNLOCK`` and ``PROPPATCH``.

The projection is virtual: nothing here invents a layout on disk. A path is
resolved by walking the tree one level at a time from the root, which is also
what makes the permission gate work — a file the caller may not read is not in
the listing its parent produces, so no path leads to it. A write resolves its
*parent* the same way and then validates the one remaining segment, so the same
walk is the only way in and there is no second resolver to keep honest.

The write half is mostly about how clients actually write, which is nothing
like a single clean PUT: they create the file empty and fill it afterwards,
they write a temporary name and rename it into place, and they scatter
``desktop.ini`` into folders they only looked at. Each of those has its answer
below, next to the code that handles it.
"""

from __future__ import annotations

import base64
import binascii
import contextlib
import errno
import mimetypes
import os
import shutil
import uuid
import xml.etree.ElementTree as ET
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import format_datetime
from pathlib import Path
from stat import S_ISREG
from urllib.parse import quote, unquote, urlsplit

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import FileResponse
from fastapi.routing import APIRoute
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.routing import Match
from starlette.types import Receive, Scope, Send

from backend.app.api.routes.auth import (
    _get_client_ip,
    authenticate_credentials,
    resolve_second_factors,
)
from backend.app.api.routes.library import (
    _ensure_library_file_visible,
    _mtime_to_datetime,
    _resolve_source_disk_path,
    _resolve_upload_destination,
    _restricted_folder_delete_blocker,
    _stored_file_path,
    _unique_zip_name,
    _zip_entry_name,
    classify_file_type,
    delete_file,
    get_library_files_dir,
    get_library_thumbnails_dir,
    ingest_library_file_content,
    to_absolute_path,
)
from backend.app.api.routes.mfa import (
    MAX_LOGIN_ATTEMPTS,
    check_rate_limit,
    clear_failed_attempts,
    record_failed_attempt,
)
from backend.app.api.routes.settings import get_setting
from backend.app.core.database import get_db
from backend.app.core.permissions import Permission
from backend.app.models.auth_ephemeral import EventType
from backend.app.models.library import LibraryFile, LibraryFolder
from backend.app.models.user import User
from backend.app.schemas.settings import WEBDAV_MODES
from backend.app.utils.filename import InvalidFilenameError, validate_print_filename
from backend.app.utils.safe_path import safe_join_under

# include_in_schema=False for the whole router: PROPFIND is not an OpenAPI
# operation, and a /webdav entry in the API docs would suggest a REST surface
# that callers should use instead of /api/v1/library.
router = APIRouter(prefix="/webdav", tags=["webdav"], include_in_schema=False)

WEBDAV_PREFIX = "/webdav"

MODE_OFF, MODE_READ, MODE_READWRITE = WEBDAV_MODES

# What ``read`` answers, in the order a reader expects rather than alphabetical.
READ_METHODS = ("OPTIONS", "PROPFIND", "HEAD", "GET")
# What ``readwrite`` adds. LOCK and UNLOCK are in here because Windows takes a
# lock before it writes and reads a refusal as "the share is read-only", and
# PROPPATCH because Explorer sets timestamps straight after a PUT and reports
# the save as failed if that is refused — neither is optional in practice.
WRITE_METHODS = ("PUT", "DELETE", "MKCOL", "MOVE", "COPY", "LOCK", "UNLOCK", "PROPPATCH")

# Methods no mode implements. Everything outside the two lists above is refused
# the same way — see _AnyMethodRoute — so this documents rather than decides.
UNROUTED_METHODS = ["POST", "PATCH", "REPORT", "SEARCH"]

# Files an operating system writes into folders it merely *looks* at. A PUT of
# one is accepted and dropped, and the name never appears in a listing, because
# the alternative is a library that grows a desktop.ini row for every folder a
# Windows user ever opened. Deliberately a fixed list and not a pattern: a
# ``.tmp`` may well be the user's own file, and guessing wrong loses it.
JUNK_NAMES = frozenset({"desktop.ini", "thumbs.db", "ehthumbs.db", ".ds_store"})
JUNK_PREFIXES = ("._",)

# Ceiling on one PUT. The REST upload route has none, so this is the new
# number: big enough for any sliced project, small enough that a folder dragged
# onto the drive by accident cannot fill the SSD before anyone notices. The
# body is streamed to disk rather than buffered, so this is a storage guard and
# not a memory one.
MAX_PUT_BYTES = 1024 * 1024 * 1024

# How long a granted lock claims to last. Nothing enforces it — see
# ``webdav_lock`` — so this is only what a client is told.
LOCK_TIMEOUT_SECONDS = 3600

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


@dataclass(frozen=True)
class _Principal:
    """Who is asking, how much they may see, and what the share allows today.

    ``mode`` travels with the caller rather than being read again per handler:
    every refusal has to name the same ``Allow`` the same request's OPTIONS
    would, and re-reading the setting mid-request could disagree with itself.
    """

    user: User
    can_read_all: bool
    mode: str


def _allow_header(mode: str) -> str:
    methods = READ_METHODS + WRITE_METHODS if mode == MODE_READWRITE else READ_METHODS
    return ", ".join(methods)


def _unauthorized() -> HTTPException:
    return HTTPException(status_code=401, detail="WebDAV requires credentials", headers=dict(_BASIC_CHALLENGE))


def _not_found() -> HTTPException:
    return HTTPException(status_code=404, detail="Not found")


def _method_not_allowed(mode: str, detail: str = "Method not allowed") -> HTTPException:
    return HTTPException(status_code=405, detail=detail, headers={"Allow": _allow_header(mode)})


async def webdav_mode(db: AsyncSession) -> str:
    """The share's mode, normalised, defaulting to ``off``.

    Read straight from the settings row rather than through the settings
    response: a single unrecognised value must fail closed here, not fall back
    to a mode that serves anything.
    """
    stored = (await get_setting(db, "webdav_mode") or "").strip().lower()
    return stored if stored in WEBDAV_MODES else MODE_OFF


async def webdav_principal(
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> _Principal:
    """Resolve the caller, or refuse the request.

    Five gates, in this order:

    1. ``webdav_mode`` off → 404 for everything, so an install that has not
       turned the feature on is indistinguishable from one that never had it.
       This has to precede authentication: a 401 would announce the endpoint.
    2. The two login rate-limit buckets, the ones ``POST /auth/login`` uses —
       the same buckets, not a second pair, or an attacker locked out of the
       login route would simply get another ten guesses here. Without them
       this would be the one unthrottled credential endpoint in the app, and
       ``verify_password`` is synchronous pbkdf2 on the event loop that also
       carries printer MQTT traffic.
    3. HTTP Basic through ``authenticate_credentials``, the login route's own
       credential path: the configured directory first, then the #1589
       ``local_login_enabled`` switch, then the local hash. Basic is the only
       credential this protocol can present — no OS WebDAV client carries the
       app's session cookie or a Bearer header — and credentials are required
       even when authentication is disabled for the web UI, because the
       alternative is serving a whole library anonymously on a second protocol.
    4. Two-factor accounts are refused outright. Basic has nowhere to put a
       challenge, so serving one on the password alone would make a password
       worth more here than it is in the browser — the exact thing the second
       factor was turned on to prevent.
    5. The library read permissions, the same pair the REST read routes use.
       ``can_read_all`` false means the caller sees only their own files.
       Read permission is the floor for reaching the share at all; each write
       method asks for its own permission on top, where it happens.
    """
    mode = await webdav_mode(db)
    if mode == MODE_OFF:
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

    client_ip = _get_client_ip(request)
    recent_failures = await check_rate_limit(
        db, username, event_type=EventType.LOGIN_ATTEMPT, max_attempts=MAX_LOGIN_ATTEMPTS
    )
    await check_rate_limit(db, client_ip, event_type=EventType.LOGIN_IP, max_attempts=20)

    user = await authenticate_credentials(db, username, password)
    if user is None:
        await record_failed_attempt(db, username, event_type=EventType.LOGIN_ATTEMPT)
        await record_failed_attempt(db, client_ip, event_type=EventType.LOGIN_IP)
        raise _unauthorized()

    # Only when there is something to clear: a mapped drive authenticates on
    # every request, and an unconditional DELETE plus commit per PROPFIND is a
    # write the share does not need.
    if recent_failures:
        await clear_failed_attempts(db, username, event_type=EventType.LOGIN_ATTEMPT)
        await clear_failed_attempts(db, client_ip, event_type=EventType.LOGIN_IP)

    totp_enabled, email_otp_enabled = await resolve_second_factors(db, user)
    if totp_enabled or email_otp_enabled:
        # 403, not another 401: a Basic challenge would make Explorer re-prompt
        # for a password that is already correct, forever.
        raise HTTPException(
            status_code=403,
            detail="This account uses two-factor authentication, which WebDAV cannot ask for",
        )

    if user.has_permission(Permission.LIBRARY_READ_ALL.value):
        return _Principal(user, True, mode)
    if user.has_permission(Permission.LIBRARY_READ_OWN.value):
        return _Principal(user, False, mode)
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


def _media_type(filename: str) -> str:
    guessed, _ = mimetypes.guess_type(filename)
    return guessed or "application/octet-stream"


def _bytes_on_disk(file: LibraryFile) -> tuple[Path, os.stat_result] | None:
    """Absolute path plus stat for a file row, or None when the bytes are gone.

    A row whose file has been moved out from under Bambuddy, and a legacy row
    whose relative path escapes ``base_dir``, are both "not there" from a
    client's point of view — neither is a server error.

    One stat answers "is this a file", "how big" and "how old" at once, and is
    handed to ``FileResponse`` so a transfer does not stat the same file twice.
    """
    try:
        abs_path = to_absolute_path(file.file_path)
    except ValueError:
        return None
    if abs_path is None:
        return None
    try:
        info = abs_path.stat()
    except OSError:
        return None
    if not S_ISREG(info.st_mode):
        return None
    return abs_path, info


def _resolved_bytes(file: LibraryFile) -> tuple[Path, os.stat_result]:
    """``_bytes_on_disk`` for a caller that wants the bytes, not a verdict."""
    found = _bytes_on_disk(file)
    if found is None:
        raise _not_found()
    return found


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
    """A file's entry, sized and dated from the bytes where the bytes exist.

    The row's ``file_size`` and ``fs_modified_at`` are only the fallback, for a
    file that is not on disk at all. PROPFIND has to describe what GET will
    hand over, and GET hands ``FileResponse`` a real ``stat`` — for an external
    (#124) file the two drift the moment someone rewrites it on the share
    between scans, and ``updated_at`` moves for a tag edit that never touched
    the bytes. A listing that disagrees with the transfer makes rclone delete
    the copy it just made ("sizes differ") and turns a resumed download's
    ``If-Range`` into a silent restart from byte 0.
    """
    found = _bytes_on_disk(file)
    if found is None:
        size = file.file_size or 0
        modified = _as_utc(file.fs_modified_at or file.updated_at)
    else:
        size = found[1].st_size
        # Truncated to whole seconds, the resolution FileResponse's own
        # Last-Modified carries: the two have to render as the same string or a
        # client's If-Range does not validate against the response it gets.
        modified = datetime.fromtimestamp(int(found[1].st_mtime), timezone.utc)
    return _Entry(
        name=name,
        is_collection=False,
        size=size,
        created_at=_as_utc(file.created_at),
        modified_at=modified,
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


def _is_junk_name(name: str) -> bool:
    """Whether *name* is an operating system's own litter rather than a file."""
    lowered = name.lower()
    return lowered in JUNK_NAMES or lowered.startswith(JUNK_PREFIXES)


async def _visible_files(
    db: AsyncSession,
    folder_id: int | None,
    principal: _Principal,
    *,
    external: bool | None = None,
) -> list[LibraryFile]:
    """Non-trashed files of one folder the caller is allowed to see.

    ``_ensure_library_file_visible`` is the same gate ``download_file`` runs,
    called here for its verdict rather than its exception so an unreadable row
    simply is not in the listing. A user who cannot read a file must not learn
    it exists from the directory it sits in.

    Junk names are dropped here rather than at the writes that accept them: an
    external scan can have picked up a ``Thumbs.db`` long before this feature
    existed, and the rule is about what the share shows, not about who wrote it.
    """
    query = LibraryFile.active().where(LibraryFile.folder_id == folder_id).order_by(LibraryFile.id)
    if external is not None:
        query = query.where(LibraryFile.is_external.is_(external))
    rows = (await db.execute(query)).scalars().all()
    visible: list[LibraryFile] = []
    for row in rows:
        if _is_junk_name(row.filename):
            continue
        try:
            _ensure_library_file_visible(row, principal.user, principal.can_read_all)
        except HTTPException:
            continue
        visible.append(row)
    return visible


async def _has_external_content(db: AsyncSession, principal: _Principal) -> bool:
    """Whether the ``External`` bucket is worth showing at all.

    Either an external folder at the top level, or a file in no folder that
    came from an external scan — the File Manager counts those separately
    (``unfoldered_external_files``) and so must this, or an install whose only
    external content is loose files would have no path to it.

    The loose-file test runs the same visibility gate the bucket's listing
    runs. Asking a looser question here than the listing answers would put an
    empty ``External/`` in front of a ``read_own`` caller, and its mere
    presence is the fact the per-file gate exists to withhold.
    """
    folder = await db.execute(
        select(LibraryFolder.id).where(LibraryFolder.parent_id.is_(None), LibraryFolder.is_external.is_(True)).limit(1)
    )
    if folder.scalar_one_or_none() is not None:
        return True
    return bool(await _visible_files(db, None, principal, external=True))


async def _subfolders(db: AsyncSession, parent_id: int | None, *, external: bool | None = None):
    query = select(LibraryFolder).where(LibraryFolder.parent_id == parent_id).order_by(LibraryFolder.id)
    if external is not None:
        query = query.where(LibraryFolder.is_external.is_(external))
    return (await db.execute(query)).scalars().all()


async def _children(db: AsyncSession, entry: _Entry, principal: _Principal) -> list[_Entry]:
    """The direct children of a collection, with their final path segments.

    Disambiguated oldest first — ``created_at``, then kind and id so the order
    is total — rather than every folder and then every file. Both orders are
    stable across requests, but only this one leaves an entry's name alone
    when a sibling turns up: under folders-first, a new folder called
    ``part.3mf`` takes the name the file of that name has had all along and
    pushes the file to ``part (2).3mf``, which is exactly the "a file moves
    under Explorer's feet" failure a stable rule is for. The newcomer gets the
    suffix instead.
    """
    if not entry.is_collection:
        return []

    if entry.bucket == "":
        children = [_bucket_entry(BUCKET_MANAGED)]
        if await _has_external_content(db, principal):
            children.append(_bucket_entry(BUCKET_EXTERNAL))
        return children

    if entry.bucket in (BUCKET_MANAGED, BUCKET_EXTERNAL):
        is_external = entry.bucket == BUCKET_EXTERNAL
        folders = await _subfolders(db, None, external=is_external)
        # Files belonging to no folder would otherwise have no path at all.
        # They sit directly in the bucket rather than behind the File Manager's
        # synthetic "No folder" entry, which is a UI affordance and not a
        # directory anyone would want to type.
        files = await _visible_files(db, None, principal, external=is_external)
    elif entry.folder is not None:
        folders = await _subfolders(db, entry.folder.id)
        files = await _visible_files(db, entry.folder.id, principal)
    else:  # pragma: no cover - every collection is a bucket or a folder
        return []

    # Kind is the tiebreak, not the primary key: the timestamp columns have
    # one-second resolution, so two siblings made in the same second have to
    # fall back to something, and it may as well be deterministic.
    rows: list[tuple[datetime, int, int, LibraryFolder | LibraryFile]] = [
        (_as_utc(folder.created_at), 0, folder.id, folder) for folder in folders
    ]
    rows += [(_as_utc(file.created_at), 1, file.id, file) for file in files]
    rows.sort(key=lambda row: row[:3])

    taken: set[str] = set()
    children = []
    for _created, kind, row_id, row in rows:
        if kind == 0:
            children.append(_folder_entry(row, _label(row.name, f"folder-{row_id}", taken)))
        else:
            children.append(_file_entry(row, _label(row.filename, f"file-{row_id}", taken)))
    return children


def _split_path(dav_path: str) -> list[str]:
    return [segment for segment in dav_path.split("/") if segment]


def _match_child(children: Sequence[_Entry], name: str) -> _Entry | None:
    """The child a path segment names, matched the way the client matches it.

    Exactly first, then case-insensitively. Windows is case-insensitive and
    Explorer decides from its own listing: it compares the name it is about to
    write against what PROPFIND returned, offers "replace the file in the
    destination?" — and then writes using the *source* file's spelling. A
    case-sensitive lookup turns that confirmed replace into a second row,
    leaving one folder holding two names Windows cannot tell apart.

    An ambiguous fold matches nothing. Only a library that already held
    ``part.3mf`` and ``PART.3MF`` can produce one, and picking either of them
    for a write would be a guess about which file the client meant.
    """
    for child in children:
        if child.name == name:
            return child
    folded = name.casefold()
    matches = [child for child in children if child.name.casefold() == folded]
    return matches[0] if len(matches) == 1 else None


def _is_same_entry(left: _Entry | None, right: _Entry | None) -> bool:
    """Whether two entries are the same row, reached under two spellings."""
    if left is None or right is None:
        return False
    if left.file is not None and right.file is not None:
        return left.file.id == right.file.id
    if left.folder is not None and right.folder is not None:
        return left.folder.id == right.folder.id
    return False


async def _resolve(db: AsyncSession, segments: Sequence[str], principal: _Principal) -> _Entry:
    """Walk the projection from the root to *segments*, or raise 404.

    Level by level rather than by a path lookup, because each level's listing
    is the thing that applies the permission and trash filters — resolving a
    path any other way would let a request reach a row the listing hides. The
    write methods resolve through here too, which is the whole reason a
    ``..`` or an absolute path in a request cannot reach anything: there is no
    child named ``..``, so the walk simply ends in a 404.
    """
    entry = _root_entry()
    for segment in segments:
        if not entry.is_collection:
            raise _not_found()
        match = _match_child(await _children(db, entry, principal), segment)
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


def _response_element(parent: ET.Element, href: str, entry: _Entry, *, writable: bool = False) -> None:
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
        # The guessed type belongs here and nowhere else: a property tells a
        # client which application owns the file, while the type on a GET
        # response tells a browser what to execute — which is why that one is
        # octet-stream regardless of the name.
        ET.SubElement(prop, f"{{{DAV_NS}}}getcontenttype").text = _media_type(entry.file.filename)

    if writable:
        # Only in readwrite mode, and only because a client decides from this
        # (together with ``DAV: 2``) whether the drive can be written to at all.
        # It describes what LOCK answers, which is "yes" — see ``webdav_lock``.
        supportedlock = ET.SubElement(prop, f"{{{DAV_NS}}}supportedlock")
        for scope in ("exclusive", "shared"):
            lockentry = ET.SubElement(supportedlock, f"{{{DAV_NS}}}lockentry")
            ET.SubElement(ET.SubElement(lockentry, f"{{{DAV_NS}}}lockscope"), f"{{{DAV_NS}}}{scope}")
            ET.SubElement(ET.SubElement(lockentry, f"{{{DAV_NS}}}locktype"), f"{{{DAV_NS}}}write")

    ET.SubElement(propstat, f"{{{DAV_NS}}}status").text = "HTTP/1.1 200 OK"


def _multistatus(rows: Sequence[tuple[str, _Entry]], *, writable: bool = False) -> Response:
    multistatus = ET.Element(f"{{{DAV_NS}}}multistatus")
    for href, entry in rows:
        _response_element(multistatus, href, entry, writable=writable)
    # xml_declaration=False explicitly: whether ``encoding="utf-8"`` alone emits
    # a declaration has changed between Python versions, and a body carrying two
    # of them is not XML at all — every client would show an empty drive.
    body = b'<?xml version="1.0" encoding="utf-8"?>\n' + ET.tostring(
        multistatus, encoding="utf-8", xml_declaration=False
    )
    return Response(content=body, status_code=207, media_type='application/xml; charset="utf-8"')


@router.api_route("", methods=["OPTIONS"])
@router.api_route("/{dav_path:path}", methods=["OPTIONS"])
async def webdav_options(
    dav_path: str = "",
    principal: _Principal = Depends(webdav_principal),
) -> Response:
    """Announce the protocol level and the methods this share answers today.

    ``MS-Author-Via`` is what the Windows WebClient looks for before it will
    treat the URL as a DAV share at all; without it a mapped drive fails with
    an unhelpful "folder is invalid".

    ``DAV: 1, 2`` in readwrite mode, ``DAV: 1`` otherwise. Level 2 means
    locking, and a client that does not see it treats the share as one it
    cannot save to no matter what ``Allow`` says.
    """
    return Response(
        status_code=200,
        headers={
            "DAV": "1, 2" if principal.mode == MODE_READWRITE else "1",
            "MS-Author-Via": "DAV",
            "Allow": _allow_header(principal.mode),
            "Content-Length": "0",
        },
    )


@router.api_route("", methods=["PROPFIND"])
@router.api_route("/{dav_path:path}", methods=["PROPFIND"])
async def webdav_propfind(
    request: Request,
    dav_path: str = "",
    db: AsyncSession = Depends(get_db),
    principal: _Principal = Depends(webdav_principal),
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
    depth = (request.headers.get("Depth") or "1").strip().lower()
    if depth not in ("0", "1"):
        body = b'<?xml version="1.0" encoding="utf-8"?>\n<D:error xmlns:D="DAV:"><D:propfind-finite-depth/></D:error>'
        return Response(content=body, status_code=403, media_type='application/xml; charset="utf-8"')

    segments = _split_path(dav_path)
    entry = await _resolve(db, segments, principal)

    root_path = request.scope.get("root_path", "")
    rows: list[tuple[str, _Entry]] = [(_href(root_path, segments, entry.is_collection), entry)]
    if depth == "1" and entry.is_collection:
        for child in await _children(db, entry, principal):
            rows.append((_href(root_path, [*segments, child.name], child.is_collection), child))
    return _multistatus(rows, writable=principal.mode == MODE_READWRITE)


@router.api_route("", methods=["GET", "HEAD"])
@router.api_route("/{dav_path:path}", methods=["GET", "HEAD"])
async def webdav_get(
    dav_path: str = "",
    db: AsyncSession = Depends(get_db),
    principal: _Principal = Depends(webdav_principal),
):
    """Stream a file's bytes.

    ``FileResponse`` carries the single-``Range`` handling (206 plus
    ``Content-Range``) that slicers and Explorer's preview both use, and
    answers a HEAD with the headers alone. A collection has no bytes, so it
    answers 405 rather than inventing a directory listing.

    Served as an attachment of ``application/octet-stream``, the way
    ``download_file`` serves the same rows, and never as the type the name
    suggests. The library takes whatever a user uploads or a scan finds, so a
    ``.html`` or ``.js`` in a shared folder would otherwise render as an active
    document on Bambuddy's own origin, where ``script-src 'self'`` permits it
    and the session token sits in web storage. A WebDAV client does not read
    the type off the wire anyway — it names the file from the path. (The
    ``nosniff`` that keeps a browser from second-guessing the type comes from
    ``security_headers_middleware``, on this response like every other.)
    """
    entry = await _resolve(db, _split_path(dav_path), principal)
    if entry.is_collection or entry.file is None:
        raise _method_not_allowed(principal.mode, "A collection has no bytes")

    abs_path, info = _resolved_bytes(entry.file)
    return FileResponse(
        abs_path,
        media_type="application/octet-stream",
        filename=entry.name,
        content_disposition_type="attachment",
        stat_result=info,
    )


# --------------------------------------------------------------------------
# The write half
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class _Target:
    """A path a write names: where it lands, under what name, onto what.

    ``entry`` is what is already there, or None — the one question every write
    method has to answer before it does anything.
    """

    parent: _Entry
    name: str
    entry: _Entry | None


def _refuse_unless_writable(principal: _Principal) -> None:
    if principal.mode != MODE_READWRITE:
        raise _method_not_allowed(principal.mode, "This WebDAV share is read-only")


def _forbidden(detail: str) -> HTTPException:
    return HTTPException(status_code=403, detail=detail)


def _require_permission(user: User, *permissions: Permission) -> None:
    """The permission gate the matching REST route applies, checked inline.

    ``require_permission_if_auth_enabled`` cannot be used here: it reads a
    Bearer token or an API key, and a WebDAV client has neither. The rule it
    encodes is the same one, minus the "auth disabled" branch — this share
    always has a real user behind it, by design.
    """
    if not user.has_any_permission(*(permission.value for permission in permissions)):
        wanted = " or ".join(permission.value for permission in permissions)
        raise _forbidden(f"Missing permission: {wanted}")


def _require_ownership(user: User, file: LibraryFile, all_permission: Permission, own_permission: Permission) -> None:
    """``require_ownership_permission``'s rule for one row, checked inline.

    Including its fail-closed corner: a row nobody owns needs the ``_all``
    permission, because "their own" cannot be true of a file with no owner.
    """
    if user.has_permission(all_permission.value):
        return
    if user.has_permission(own_permission.value) and file.created_by_id is not None and file.created_by_id == user.id:
        return
    raise _forbidden(f"Missing permission: {all_permission.value}")


def _require_update(user: User, file: LibraryFile) -> None:
    """The permission a write onto an existing row needs.

    Normally the update pair, the same one the REST route asks for. The one
    exception is a row this caller created and never filled: because Windows
    creates the file empty and sends the bytes in a *second* request, the back
    half of a single save always looks like an update of a row that exists only
    because its front half made it. A group holding ``library:upload`` and no
    update permission — "may add files, may not change the ones already there",
    which the REST upload serves in one request — could otherwise never finish
    a save over the share, and would be left with a 0-byte row it has no
    permission to remove either.
    """
    if (
        file.ingest_pending
        and file.file_hash is None
        and file.created_by_id is not None
        and file.created_by_id == user.id
        and user.has_permission(Permission.LIBRARY_UPLOAD.value)
    ):
        return
    _require_ownership(user, file, Permission.LIBRARY_UPDATE_ALL, Permission.LIBRARY_UPDATE_OWN)


def _require_delete(user: User, file: LibraryFile) -> None:
    """The permission for a write that ends a row's existence.

    The same rule ``webdav_delete`` applies, asked wherever a row is destroyed
    rather than only where the method is called DELETE.
    """
    if user.has_permission(Permission.LIBRARY_DELETE_ALL.value):
        return
    _require_permission(user, Permission.LIBRARY_DELETE_OWN)
    if file.created_by_id is None or file.created_by_id != user.id:
        raise _forbidden(f"Missing permission: {Permission.LIBRARY_DELETE_ALL.value}")


def _check_no_traversal(segments: Sequence[str]) -> None:
    """Refuse a path with a ``..`` or a separator anywhere in it.

    The walk in ``_resolve`` would answer 404 for these on its own, since no
    child is ever called ``..`` — but a write deserves the truthful answer
    rather than "no such folder", and 403 is the one the containment rule
    names. Every write resolves through here, so the projection is the only
    thing a write can reach.
    """
    for segment in segments:
        if segment in (".", "..") or "/" in segment or "\\" in segment:
            raise _forbidden("That path is outside the library")


def _check_new_name(name: str) -> None:
    """Refuse a final path segment the projection could not hand back.

    Three ways a name fails, and they answer differently on purpose:

    - a separator or a ``..`` is an attempt to leave the library, and the one
      answer the spec names for that is 403;
    - a name the SD card cannot hold is the 400 the REST upload already gives
      it (#1540), so the same file refused in the browser is refused here;
    - a name that survives both but is not what ``_label`` would project —
      ``.gitignore`` loses its leading dot — would come back from the next
      PROPFIND under a different name, which reads to a client as a write that
      silently went somewhere else. Refused rather than renamed.
    """
    if "/" in name or "\\" in name or name in (".", ".."):
        raise _forbidden("That path is outside the library")
    try:
        validate_print_filename(name)
    except InvalidFilenameError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None
    if _zip_entry_name(name, fallback="") != name:
        raise HTTPException(status_code=400, detail=f"The library cannot store a file named {name!r}")


async def _resolve_target(
    db: AsyncSession,
    segments: Sequence[str],
    principal: _Principal,
    *,
    validate_name: bool = True,
) -> _Target:
    """Resolve *segments* as something a write may name.

    The parent is resolved by the same walk a read uses, so a write can only
    reach a collection a read could have listed. A missing parent is 409 rather
    than 404, which is what RFC 4918 gives a PUT into a folder that is not
    there and what makes a client offer to create it.
    """
    if not segments:
        raise _forbidden("The share root itself cannot be written")
    _check_no_traversal(segments)
    try:
        parent = await _resolve(db, segments[:-1], principal)
    except HTTPException as exc:
        if exc.status_code == 404:
            raise HTTPException(status_code=409, detail="The parent folder does not exist") from None
        raise
    if not parent.is_collection:
        raise HTTPException(status_code=409, detail="The parent of that path is a file")

    name = segments[-1]
    entry = _match_child(await _children(db, parent, principal), name)
    if entry is None and validate_name:
        _check_new_name(name)
    return _Target(parent=parent, name=name, entry=entry)


def _write_folder(parent: _Entry) -> LibraryFolder | None:
    """The folder a write into *parent* belongs to, or None for the managed bucket.

    The root and ``External`` are not directories: the first holds exactly the
    two buckets, and a loose file under the second would have no directory on
    any share to live in. Both refuse rather than inventing a home.
    """
    if parent.folder is not None:
        return parent.folder
    if parent.bucket == BUCKET_MANAGED:
        return None
    raise _forbidden("New entries belong in 'Files' or in one of the external folders")


def _refuse_readonly(folder: LibraryFolder | None) -> None:
    """A read-only external folder refuses every write, as the REST routes do."""
    if folder is not None and folder.is_external and folder.external_readonly:
        raise _forbidden("That external folder is registered read-only")


def _refuse_readonly_row(file: LibraryFile, folder: LibraryFolder | None) -> None:
    """The same rule for a file, including the case with no folder to ask.

    An external row outside any folder has no mount to check the flag on, so it
    is refused: a guess in the permissive direction here writes to somebody's
    share.
    """
    if not file.is_external:
        return
    if folder is None:
        raise _forbidden("That external file is not in a folder the share can write to")
    _refuse_readonly(folder)


def _external_directory(folder: LibraryFolder) -> Path:
    """The real directory behind an external folder, checked before it is used."""
    if not folder.external_path:
        raise HTTPException(status_code=409, detail="That external folder has no configured path")
    directory = Path(folder.external_path)
    if not directory.is_dir():
        raise HTTPException(status_code=409, detail=f"External path is not accessible: {folder.external_path}")
    if not os.access(directory, os.W_OK):
        raise _forbidden(f"External path is not writable: {folder.external_path}")
    return directory


def _discard_thumbnail(stored: str | None) -> None:
    """Delete a thumbnail whose row no longer points at it.

    Replacing a file's content in place leaves the old preview behind, and
    nothing else sweeps it: the trash sweeper only ever sees deleted rows. The
    containment check is not ceremony — ``thumbnail_path`` is a stored string,
    and an external row's could be anything at all.
    """
    if not stored:
        return
    try:
        path = to_absolute_path(stored)
    except ValueError:
        return
    if path is None:
        return
    try:
        if path.is_relative_to(get_library_thumbnails_dir().resolve()):
            path.unlink(missing_ok=True)
    except OSError:
        pass


def _adopt_content(file: LibraryFile, path: Path) -> None:
    """Re-derive everything about *file* that comes from the bytes at *path*.

    The row keeps its identity — id, tags, project link, notes, photos — and
    takes new content. That is the whole of the overwrite rule, and the reason
    a mapped drive saving over a file does not produce a second row.
    """
    previous_thumbnail = file.thumbnail_path
    derived = ingest_library_file_content(path, file.filename)
    file.file_type = derived.file_type
    file.file_hash = derived.file_hash
    file.thumbnail_path = derived.thumbnail_path
    file.file_metadata = derived.metadata
    file.ingest_pending = False
    try:
        info = path.stat()
        file.file_size = info.st_size
        file.fs_modified_at = _mtime_to_datetime(info.st_mtime)
    except OSError:  # pragma: no cover - the bytes were written a line ago
        pass
    if previous_thumbnail and previous_thumbnail != derived.thumbnail_path:
        _discard_thumbnail(previous_thumbnail)


def _refuse_oversize(request: Request) -> None:
    """Refuse a declared body over the cap before a byte of it is read.

    A chunked request declares nothing, which is why ``_stream_to_temp``
    counts as well; this is only the early out.
    """
    declared = request.headers.get("Content-Length")
    if declared is None:
        return
    try:
        length = int(declared)
    except ValueError:
        return
    if length > MAX_PUT_BYTES:
        raise _too_large()


def _too_large() -> HTTPException:
    return HTTPException(
        status_code=413,
        detail=f"A single file may not exceed {MAX_PUT_BYTES // (1024 * 1024)} MB over WebDAV",
    )


async def _discard_body(request: Request) -> None:
    """Read and drop a body. A client that is not read out stalls or errors."""
    async for _chunk in request.stream():
        pass


def _write_failed(exc: OSError) -> HTTPException:
    """The answer for a filesystem that refused a write once it had started.

    A 500 is the wrong one for every case here: the caller can act on "the
    share went read-only" and on "the disk is full", and neither is a bug in
    the server.
    """
    if isinstance(exc, PermissionError):
        return _forbidden("The folder this file belongs to is not writable")
    if exc.errno in (errno.ENOSPC, errno.EDQUOT):
        return HTTPException(status_code=507, detail="The storage behind this share is full")
    return HTTPException(status_code=409, detail=f"The file could not be written: {exc.strerror or exc}")


def _require_writable_dir(directory: Path) -> None:
    """Ask the filesystem, not only the database, whether a write can land here.

    ``external_readonly`` is a flag somebody set once; the mount it describes
    can be remounted read-only or lose its credentials afterwards, which is how
    a share in a farm usually becomes read-only. The create path asks through
    ``_resolve_upload_destination``, so an overwrite has to ask too — otherwise
    saving over a file answers 500 from the ``open()`` a moment later while
    creating one in the same folder answers a clean 400.
    """
    if not directory.is_dir():
        raise HTTPException(status_code=409, detail="The folder this file belongs to is not accessible")
    if not os.access(directory, os.W_OK):
        raise _forbidden("The folder this file belongs to is not writable")


async def _stream_to_temp(request: Request, directory: Path) -> tuple[Path, int]:
    """Stream the body into a scratch file beside its destination.

    Beside it, not in the system temp, for two reasons: the move into place is
    then a rename on one filesystem rather than a copy, and a 1 GiB body never
    sits in the memory of a Pi that is also carrying printer MQTT traffic. The
    scratch file is removed on every failure path, including a client that
    disconnects mid-body.

    The directory has to exist already — creating it would mean building a tree
    on the local disk where an unmounted share used to be, and a write that
    lands under a dead mount point is worse than one that fails.
    """
    _require_writable_dir(directory)
    temp = directory / f".bambuddy-dav-{uuid.uuid4().hex}.part"
    written = 0
    try:
        try:
            with open(temp, "wb") as handle:
                async for chunk in request.stream():
                    written += len(chunk)
                    if written > MAX_PUT_BYTES:
                        raise _too_large()
                    handle.write(chunk)
        except OSError as exc:
            raise _write_failed(exc) from None
    except BaseException:
        with contextlib.suppress(OSError):
            temp.unlink(missing_ok=True)
        raise
    return temp, written


def _move_into_place(temp: Path, destination: Path) -> None:
    """Put a finished scratch file where it belongs, replacing what is there.

    ``os.replace`` is atomic and overwrites on both platforms, so a reader
    never sees a half-written file. The fallback is for the one case it cannot
    serve — a destination on another filesystem, which is every managed-to-
    external move, since the blob lives under ``DATA_DIR`` and the share does
    not — and it has to keep the same promise. So it copies to a scratch file
    beside the destination and renames that into place: copying straight onto
    the destination truncates it at ``open()``, and a mount that drops halfway
    through would leave somebody's file cut in half with nothing to restore it
    from and a row still describing the bytes that used to be there.
    """
    try:
        os.replace(temp, destination)
        return
    except OSError:
        pass
    scratch = destination.parent / f".bambuddy-dav-{uuid.uuid4().hex}.part"
    try:
        shutil.copy2(temp, scratch)
        os.replace(scratch, destination)
    except OSError as exc:
        with contextlib.suppress(OSError):
            scratch.unlink(missing_ok=True)
        raise _write_failed(exc) from None
    with contextlib.suppress(OSError):
        temp.unlink(missing_ok=True)


def _copy_into_place(source: Path, destination: Path) -> None:
    """Copy *source* to *destination* without ever truncating what is there.

    A COPY onto an existing name replaces a file that stays live until the copy
    finishes, so the bytes land beside it and are renamed over it — the same
    promise ``_move_into_place`` makes, for the same reason.
    """
    _require_writable_dir(destination.parent)
    scratch = destination.parent / f".bambuddy-dav-{uuid.uuid4().hex}.part"
    try:
        shutil.copy2(source, scratch)
        _move_into_place(scratch, destination)
    except OSError as exc:
        with contextlib.suppress(OSError):
            scratch.unlink(missing_ok=True)
        raise _write_failed(exc) from None
    except HTTPException:
        with contextlib.suppress(OSError):
            scratch.unlink(missing_ok=True)
        raise


def _replacement_destination(file: LibraryFile) -> Path:
    """Where to write when an existing row's content is being replaced.

    Its own path, so an external file is rewritten on the share under its real
    name and a managed blob keeps the UUID every other row already points at. A
    managed row whose stored path cannot be resolved — a legacy absolute path,
    or one that escapes ``base_dir`` — is given a fresh blob instead of being
    trusted to name a write target.
    """
    try:
        current = _resolve_source_disk_path(file)
    except ValueError:
        current = None
    if current is not None:
        if file.is_external:
            return current
        if current.parent.is_dir():
            return current
    suffix = os.path.splitext(file.filename)[1].lower()
    return get_library_files_dir() / f"{uuid.uuid4().hex}{suffix}"


def _managed_blob(file: LibraryFile) -> Path | None:
    """The row's own file under the managed store, or None for anything else.

    Used to decide whether a blob may be removed: an external row's bytes
    belong to somebody's share and are never Bambuddy's to unlink.
    """
    if file.is_external:
        return None
    try:
        path = to_absolute_path(file.file_path)
    except ValueError:
        return None
    if path is None:
        return None
    try:
        return path if path.is_relative_to(get_library_files_dir().resolve()) else None
    except OSError:  # pragma: no cover - resolve() on a broken mount
        return None


def _blob_destination(name: str) -> Path:
    """A fresh managed blob whose extension matches the name the library shows.

    The extension matters even though nothing reads the blob by name: the STL
    thumbnailer loads by suffix, so a ``.stl`` parked at ``<uuid>.tmp`` would
    silently never get a preview.
    """
    return get_library_files_dir() / f"{uuid.uuid4().hex}{os.path.splitext(name)[1].lower()}"


async def _is_unclaimed_name(db: AsyncSession, folder: LibraryFolder | None, name: str) -> bool:
    """Whether a name in an external folder belongs to no library row at all.

    Deleting an external file removes its row and leaves the bytes on the share
    — the File Manager's own rule, and the reason the next scan finds the file
    again. In between, the name is absent from every listing while the
    directory still holds it, so re-saving the name you just deleted was told
    that a file exists which nothing shows. A PUT says "these bytes, at this
    path", so the file on the share is replaced and the row comes back.

    Deliberately not filtered by what the caller may see, and deliberately
    counting trashed rows too: a file somebody else owns is missing from *this*
    listing, and overwriting it because it is invisible from here is the exact
    mistake this question exists to prevent.
    """
    if folder is None or not folder.is_external:
        return False
    claimed = await db.scalar(
        select(func.count(LibraryFile.id)).where(
            LibraryFile.folder_id == folder.id,
            func.lower(LibraryFile.filename) == name.lower(),
        )
    )
    return not claimed


async def _forget_row(db: AsyncSession, file: LibraryFile) -> None:
    """Drop a row whose bytes have moved to another row, without touching disk.

    Not the trash, and it cannot be: the bytes this row used to name belong to
    the row that replaced it now, and for a managed rename they are the very
    same blob. A trashed row pointing at a live file is a file the sweeper
    deletes out from under the row that is still using it — worse than the
    thing the trash is for. A client that renamed ``foo.tmp`` onto ``foo.3mf``
    has not asked for a ``foo.tmp`` in the trash either.

    So this destroys a row, and the caller has to have established that the
    user may destroy one (``_require_delete``). The same dependent cleanup the
    delete routes do, because a queue entry pointing at a row that no longer
    exists is a 500 waiting to happen.
    """
    from backend.app.services.library_trash import delete_dependent_variants, release_queue_references
    from backend.app.utils.library_paths import remove_library_photos_dir

    await delete_dependent_variants(db, [file.id])
    await release_queue_references(db, [file.id])
    remove_library_photos_dir(file.id)
    _discard_thumbnail(file.thumbnail_path)
    await db.delete(file)


@router.api_route("", methods=["PUT"])
@router.api_route("/{dav_path:path}", methods=["PUT"])
async def webdav_put(
    request: Request,
    dav_path: str = "",
    db: AsyncSession = Depends(get_db),
    principal: _Principal = Depends(webdav_principal),
) -> Response:
    """Write a file, creating the row or replacing the one already there.

    Two client habits decide the shape of this:

    **The empty-file-then-content dance.** Almost every application creates the
    file, locks it, writes the bytes and unlocks — so the first PUT carries no
    body. Hashing that would produce a 0-byte row, a hash shared by every empty
    file in the library and a failed 3MF parse, so an empty body creates the row
    and defers all of it; the row is marked pending and the next PUT fills it
    in. An empty body onto a row that already has content changes *nothing*: a
    truncate-to-zero and the first half of that dance are indistinguishable on
    the wire, and only one of the two readings can destroy a file.

    **Overwrite means replace.** A PUT onto an existing name keeps that row —
    its id, its tags, its project link, its notes and its photos — and swaps the
    content underneath. It does not make a version, and it does not make a
    variant. A Bambuddy user might expect either; a drive does neither.
    """
    _refuse_unless_writable(principal)
    segments = _split_path(dav_path)
    try:
        if not segments:
            raise _forbidden("The share root is not a file")
        _check_no_traversal(segments)
    except HTTPException:
        # The body has to come off the wire even for a refusal, or the client
        # sees a broken connection instead of the answer.
        await _discard_body(request)
        raise

    if _is_junk_name(segments[-1]):
        # Answered before the path is resolved, because Explorer drops these
        # into the share root too, where nothing else may be written.
        await _discard_body(request)
        return Response(status_code=201)

    _refuse_oversize(request)
    target = await _resolve_target(db, segments, principal)
    if target.entry is not None and target.entry.is_collection:
        raise _method_not_allowed(principal.mode, "A folder cannot be replaced by a file")

    existing = target.entry.file if target.entry is not None else None
    if existing is None:
        folder = _write_folder(target.parent)
        _require_permission(principal.user, Permission.LIBRARY_UPLOAD)
        _refuse_readonly(folder)
        # The upload route's own resolver: it picks the managed blob or the
        # real path on the mount, and refuses a read-only or unreachable one.
        destination, is_external = _resolve_upload_destination(
            folder, target.name, allow_existing=await _is_unclaimed_name(db, folder, target.name)
        )
    else:
        folder = target.parent.folder
        _require_update(principal.user, existing)
        _refuse_readonly_row(existing, folder)
        destination = _replacement_destination(existing)
        is_external = existing.is_external

    temp, written = await _stream_to_temp(request, destination.parent)

    if written == 0 and existing is not None:
        with contextlib.suppress(OSError):
            temp.unlink(missing_ok=True)
        return Response(status_code=204)

    try:
        _move_into_place(temp, destination)
    except HTTPException:
        with contextlib.suppress(OSError):
            temp.unlink(missing_ok=True)
        raise

    if existing is not None:
        existing.file_path = _stored_file_path(destination, is_external)
        _adopt_content(existing, destination)
        await db.commit()
        return Response(status_code=204)

    row = LibraryFile(
        folder_id=folder.id if folder is not None else None,
        is_external=is_external,
        filename=target.name,
        file_path=_stored_file_path(destination, is_external),
        # Classified from the name alone while pending: there is nothing inside
        # an empty file for ``classify_file_type`` to look at.
        file_type=classify_file_type(target.name),
        file_size=0,
        ingest_pending=True,
        created_by_id=principal.user.id,
    )
    if written:
        _adopt_content(row, destination)
    db.add(row)
    await db.commit()
    return Response(status_code=201)


@router.api_route("", methods=["DELETE"])
@router.api_route("/{dav_path:path}", methods=["DELETE"])
async def webdav_delete(
    dav_path: str = "",
    db: AsyncSession = Depends(get_db),
    principal: _Principal = Depends(webdav_principal),
) -> Response:
    """Move a file to the trash, or remove an empty folder.

    A file goes exactly where the File Manager's delete sends it — ``deleted_at``
    and the retention window, not the bytes. A drive that empties the recycle
    bin for you is not what anyone expects, and it is not recoverable.

    A folder is refused unless it is empty. The UI's folder delete is a cascade
    that hard-deletes every file under it, bytes and all; behind a mapped drive
    that is one stray keystroke away from a print farm's whole library, with no
    trash to undo it from. An empty folder holds nobody's work, so that case is
    allowed and everything else points at the File Manager.
    """
    _refuse_unless_writable(principal)
    segments = _split_path(dav_path)
    if not segments:
        raise _forbidden("The share root cannot be deleted")
    _check_no_traversal(segments)
    if _is_junk_name(segments[-1]):
        # It was never stored, so it is already gone.
        return Response(status_code=204)

    target = await _resolve_target(db, segments, principal, validate_name=False)
    if target.entry is None:
        raise _not_found()

    if target.entry.is_collection:
        await _delete_collection(db, target, principal)
        return Response(status_code=204)

    file = target.entry.file
    _refuse_readonly_row(file, target.parent.folder)
    can_delete_all = principal.user.has_permission(Permission.LIBRARY_DELETE_ALL.value)
    if not can_delete_all:
        _require_permission(principal.user, Permission.LIBRARY_DELETE_OWN)
    # The File Manager's own delete, called rather than copied: it is the one
    # place that knows an external row leaves the trash out of it, and which
    # dependants have to be released first.
    await delete_file(file_id=file.id, db=db, auth_result=(principal.user, can_delete_all))
    return Response(status_code=204)


async def _delete_collection(db: AsyncSession, target: _Target, principal: _Principal) -> None:
    folder = target.entry.folder if target.entry is not None else None
    if folder is None:
        raise _forbidden("'Files' and 'External' are part of the share, not folders in the library")
    if folder.is_external:
        raise _forbidden("An external folder is unregistered in the File Manager, not deleted from a drive")

    can_delete_all = principal.user.has_permission(Permission.LIBRARY_DELETE_ALL.value)
    if not can_delete_all:
        _require_permission(principal.user, Permission.LIBRARY_DELETE_OWN)
        blocker = await _restricted_folder_delete_blocker(db, folder)
        if blocker:
            raise _forbidden(blocker)

    children = await db.scalar(select(func.count(LibraryFolder.id)).where(LibraryFolder.parent_id == folder.id))
    # Trashed files count: ``folder_id`` cascades, so deleting the folder would
    # hard-delete rows somebody still has a restore button for.
    files = await db.scalar(select(func.count(LibraryFile.id)).where(LibraryFile.folder_id == folder.id))
    if (children or 0) or (files or 0):
        raise _forbidden("Only an empty folder can be deleted over WebDAV — use the File Manager for the rest")

    await db.delete(folder)
    await db.commit()


@router.api_route("", methods=["MKCOL"])
@router.api_route("/{dav_path:path}", methods=["MKCOL"])
async def webdav_mkcol(
    request: Request,
    dav_path: str = "",
    db: AsyncSession = Depends(get_db),
    principal: _Principal = Depends(webdav_principal),
) -> Response:
    """Create a folder, with no number even when the series is on.

    There is no dialog here to ask, and consuming a number from the
    ``library_folder`` series for a folder the user did not ask to number is
    worse than leaving the field empty — the number is what an order is filed
    under, and a gap in the series is a question nobody can answer later.
    """
    _refuse_unless_writable(principal)
    # Permission before the body check, so a caller with no rights here learns
    # nothing about the request shapes the method does and does not accept.
    _require_permission(principal.user, Permission.LIBRARY_UPLOAD)
    if await request.body():
        # RFC 4918 9.3.1: a body whose semantics the server does not understand.
        raise HTTPException(status_code=415, detail="MKCOL with a body is not supported")

    target = await _resolve_target(db, _split_path(dav_path), principal)
    if target.entry is not None:
        raise _method_not_allowed(principal.mode, "That name already exists")

    parent = _write_folder(target.parent)
    _refuse_readonly(parent)

    folder = LibraryFolder(name=target.name, parent_id=parent.id if parent is not None else None)
    if parent is not None and parent.is_external:
        # The row follows the directory here, the same shape a scan produces,
        # so the next scan recognises it instead of creating a second row.
        directory = safe_join_under(_external_directory(parent), target.name)
        try:
            directory.mkdir()
        except FileExistsError:
            raise _method_not_allowed(principal.mode, "That directory already exists on the share") from None
        except OSError as exc:
            raise HTTPException(status_code=409, detail=f"Could not create the directory: {exc}") from None
        folder.is_external = True
        folder.external_path = str(directory)
        folder.external_readonly = parent.external_readonly
        folder.external_show_hidden = parent.external_show_hidden

    db.add(folder)
    await db.commit()
    return Response(status_code=201)


def _destination_segments(request: Request) -> list[str]:
    """The ``Destination`` header, as path segments inside this share.

    Decoded one segment at a time, after the split: decoding the whole path
    first would let a ``%2F`` in a name turn into a separator and invent a
    level the client never named. Anything that does not land under ``/webdav``
    is 403 — the spec's answer for a target outside the library — rather than
    the 502 the RFC reserves for a destination on another server, because from
    here those are the same mistake.
    """
    raw = request.headers.get("Destination")
    if not raw:
        raise HTTPException(status_code=400, detail="MOVE and COPY need a Destination header")
    path = urlsplit(raw).path
    prefix = f"{request.scope.get('root_path', '')}{WEBDAV_PREFIX}"
    if path != prefix and not path.startswith(f"{prefix}/"):
        raise _forbidden("The destination is outside the library")
    segments = [unquote(segment) for segment in path[len(prefix) :].split("/") if segment]
    _check_no_traversal(segments)
    return segments


def _overwrite_allowed(request: Request) -> bool:
    """``Overwrite`` defaults to T, per RFC 4918 10.6."""
    return (request.headers.get("Overwrite") or "T").strip().upper() != "F"


async def _resolve_transfer(
    request: Request,
    db: AsyncSession,
    dav_path: str,
    principal: _Principal,
    *,
    fold_is_rename: bool,
) -> tuple[_Target, _Target]:
    """The (source, destination) pair a MOVE or COPY names, both validated.

    ``fold_is_rename`` says what a destination that folds onto the source
    itself means — the two spellings of one case-insensitive name. For a MOVE
    it is a rename in place, of a file or of a folder. For a COPY it is the
    same path twice, and answering it would put two names in one folder that
    the client which sent them cannot tell apart.
    """
    source = await _resolve_target(db, _split_path(dav_path), principal, validate_name=False)
    if source.entry is None:
        raise _not_found()

    destination_segments = _destination_segments(request)
    if destination_segments == _split_path(dav_path):
        raise _forbidden("The source and the destination are the same path")
    destination = await _resolve_target(db, destination_segments, principal)
    if _is_junk_name(destination.name):
        # The name is one the share drops on the floor, so landing a real file
        # on it would delete the file while answering success.
        raise _forbidden(f"{destination.name!r} is a name this share does not store")

    if _is_same_entry(source.entry, destination.entry):
        if not fold_is_rename:
            raise _forbidden("The source and the destination are the same path")
        # Neither a replace — which would hand the row its own bytes and then
        # delete it — nor "that name is taken", which is what the collection
        # check below would have called it.
        _check_new_name(destination.name)
        destination = _Target(parent=destination.parent, name=destination.name, entry=None)

    if destination.entry is not None:
        if not _overwrite_allowed(request):
            raise HTTPException(status_code=412, detail="The destination exists and Overwrite is F")
        if destination.entry.is_collection or source.entry.is_collection:
            # Replacing a collection means deleting one first, recursively. A
            # drive does not get to do that; the File Manager does.
            raise _forbidden("That name is taken by a folder")
    return source, destination


@router.api_route("", methods=["MOVE"])
@router.api_route("/{dav_path:path}", methods=["MOVE"])
async def webdav_move(
    request: Request,
    dav_path: str = "",
    db: AsyncSession = Depends(get_db),
    principal: _Principal = Depends(webdav_principal),
) -> Response:
    """Rename, move between folders, or land a temporary name on its real one.

    The third is why this has to do more than change a column. Many
    applications write ``foo.tmp`` and rename it onto ``foo.3mf``, so the
    classification, the 3MF parse and the thumbnail have to run on the *final*
    name — a move that changes the type, or that lands on a row still waiting
    for its bytes, re-derives all of it. A move onto an existing file follows
    the same rule a PUT does: the destination row keeps its identity and takes
    the content, and the source row goes away.
    """
    _refuse_unless_writable(principal)
    source, destination = await _resolve_transfer(request, db, dav_path, principal, fold_is_rename=True)
    created = destination.entry is None

    if source.entry.is_collection:
        await _move_collection(db, source, destination, principal)
        return Response(status_code=201 if created else 204)

    file = source.entry.file
    _require_ownership(principal.user, file, Permission.LIBRARY_UPDATE_ALL, Permission.LIBRARY_UPDATE_OWN)
    _refuse_readonly_row(file, source.parent.folder)

    target_folder = _write_folder(destination.parent)
    _refuse_readonly(target_folder)
    replaced = destination.entry.file if destination.entry is not None else None
    if replaced is not None:
        # The destination row keeps its identity and takes the content, so it
        # is the *source* row that stops existing. Ending a row is the delete
        # permission's business whatever the method is called: without this a
        # role that may edit but not delete could destroy one by renaming it
        # onto a name that is already taken.
        _require_delete(principal.user, file)
        _require_ownership(principal.user, replaced, Permission.LIBRARY_UPDATE_ALL, Permission.LIBRARY_UPDATE_OWN)
        _refuse_readonly_row(replaced, destination.parent.folder)

    try:
        source_path = _resolve_source_disk_path(file)
    except ValueError:
        source_path = None
    if source_path is None or not source_path.is_file():
        raise HTTPException(status_code=409, detail="The file's bytes are not on disk")

    target_is_external = target_folder is not None and target_folder.is_external
    if target_is_external:
        # The row that ends up here is the one that names the file on the
        # share, and a replaced row keeps its own filename — writing the
        # destination's spelling instead would leave the share holding a name
        # no row claims, for the next scan to adopt as a second file.
        new_path = safe_join_under(
            _external_directory(target_folder), replaced.filename if replaced is not None else destination.name
        )
        if new_path.exists() and new_path != source_path and replaced is None:
            # Something is on the share that the library does not know about.
            # Overwriting it would destroy a file nobody asked about.
            raise HTTPException(status_code=409, detail="A file of that name already exists on the share")
    else:
        new_path = source_path if not file.is_external and _same_type(file.filename, destination.name) else None
        new_path = new_path or _blob_destination(destination.name)

    # The blob the destination row is about to stop pointing at. Captured
    # before the move, because after it the row's own path is the new one and
    # the old managed blob would be left behind with nothing referencing it.
    displaced_blob = _managed_blob(replaced) if replaced is not None else None

    if new_path != source_path:
        _move_into_place(source_path, new_path)

    if replaced is not None:
        replaced.folder_id = target_folder.id if target_folder is not None else None
        replaced.is_external = target_is_external
        replaced.file_path = _stored_file_path(new_path, target_is_external)
        _adopt_content(replaced, new_path)
        await _forget_row(db, file)
        if displaced_blob is not None and displaced_blob != new_path:
            with contextlib.suppress(OSError):
                displaced_blob.unlink(missing_ok=True)
    else:
        renamed = file.filename != destination.name
        file.filename = destination.name
        file.folder_id = target_folder.id if target_folder is not None else None
        file.is_external = target_is_external
        file.file_path = _stored_file_path(new_path, target_is_external)
        # The third case is a scanned external file pulled into managed
        # storage under the same name: the scan stores no hash, and without
        # one the file it has just become can never match a duplicate. The
        # REST bulk move computes it at the same boundary and for the same
        # reason.
        if file.ingest_pending or renamed or (file.file_hash is None and not target_is_external):
            _adopt_content(file, new_path)

    await db.commit()
    return Response(status_code=201 if created else 204)


def _same_type(old_name: str, new_name: str) -> bool:
    """Whether a rename leaves the library's idea of the file type alone.

    Compound-aware, so ``a.gcode.3mf`` → ``b.gcode.3mf`` counts as unchanged
    and ``a.tmp`` → ``b.3mf`` does not. A rename that keeps the type keeps the
    blob too; a rename that changes it earns a fresh blob with the right
    suffix and a fresh parse.
    """
    return classify_file_type(old_name) == classify_file_type(new_name)


async def _move_collection(
    db: AsyncSession,
    source: _Target,
    destination: _Target,
    principal: _Principal,
) -> None:
    folder = source.entry.folder if source.entry is not None else None
    if folder is None:
        raise _forbidden("'Files' and 'External' are part of the share and cannot be moved")
    if folder.is_external:
        raise _forbidden("An external folder mirrors a real directory — rename it on the share instead")
    # Folders carry no ownership, so the File Manager asks for update_all here
    # and so does this.
    _require_permission(principal.user, Permission.LIBRARY_UPDATE_ALL)

    parent = _write_folder(destination.parent)
    if parent is not None and parent.is_external:
        raise _forbidden("A managed folder cannot be moved into an external one")

    ancestor = parent
    while ancestor is not None:
        if ancestor.id == folder.id:
            raise HTTPException(status_code=409, detail="A folder cannot be moved into its own subtree")
        ancestor = (
            await db.execute(select(LibraryFolder).where(LibraryFolder.id == ancestor.parent_id))
        ).scalar_one_or_none()

    folder.name = destination.name
    folder.parent_id = parent.id if parent is not None else None
    await db.commit()


@router.api_route("", methods=["COPY"])
@router.api_route("/{dav_path:path}", methods=["COPY"])
async def webdav_copy(
    request: Request,
    dav_path: str = "",
    db: AsyncSession = Depends(get_db),
    principal: _Principal = Depends(webdav_principal),
) -> Response:
    """Copy a file, through the ingest, so the copy is a real library file.

    Hashed, classified, parsed and thumbnailed in its own right rather than
    sharing the original's derived columns — a copy that pointed at the same
    blob would vanish when the original was trashed and swept.

    A collection is refused: a recursive copy is a whole tree of ingests inside
    one request, and every client that copies a folder does it the other way
    round anyway, with a MKCOL and one PUT per file.
    """
    _refuse_unless_writable(principal)
    source, destination = await _resolve_transfer(request, db, dav_path, principal, fold_is_rename=False)
    if source.entry.is_collection:
        raise _forbidden("Copy the files; a folder is copied by creating it and copying into it")

    file = source.entry.file
    target_folder = _write_folder(destination.parent)
    _refuse_readonly(target_folder)
    replaced = destination.entry.file if destination.entry is not None else None
    if replaced is not None:
        _require_ownership(principal.user, replaced, Permission.LIBRARY_UPDATE_ALL, Permission.LIBRARY_UPDATE_OWN)
        _refuse_readonly_row(replaced, destination.parent.folder)
    else:
        _require_permission(principal.user, Permission.LIBRARY_UPLOAD)

    try:
        source_path = _resolve_source_disk_path(file)
    except ValueError:
        source_path = None
    if source_path is None or not source_path.is_file():
        raise HTTPException(status_code=409, detail="The file's bytes are not on disk")

    if replaced is not None:
        new_path = _replacement_destination(replaced)
        is_external = replaced.is_external
    else:
        new_path, is_external = _resolve_upload_destination(target_folder, destination.name)

    _copy_into_place(source_path, new_path)

    if replaced is not None:
        replaced.file_path = _stored_file_path(new_path, is_external)
        _adopt_content(replaced, new_path)
        await db.commit()
        return Response(status_code=204)

    row = LibraryFile(
        folder_id=target_folder.id if target_folder is not None else None,
        is_external=is_external,
        filename=destination.name,
        file_path=_stored_file_path(new_path, is_external),
        file_type=classify_file_type(destination.name),
        file_size=0,
        created_by_id=principal.user.id,
    )
    _adopt_content(row, new_path)
    db.add(row)
    await db.commit()
    return Response(status_code=201)


def _lock_body(href: str, token: str) -> bytes:
    prop = ET.Element(f"{{{DAV_NS}}}prop")
    activelock = ET.SubElement(ET.SubElement(prop, f"{{{DAV_NS}}}lockdiscovery"), f"{{{DAV_NS}}}activelock")
    ET.SubElement(ET.SubElement(activelock, f"{{{DAV_NS}}}locktype"), f"{{{DAV_NS}}}write")
    ET.SubElement(ET.SubElement(activelock, f"{{{DAV_NS}}}lockscope"), f"{{{DAV_NS}}}exclusive")
    ET.SubElement(activelock, f"{{{DAV_NS}}}depth").text = "0"
    ET.SubElement(activelock, f"{{{DAV_NS}}}timeout").text = f"Second-{LOCK_TIMEOUT_SECONDS}"
    ET.SubElement(ET.SubElement(activelock, f"{{{DAV_NS}}}locktoken"), f"{{{DAV_NS}}}href").text = token
    ET.SubElement(ET.SubElement(activelock, f"{{{DAV_NS}}}lockroot"), f"{{{DAV_NS}}}href").text = href
    return b'<?xml version="1.0" encoding="utf-8"?>\n' + ET.tostring(prop, encoding="utf-8", xml_declaration=False)


@router.api_route("", methods=["LOCK"])
@router.api_route("/{dav_path:path}", methods=["LOCK"])
async def webdav_lock(
    request: Request,
    dav_path: str = "",
    db: AsyncSession = Depends(get_db),
    principal: _Principal = Depends(webdav_principal),
) -> Response:
    """Grant every lock that is asked for, and then forget it.

    This is deliberate and should stay that way. Windows takes a lock before it
    writes and reads a refusal as "the share is read-only", so the method
    cannot simply be absent — but a real lock manager would be arbitrating
    between the one person who has the drive mapped and themselves. There is no
    second writer to protect anyone from, and the state it would need (tokens,
    timeouts, a sweeper for the ones a crashed client never released) is a
    standing source of "the file is locked and I cannot save" with nothing on
    the other side of the trade. ``If:`` headers are accepted without being
    checked for the same reason: the token came from here, and here is the only
    place that could have invalidated it.

    Please do not "fix" this into a lock manager without a case for one.
    """
    _refuse_unless_writable(principal)
    _require_permission(
        principal.user,
        Permission.LIBRARY_UPLOAD,
        Permission.LIBRARY_UPDATE_ALL,
        Permission.LIBRARY_UPDATE_OWN,
    )
    segments = _split_path(dav_path)
    collection = False
    exists = True
    try:
        entry = await _resolve(db, segments, principal)
        collection = entry.is_collection
    except HTTPException as exc:
        if exc.status_code != 404:
            raise
        # A lock on a path that does not exist yet: the client is about to
        # create it. RFC 4918 9.10.4 calls that a lock-null resource and answers
        # 201 — without making the resource, which is the part that matters.
        exists = False

    token = f"opaquelocktoken:{uuid.uuid4()}"
    href = _href(request.scope.get("root_path", ""), segments, collection)
    return Response(
        content=_lock_body(href, token),
        status_code=200 if exists else 201,
        media_type='application/xml; charset="utf-8"',
        headers={"Lock-Token": f"<{token}>"},
    )


@router.api_route("", methods=["UNLOCK"])
@router.api_route("/{dav_path:path}", methods=["UNLOCK"])
async def webdav_unlock(
    dav_path: str = "",
    principal: _Principal = Depends(webdav_principal),
) -> Response:
    """Accept any token back. Nothing was recorded, so nothing has to match."""
    _refuse_unless_writable(principal)
    _require_permission(
        principal.user,
        Permission.LIBRARY_UPLOAD,
        Permission.LIBRARY_UPDATE_ALL,
        Permission.LIBRARY_UPDATE_OWN,
    )
    return Response(status_code=204)


# What PROPPATCH accepts, by local name and whatever namespace the client put
# them in. These are the four Explorer sets straight after a PUT, and all four
# are advisory: the library's own timestamps are the row's, and its idea of a
# hidden or read-only file is the permission the caller has, not a bit a client
# sent. Accepted and discarded, therefore — the alternative, refusing them, is
# a save that Explorer reports as failed although every byte arrived.
_ACCEPTED_PROPPATCH_NAMES = frozenset(
    {
        "Win32CreationTime",
        "Win32LastAccessTime",
        "Win32LastModifiedTime",
        "Win32FileAttributes",
    }
)


@router.api_route("", methods=["PROPPATCH"])
@router.api_route("/{dav_path:path}", methods=["PROPPATCH"])
async def webdav_proppatch(
    request: Request,
    dav_path: str = "",
    db: AsyncSession = Depends(get_db),
    principal: _Principal = Depends(webdav_principal),
) -> Response:
    """Answer per property, which is what decides whether a save "worked".

    Explorer sends this immediately after the PUT to stamp the file's
    timestamps, and reads a 405 as the save having failed — bytes safely on
    disk, dialog saying otherwise. So every property gets an answer of its own:
    200 for the Win32 set, which is accepted and then ignored, and 403 for
    anything else, which is honest about a projection that has no property
    storage behind it.
    """
    _refuse_unless_writable(principal)
    _require_permission(
        principal.user,
        Permission.LIBRARY_UPLOAD,
        Permission.LIBRARY_UPDATE_ALL,
        Permission.LIBRARY_UPDATE_OWN,
    )
    segments = _split_path(dav_path)
    entry = await _resolve(db, segments, principal)

    try:
        body = ET.fromstring(await request.body())
    except ET.ParseError:
        raise HTTPException(status_code=400, detail="PROPPATCH body is not XML") from None

    accepted: list[str] = []
    refused: list[str] = []
    for instruction in (f"{{{DAV_NS}}}set", f"{{{DAV_NS}}}remove"):
        for prop in body.findall(f"{instruction}/{{{DAV_NS}}}prop"):
            for element in prop:
                local = element.tag.rpartition("}")[2]
                (accepted if local in _ACCEPTED_PROPPATCH_NAMES else refused).append(element.tag)

    multistatus = ET.Element(f"{{{DAV_NS}}}multistatus")
    response = ET.SubElement(multistatus, f"{{{DAV_NS}}}response")
    href = _href(request.scope.get("root_path", ""), segments, entry.is_collection)
    ET.SubElement(response, f"{{{DAV_NS}}}href").text = href
    for names, status in ((accepted, "HTTP/1.1 200 OK"), (refused, "HTTP/1.1 403 Forbidden")):
        if not names:
            continue
        propstat = ET.SubElement(response, f"{{{DAV_NS}}}propstat")
        prop_element = ET.SubElement(propstat, f"{{{DAV_NS}}}prop")
        for name in names:
            ET.SubElement(prop_element, name)
        ET.SubElement(propstat, f"{{{DAV_NS}}}status").text = status

    payload = b'<?xml version="1.0" encoding="utf-8"?>\n' + ET.tostring(
        multistatus, encoding="utf-8", xml_declaration=False
    )
    return Response(content=payload, status_code=207, media_type='application/xml; charset="utf-8"')


class _AnyMethodRoute(APIRoute):
    """A route that matches every method, not only the ones it declares.

    Starlette answers a method no route declares with a 405 of its own, built
    from the first route whose *path* matched — here the OPTIONS registration,
    so ``POST /webdav`` came back ``Allow: OPTIONS``, and ``webdav_principal``
    never ran. That second part is the one that matters: with the feature
    switched off every other method answers 404, and a single POST told a
    scanner the difference between an install that has WebDAV turned off and
    one that does not have it at all.
    """

    def matches(self, scope: Scope) -> tuple[Match, Scope]:
        # PARTIAL from a Route means the path matched and the method did not.
        match, child_scope = super().matches(scope)
        return (Match.FULL, child_scope) if match is Match.PARTIAL else (match, child_scope)

    async def handle(self, scope: Scope, receive: Receive, send: Send) -> None:
        # Route.handle checks the declared methods a second time and raises its
        # own 405 — the one with the wrong Allow header. Both overrides are
        # needed; the alternative, clearing ``methods`` outright, trips the
        # "Methods must be a list" assertion in FastAPI's OpenAPI builder and
        # takes /openapi.json down with it.
        await self.app(scope, receive, send)


# Its own router only so the route class applies to these two routes and not to
# the four supported methods, which must keep answering exactly their own.
_fallback_router = APIRouter(prefix=WEBDAV_PREFIX, route_class=_AnyMethodRoute, include_in_schema=False)


@_fallback_router.api_route("", methods=UNROUTED_METHODS)
@_fallback_router.api_route("/{dav_path:path}", methods=UNROUTED_METHODS)
async def webdav_unsupported(
    dav_path: str = "",
    principal: _Principal = Depends(webdav_principal),
) -> Response:
    """Refuse every method no mode implements, naming the ones this mode does.

    Answered here rather than left to the framework so the 405 carries a
    truthful ``Allow`` header; a client that sees the header stops retrying and
    reports a read-only share instead of a broken one. The declared list is
    only an example — the route class widens it to everything else, so a
    versioning client's ``REPORT`` and a scanner's ``POST`` get the same
    answer, behind the same ``webdav_mode`` gate. A write method in ``read``
    mode never reaches here: its own handler answers, with the same ``Allow``.
    """
    raise _method_not_allowed(principal.mode)


# Appended rather than ``include_router``-ed, which refuses a route whose path
# is empty — and ``/webdav`` with no trailing slash is one. Last in the list, so
# a method the four handlers above declare still wins the full match.
router.routes.extend(_fallback_router.routes)
