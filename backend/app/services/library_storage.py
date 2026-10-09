"""Where the File Manager's library keeps its bytes (#3160).

Two modes. ``managed`` is what every install has always done: the bytes live
flat under ``archive/library/files`` with a UUID for a name, Bambuddy owns them,
and renaming a folder is one database write. ``directory`` points the library at
a real directory tree — typically a mounted share — where folders are real
directories, files keep their real names, and the database indexes rather than
owns.

A directory-mode folder *is* an external folder in the model's terms:
``is_external=True`` with its own ``external_path``. That is deliberate rather
than a second concept — everything #124 already built for external folders (the
scan reconciliation, the read-only flag, the "never unlink somebody else's
bytes" rule, the WebDAV write path, ``_resolve_upload_destination``) then applies
to the tree instead of having a parallel implementation to keep honest. What
this module adds is the piece external folders never had: a *root*, so a writer
with no folder in hand still knows where the library lives.

The root is also what gives the tree a trash of its own. #124's delete drops
the row and leaves the bytes, which in the tree means the next scan files them
straight back into the library; so a delete there moves them into
``<root>/.bambuddy-trash`` instead, and the row goes through the ordinary trash
pointing at them. A purge unlinks bytes from that directory and from nowhere
else on the share.

Two rules run through everything here:

* Every constructed path goes through ``safe_join_under`` / ``assert_under``
  against the configured root. A folder somebody named ``..`` in Explorer must
  not be able to steer a write out of the tree, and a stored ``external_path``
  is just a string in a column — it is evidence of nothing.
* An unreachable root is an error, never a quiet fallback. Writing to the
  managed store because the share is down would scatter a farm's files across
  two places with nothing saying which, so the writers refuse and say the mount
  is not there.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import logging
import os
import re
import shutil
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.models.library import LibraryFile, LibraryFolder
from backend.app.utils.filename import safe_path_component
from backend.app.utils.safe_path import PathTraversalError, assert_under, safe_join_under

logger = logging.getLogger(__name__)

MODE_MANAGED = "managed"
MODE_DIRECTORY = "directory"
LIBRARY_STORAGE_MODES = (MODE_MANAGED, MODE_DIRECTORY)

SETTING_MODE = "library_storage_mode"
SETTING_PATH = "library_storage_path"

# How deep a folder chain may be walked before the walk is treated as corrupt.
# The API refuses to make a folder its own ancestor, so a cycle means the table
# is already broken — but this walk runs inside upload and migration requests,
# and hanging one of those is worse than refusing it.
_MAX_CHAIN_DEPTH = 64


def storage_path_problem(path_str: str | None) -> str | None:
    """Why *path_str* cannot be the library's home, or ``None`` if it can.

    The return value is the predicate half of a sentence — "The library storage
    path does not exist" — so the caller decides the subject and every refusal
    names which of the conditions failed rather than saying "invalid path".
    """
    candidate = (path_str or "").strip()
    if not candidate:
        return "is not set"
    path = Path(candidate)
    if not path.is_absolute():
        return f"is not an absolute path: {candidate}"
    for reserved in _reserved_roots():
        try:
            if path.resolve().is_relative_to(reserved):
                return f"is inside a Bambuddy-managed directory ({reserved})"
        except OSError:  # pragma: no cover - resolve() on a broken mount
            return f"cannot be resolved: {candidate}"
    if not path.exists():
        return f"does not exist: {candidate}"
    if not path.is_dir():
        return f"is not a directory: {candidate}"
    if not os.access(path, os.W_OK):
        return f"is not writable: {candidate}"
    return None


def _reserved_roots() -> tuple[Path, ...]:
    """Bambuddy's own data directories, which the tree may never be inside.

    Imported at call time: ``routes.library`` imports this module, and the
    reserved set is a security list that must not be copied into a second
    place where the two can drift.
    """
    from backend.app.api.routes.library import _bambuddy_reserved_roots

    return _bambuddy_reserved_roots()


async def storage_mode(db: AsyncSession) -> str:
    """The library's storage mode, normalised, defaulting to ``managed``.

    Read straight from the settings row rather than through the settings
    response, and an unrecognised value reads as ``managed``: the mode decides
    where a write lands, and the only safe reading of a value nobody wrote is
    the behaviour every install already had.
    """
    from backend.app.api.routes.settings import get_setting

    stored = (await get_setting(db, SETTING_MODE) or "").strip().lower()
    return stored if stored in LIBRARY_STORAGE_MODES else MODE_MANAGED


async def configured_storage_root(db: AsyncSession) -> Path | None:
    """The tree's root, or ``None`` when the library is in managed mode.

    Says nothing about whether the mount is up — a listing, a scan and a
    migration plan all need the path even when it is unreachable, and each has
    its own answer for that. Writers call :func:`storage_root_for_write`.
    """
    if await storage_mode(db) != MODE_DIRECTORY:
        return None
    from backend.app.api.routes.settings import get_setting

    raw = (await get_setting(db, SETTING_PATH) or "").strip()
    if not raw:
        # The mode switch refuses an empty path, so this is a row edited by
        # hand or a restored backup. Managed storage is the fail-safe: files
        # keep landing somewhere the database can find them.
        logger.warning("library_storage_mode is 'directory' but library_storage_path is empty — using managed storage")
        return None
    path = Path(raw)
    if not path.is_absolute():
        logger.warning("library_storage_path %r is not absolute — using managed storage", raw)
        return None
    return path


async def storage_root_for_write(db: AsyncSession) -> Path | None:
    """The tree's root, checked, for a caller about to write into it.

    ``None`` means managed mode and the caller's existing behaviour. An
    unreachable root raises 400 naming the problem rather than returning None,
    because a write that silently lands in the managed store while the share is
    down is a file the user will look for on the share and not find.
    """
    root = await configured_storage_root(db)
    if root is None:
        return None
    problem = storage_path_problem(str(root))
    if problem:
        raise HTTPException(status_code=400, detail=f"The library storage path {problem}")
    return root.resolve()


async def storage_root_if_usable(db: AsyncSession) -> Path | None:
    """The tree's root, or ``None`` when it cannot receive a write right now.

    For the one writer that must not raise: slicing has already spent minutes
    producing bytes, and :func:`storage_root_for_write`'s 400 would throw them
    away. This logs the problem and lets the caller fall back to managed
    storage, where the row still finds the file.
    """
    root = await configured_storage_root(db)
    if root is None:
        return None
    problem = storage_path_problem(str(root))
    if problem:
        logger.warning("Library storage path %s — writing to managed storage instead", problem)
        return None
    return root.resolve()


def is_inside_tree(root: Path | None, path: Path | str | None) -> bool:
    """Whether *path* is the tree's root or anything under it."""
    if root is None or not path:
        return False
    try:
        return Path(path).resolve().is_relative_to(root.resolve())
    except OSError:  # pragma: no cover - resolve() on a broken mount
        return False


def numbered_name(name: str | None, number: str | None) -> str:
    """The name a numbered folder goes by on the share: ``"001 EBZ"``.

    The number comes first, the way the folder reads in Bambuddy and the way
    order folders are named by hand, so Explorer lists the share in the same
    order and under the same label. A folder that is only a number is just
    the number, and a name that already starts with its number — a folder
    made in Explorer as ``"4026 Gehäuse"`` and numbered afterwards — keeps it
    once instead of becoming ``"4026 4026 Gehäuse"``.
    """
    name = (name or "").strip()
    number = (number or "").strip()
    if not number:
        return name
    if not name:
        return number
    if re.match(rf"{re.escape(number)}(?![0-9A-Za-z])", name):
        return name
    return f"{number} {name}"


def directory_component(name: str | None, number: str | None, *, fallback_id: int | None = None) -> str:
    """The one directory-name component a folder with this name maps to.

    A folder name is a display string, not a path component: it can hold a
    separator, it can be ``..``, and an order folder filed under a number alone
    can be empty. ``safe_path_component`` reduces all three to something a
    single ``mkdir`` accepts, and the number — then the id — is the fallback so
    the result is never empty. A numbered folder's directory carries the
    number in front of the name (:func:`numbered_name`).

    Takes the two fields rather than the row because a folder about to be
    created has no row yet, and the directory has to exist before the row is
    worth writing.
    """
    fallback = f"folder-{number or fallback_id or 'unnamed'}"
    return safe_path_component(numbered_name(name, number), fallback=fallback)


def folder_component(folder: LibraryFolder) -> str:
    """The one directory-name component *folder* maps to."""
    return directory_component(folder.name, folder.number, fallback_id=folder.id)


async def folder_chain(db: AsyncSession, folder: LibraryFolder) -> list[LibraryFolder]:
    """*folder* and its ancestors, outermost first."""
    chain = [folder]
    current = folder
    while current.parent_id is not None and len(chain) < _MAX_CHAIN_DEPTH:
        parent = (
            await db.execute(select(LibraryFolder).where(LibraryFolder.id == current.parent_id))
        ).scalar_one_or_none()
        if parent is None:
            break
        chain.append(parent)
        current = parent
    chain.reverse()
    return chain


async def folder_directory(db: AsyncSession, root: Path, folder: LibraryFolder | None) -> Path | None:
    """The real directory *folder* maps to inside the tree, or ``None``.

    ``None`` for a folder that is not part of the tree at all — a managed folder
    that predates the migration, or one of #124's external folders pointing at
    a different mount. Those keep the behaviour they already had; guessing a
    directory for them would put a file somewhere the row does not name.

    A stored ``external_path`` is trusted only after the containment check: the
    column is a string, and a row edited by hand or restored from another
    install must not be able to name a write target outside the root.
    """
    if folder is None:
        return root
    if not folder.is_external or not folder.external_path:
        return None
    try:
        return assert_under(root, Path(folder.external_path), http=False)
    except PathTraversalError:
        logger.warning(
            "Folder %s points at %r, which is outside the library storage path %s",
            folder.id,
            folder.external_path,
            root,
        )
        return None


def adopt_or_create_directory(directory: Path, root: Path) -> bool:
    """Make *directory* exist. Returns True when it was created here.

    An existing directory is *adopted* rather than refused: somebody making the
    folder in Explorer first and then in Bambuddy is the normal way this mode
    gets used, and two names for one directory is the outcome a refusal would
    protect against. A name already taken by a *file* is a real conflict and
    raises 409.
    """
    assert_under(root, directory)
    if directory.is_dir():
        return False
    if directory.exists():
        raise HTTPException(status_code=409, detail=f"A file named {directory.name!r} already exists on the share")
    try:
        directory.mkdir(parents=True)
    except FileExistsError:  # pragma: no cover - lost race with another writer
        return False
    except OSError as exc:
        raise HTTPException(status_code=400, detail=f"Could not create the directory on the share: {exc}") from None
    return True


async def descendant_rows(db: AsyncSession, folder_id: int) -> tuple[list[LibraryFolder], list[LibraryFile]]:
    """Every folder and file row under *folder_id*, at any depth.

    Excludes *folder_id* itself. Trashed files are included: their bytes are
    still on the share and a rename has to keep pointing at them, or restoring
    from the trash hands back a row whose path no longer exists.
    """
    folders: list[LibraryFolder] = []
    files: list[LibraryFile] = []
    pending = [folder_id]
    seen: set[int] = set()
    while pending:
        current = pending.pop()
        if current in seen:
            continue
        seen.add(current)
        children = (await db.execute(select(LibraryFolder).where(LibraryFolder.parent_id == current))).scalars().all()
        for child in children:
            folders.append(child)
            pending.append(child.id)
        files.extend((await db.execute(select(LibraryFile).where(LibraryFile.folder_id == current))).scalars().all())
    return folders, files


async def rewrite_subtree_paths(db: AsyncSession, folder: LibraryFolder, old_dir: Path, new_dir: Path) -> int:
    """Re-point *folder*'s row and every descendant's stored path at *new_dir*.

    Called after the directory has already been renamed on disk, so the old
    paths no longer exist and a row left behind is a file nothing can open.
    Returns how many rows were rewritten, for the log line the caller leaves if
    the commit that follows fails.

    Only paths that were actually under *old_dir* are touched. A descendant
    pointing elsewhere is either one of #124's external folders mounted inside
    the tree or a managed row that predates the migration; both are left alone,
    because a rename of a directory did not move them.
    """
    rewritten = 0
    folder.external_path = str(new_dir)
    rewritten += 1
    folders, files = await descendant_rows(db, folder.id)
    for child in folders:
        moved = _reparent(child.external_path, old_dir, new_dir)
        if moved is not None:
            child.external_path = moved
            rewritten += 1
    for file in files:
        if not file.is_external:
            continue
        moved = _reparent(file.file_path, old_dir, new_dir)
        if moved is not None:
            file.file_path = moved
            rewritten += 1
    return rewritten


def _reparent(stored: str | None, old_dir: Path, new_dir: Path) -> str | None:
    """*stored* with its *old_dir* prefix swapped for *new_dir*, or ``None``.

    Compared on path parts rather than on the string, so ``/mnt/lib-old`` is not
    treated as living under ``/mnt/lib``.
    """
    if not stored:
        return None
    try:
        relative = Path(stored).relative_to(old_dir)
    except ValueError:
        return None
    return str(new_dir / relative)


async def tree_relative_parts(db: AsyncSession, folder: LibraryFolder | None) -> list[str]:
    """The directory components *folder* maps to, relative to the tree root.

    Built from the folder names rather than from any stored path, which is what
    the migration needs: a managed folder has no path yet, and this is the
    answer to "where would it go".
    """
    if folder is None:
        return []
    return [folder_component(link) for link in await folder_chain(db, folder)]


def target_in_tree(root: Path, parts: list[str], filename: str) -> Path:
    """``<root>/<parts…>/<filename>``, resolved and asserted under *root*."""
    return safe_join_under(root, *parts, filename, http=False)


# ── The folder operations, filesystem half first ───────────────────────────
# Every one of these does the filesystem work and hands the caller what the row
# should now say, leaving the commit to the route. The order is deliberate and
# the same throughout: the share first, the database second. A mkdir or rename
# that fails must leave the database untouched — the alternative is a row that
# names a directory nobody can open, which no scan can tell from a directory
# somebody deleted by hand.


async def prepare_folder_directory(
    db: AsyncSession,
    parent: LibraryFolder | None,
    *,
    name: str | None,
    number: str | None,
) -> Path | None:
    """Make the real directory a folder about to be created maps to.

    Returns the directory, or ``None`` when this folder is not part of a tree —
    managed mode, or a child of a folder that predates the migration. ``None``
    means "create the row exactly as before", so directory mode never has to be
    special-cased at the call site beyond passing what comes back.
    """
    root = await configured_storage_root(db)
    if root is None:
        return None
    parent_dir = await folder_directory(db, root, parent)
    if parent_dir is None:
        # The parent is one of #124's external folders on another mount, or a
        # managed folder the migration has not taken yet. Either way its
        # children belong where it is, not in the tree.
        return None
    root = await storage_root_for_write(db)
    directory = safe_join_under(parent_dir, directory_component(name, number), http=False)
    # Adopting a directory made in Explorer is the normal case; adopting one a
    # folder already lives in is not. A new "001 EBZ" next to EBZ filed under
    # 001 would share its directory, and deleting the newcomer would take the
    # other folder's files to the trash with it.
    claimed = (
        await db.execute(
            select(LibraryFolder.id).where(
                LibraryFolder.is_external.is_(True),
                LibraryFolder.external_path.in_({str(directory), str(directory.resolve())}),
            )
        )
    ).first()
    if claimed is not None:
        raise HTTPException(
            status_code=409, detail=f"{directory.name!r} on the share already belongs to another folder"
        )
    adopt_or_create_directory(directory, root)
    return directory


async def relocate_folder_directory(db: AsyncSession, folder: LibraryFolder) -> Path | None:
    """Move *folder*'s real directory to where its row now says it belongs.

    Called from the update route after the name and the parent on the row have
    already been changed and before the commit: the stored ``external_path``
    still holds where the directory is *now*, and the row holds where it should
    be. Returns the new directory when one was moved, ``None`` when there was
    nothing on disk to move.

    Renames the whole subtree in one ``os.rename`` — a directory tree moves
    atomically on every filesystem Bambuddy supports, which is the reason a
    folder maps to a directory at all rather than to a path recomputed per file.
    """
    root = await configured_storage_root(db)
    if root is None:
        return None
    old_dir = await folder_directory(db, root, folder)
    if old_dir is None or not old_dir.is_dir():
        return None

    root = await storage_root_for_write(db)
    parent: LibraryFolder | None = None
    if folder.parent_id is not None:
        parent = (
            await db.execute(select(LibraryFolder).where(LibraryFolder.id == folder.parent_id))
        ).scalar_one_or_none()
    parent_dir = await folder_directory(db, root, parent)
    if parent_dir is None:
        raise HTTPException(
            status_code=400,
            detail=(
                "This folder lives in the library's directory tree and the folder it is being moved into does not. "
                "Move it inside the tree, or migrate the target folder first"
            ),
        )

    new_dir = safe_join_under(parent_dir, folder_component(folder), http=False)
    if new_dir == old_dir:
        return old_dir
    if new_dir.exists():
        raise HTTPException(
            status_code=409,
            detail=f"{new_dir.name!r} already exists on the share",
        )
    try:
        # to_thread because a rename on a mounted share is network IO, and this
        # runs on the same loop that carries the printers' MQTT traffic.
        await asyncio.to_thread(os.rename, old_dir, new_dir)
    except OSError as exc:
        raise HTTPException(status_code=400, detail=f"Could not move the directory on the share: {exc}") from None

    rewritten = await rewrite_subtree_paths(db, folder, old_dir, new_dir)
    # Logged before the commit, so the pair is on record even if the commit is
    # what fails: at that point the directory has moved and the rows have not,
    # and this line plus Scan is how it gets reconciled.
    logger.info(
        "Library tree: moved %s to %s, re-pointing %d row(s); folder id %s",
        old_dir,
        new_dir,
        rewritten,
        folder.id,
    )
    return new_dir


# Set once the directories of folders numbered before the number went onto
# the share have been renamed to "<number> <name>". Internal: not a field of
# the settings response, which skips keys it does not know.
SETTING_NUMBERED_DIRS_ALIGNED = "library_numbered_dirs_aligned"


async def align_numbered_directories(db: AsyncSession) -> dict:
    """Once: rename the directories of numbered folders to "<number> <name>".

    Folders numbered before the number was part of the directory name still
    sit on the share under the name alone ("EBZ" filed under 001). The edit
    dialog only saves a change, so nothing would ever bring them in line; this
    does it on the first start, parents before children, through the same
    rename a save does. Afterwards the share is the user's again — a folder
    renamed back by hand in Explorer is not fought over on every restart.

    Not marked done while the library is not in a directory or the share is
    unreachable, so a NAS that is down at startup is caught up on the next one.
    A folder that cannot be renamed is logged and skipped. Its name already
    taken on the share is final (saving the folder later renames it); any
    other failure — the directory open in Explorer — leaves the pass unmarked,
    so the next start tries again.
    """
    from backend.app.api.routes.settings import get_setting, set_setting

    if (await get_setting(db, SETTING_NUMBERED_DIRS_ALIGNED) or "").lower() == "true":
        return {"renamed": 0, "failed": 0, "skipped": "already done"}
    root = await configured_storage_root(db)
    if root is None:
        return {"renamed": 0, "failed": 0, "skipped": "not a directory library"}
    problem = storage_path_problem(str(root))
    if problem:
        return {"renamed": 0, "failed": 0, "skipped": f"the storage path {problem}"}
    try:
        if not any(root.iterdir()):
            return {"renamed": 0, "failed": 0, "skipped": "the library directory is empty"}
    except OSError as exc:
        return {"renamed": 0, "failed": 0, "skipped": f"the library directory could not be read: {exc}"}

    numbered = (
        (
            await db.execute(
                select(LibraryFolder).where(
                    LibraryFolder.number.is_not(None),
                    LibraryFolder.number != "",
                    LibraryFolder.is_external.is_(True),
                )
            )
        )
        .scalars()
        .all()
    )
    targets = [f for f in numbered if is_inside_tree(root, f.external_path)]
    # Parents first: a parent's rename re-points its children's rows, so each
    # child is moved from where it is by then.
    targets.sort(key=lambda f: len(Path(f.external_path).parts))
    ids = [f.id for f in targets]

    renamed = failed = 0
    retry_later = False
    for folder_id in ids:
        folder = (await db.execute(select(LibraryFolder).where(LibraryFolder.id == folder_id))).scalar_one_or_none()
        if folder is None or not folder.external_path:
            continue
        current = folder.external_path
        if Path(current).name == folder_component(folder):
            continue
        try:
            moved = await relocate_folder_directory(db, folder)
            await db.commit()
        except Exception as exc:  # noqa: BLE001 - one folder must not stop the rest
            # The rollback expires every row; only plain values from here on.
            await db.rollback()
            failed += 1
            detail = exc.detail if isinstance(exc, HTTPException) else exc
            # A name already taken (409) stays taken; anything else — the
            # directory open in Explorer, the share hiccuping — may pass, so
            # the next start tries again.
            if not (isinstance(exc, HTTPException) and exc.status_code == 409):
                retry_later = True
            logger.warning("Library tree: could not put the number on folder %s (%s): %s", folder_id, current, detail)
            continue
        if moved is not None:
            renamed += 1

    if not retry_later:
        await set_setting(db, SETTING_NUMBERED_DIRS_ALIGNED, "true")
        await db.commit()
    if renamed or failed:
        logger.info(
            "Library tree: number put in front of %d folder directory name(s), %d could not be renamed", renamed, failed
        )
    return {"renamed": renamed, "failed": failed, "skipped": None}


async def folder_directory_for_delete(db: AsyncSession, folder: LibraryFolder) -> Path | None:
    """*folder*'s real directory, read before its row is deleted, or ``None``."""
    root = await configured_storage_root(db)
    if root is None:
        return None
    return await folder_directory(db, root, folder)


def prune_empty_directory(root: Path, directory: Path | None) -> bool:
    """Remove *directory* and any empty directories under it. True if it went.

    A directory still holding files is kept, and that is the whole behaviour:
    the tree is somebody's share, and tidiness is never a reason to remove a
    customer's drawings from it. A folder of the tree goes to the trash whole
    (:func:`trash_tree_folder`); this is for a folder delete that cannot —
    a read-only folder keeps its files where they are, and the route says so
    in its answer — and for an entry of the trash once its last file is gone.
    """
    if directory is None:
        return False
    try:
        assert_under(root, directory, http=False)
    except PathTraversalError:
        logger.warning("Refusing to remove %s: it is outside the library storage path %s", directory, root)
        return False
    if not directory.is_dir():
        return False
    try:
        for current, dirnames, filenames in os.walk(directory, topdown=False):
            if filenames:
                return False
            for dirname in dirnames:
                Path(current, dirname).rmdir()
        directory.rmdir()
    except OSError as exc:
        logger.warning("Could not remove %s from the share: %s", directory, exc)
        return False
    return True


# ── The trash on the share ──────────────────────────────────────────────────
# A delete in the tree has to take the bytes out of the tree: the row is only
# the index, and the scan believes the directory. So they move into a hidden
# directory at the root, one entry per deletion, and the row goes into the
# ordinary trash pointing at them. Restore moves them back; the sweeper and
# "Delete now" unlink them there and nowhere else. The same order as above:
# the share first, the database second, and a move that fails leaves every row
# as it was.

TRASH_DIR_NAME = ".bambuddy-trash"

# ``<stamp>-f<file id>`` for one file, ``<stamp>-d<folder id>`` for a folder's
# directory, stamped in UTC — the sweeper reads an entry's age from its name.
# The counter only appears when one row is deleted twice within a second.
_TRASH_STAMP_FORMAT = "%Y%m%d-%H%M%S"
_TRASH_ENTRY_NAME = re.compile(r"^(?P<stamp>\d{8}-\d{6})-[fd]\d+(?:-\d+)?$")


@dataclass(slots=True)
class TrashMove:
    """One move between the tree and its trash, kept so it can be undone."""

    source: Path
    target: Path
    entry: Path


def trash_directory(root: Path) -> Path:
    """``<root>/.bambuddy-trash``, where the tree keeps what was deleted from it."""
    return root / TRASH_DIR_NAME


def _located_in_trash(root: Path | None, path: Path | str | None) -> tuple[Path, Path] | None:
    """``(entry, path)`` for a stored path lying inside an entry of the trash.

    The directory part is resolved and the name is not: a symlink moved into
    the trash is the link's own entry there, and a link elsewhere pointing
    *into* the trash is not in it — so what this hands back to be unlinked is
    always something inside the trash, never what a link reaches outside it.
    The trash directory and an entry are not "in" the trash themselves; only
    something below an entry is.
    """
    if root is None or not path:
        return None
    candidate = Path(path)
    if candidate.name in ("", ".", ".."):
        return None
    try:
        trash = trash_directory(root).resolve()
        located = candidate.parent.resolve() / candidate.name
        relative = located.relative_to(trash)
    except (OSError, ValueError):
        return None
    if len(relative.parts) < 2:
        return None
    return trash / relative.parts[0], located


def trash_entry_of(root: Path | None, path: Path | str | None) -> Path | None:
    """The trash entry *path* lies in, or ``None`` when it is not in the tree's trash."""
    located = _located_in_trash(root, path)
    return located[0] if located is not None else None


def _new_trash_entry(root: Path, kind: str, row_id: int) -> Path:
    """Make a fresh, empty entry in the tree's trash and return it."""
    trash = trash_directory(root)
    trash.mkdir(exist_ok=True)
    name = f"{datetime.now(timezone.utc).strftime(_TRASH_STAMP_FORMAT)}-{kind}{row_id}"
    for attempt in range(1, 100):
        entry = safe_join_under(trash, name if attempt == 1 else f"{name}-{attempt}", http=False)
        try:
            entry.mkdir()
        except FileExistsError:
            continue
        return entry
    raise OSError(f"every trash entry name for {name} is taken")


def _rmdir_quietly(directory: Path) -> None:
    """Remove a directory that was made for nothing. Only ever an empty one."""
    with contextlib.suppress(OSError):
        directory.rmdir()


async def move_into_trash(root: Path, kind: str, row_id: int, source: Path) -> TrashMove:
    """Move *source* — a file, or a folder's whole directory — into a new entry.

    One ``os.rename``: the trash is on the same share, so the move is atomic
    and a directory takes its subtree along. A failure raises 400 naming it,
    after taking the empty entry away again, so a refused delete changes
    neither the share nor the rows.
    """
    try:
        entry = await asyncio.to_thread(_new_trash_entry, root, kind, row_id)
    except (OSError, PathTraversalError) as exc:
        raise HTTPException(status_code=400, detail=f"Could not create the trash on the share: {exc}") from None
    try:
        target = safe_join_under(entry, source.name, http=False)
        # to_thread because a rename on a mounted share is network IO.
        await asyncio.to_thread(os.rename, source, target)
    except (OSError, PathTraversalError) as exc:
        await asyncio.to_thread(_rmdir_quietly, entry)
        raise HTTPException(
            status_code=400,
            detail=f"Could not move {source.name!r} into the trash on the share: {exc}",
        ) from None
    return TrashMove(source=source, target=target, entry=entry)


async def undo_trash_moves(moves: Sequence[TrashMove]) -> None:
    """Put back what a request moved, newest first, when its database write failed.

    The rows still name the places the bytes came from, and a file left in
    the trash behind a live row is one the next scan drops from the library
    while its bytes wait out the retention window — a delete nobody can undo.
    Best effort: a move that cannot be reversed is logged with both paths,
    which is what putting it right by hand needs.
    """
    for move in reversed(moves):
        try:
            await asyncio.to_thread(os.rename, move.target, move.source)
        except OSError as exc:
            logger.error(
                "Library trash: could not move %s back to %s after the database write failed: %s",
                move.target,
                move.source,
                exc,
            )
            continue
        await asyncio.to_thread(_rmdir_quietly, move.entry)


async def trash_tree_file(db: AsyncSession, file: LibraryFile) -> TrashMove | None:
    """Move a file of the tree into the trash on the share, and trash its row.

    ``None`` when *file* is not part of the tree — managed, on one of #124's
    external folders somewhere else, or in a read-only folder — and the
    caller keeps the behaviour it always had. ``None`` too when the bytes are
    already gone from the share: there is nothing to restore, and the scan
    would drop the row anyway.

    The row keeps its folder, so a restore puts the file back where it was.
    """
    if not file.is_external:
        return None
    root = await configured_storage_root(db)
    if root is None or not is_inside_tree(root, file.file_path):
        return None
    if file.folder_id is not None:
        folder = (
            await db.execute(select(LibraryFolder).where(LibraryFolder.id == file.folder_id))
        ).scalar_one_or_none()
        if folder is not None and folder.external_readonly:
            return None
    root = await storage_root_for_write(db)
    source = Path(file.file_path)
    if not await asyncio.to_thread(os.path.lexists, source):
        return None

    move = await move_into_trash(root, "f", file.id, source)
    file.file_path = str(move.target)
    file.deleted_at = datetime.now(timezone.utc)
    logger.info("Library tree: moved %s to the trash at %s; file id %s", source, move.target, file.id)
    return move


async def trash_tree_folder(db: AsyncSession, folder: LibraryFolder) -> TrashMove | None:
    """Move *folder*'s directory into the trash on the share, and trash its files.

    One rename for the whole subtree. Every file row whose bytes went along is
    re-pointed into the entry, stamped deleted, and taken out of its folder:
    the folder rows are deleted next and ``folder_id`` cascades, so a row left
    in the subtree would go with them and take its restore along. A restored
    file therefore lands in the library's root.

    A row trashed on its own before is taken out the same way, whether or not
    the directory moves — deleting the folder a file was deleted from, once it
    is empty, must not cost that file its restore.

    Returns the move, or ``None`` when no directory moved and the caller keeps
    the behaviour it always had: the folder is not part of the tree (managed,
    another mount, read-only), or its directory is already gone from the
    share. Flushes, so the cascade after it no longer sees the rows that left
    the subtree. An entry holding no file is the caller's to tidy once the
    rows are committed (:func:`tidy_trash_entries`).
    """
    if not folder.is_external:
        return None
    root = await configured_storage_root(db)
    if root is None:
        return None

    directory = None
    if not folder.external_readonly and is_inside_tree(root, folder.external_path):
        directory = await folder_directory(db, root, folder)
    move: TrashMove | None = None
    if directory is not None:
        root = await storage_root_for_write(db)
        # A folder mounted at the root itself is not a directory of the tree,
        # and nothing already in the trash goes into it a second time.
        movable = directory != root and not is_inside_tree(trash_directory(root), directory)
        if movable and await asyncio.to_thread(directory.is_dir):
            move = await move_into_trash(root, "d", folder.id, directory)

    try:
        now = datetime.now(timezone.utc)
        trashed = 0
        _, files = await descendant_rows(db, folder.id)
        for file in files:
            if not file.is_external:
                continue
            moved = _reparent(file.file_path, directory, move.target) if move is not None else None
            if moved is not None:
                file.file_path = moved
                if file.deleted_at is None:
                    file.deleted_at = now
                file.folder_id = None
                trashed += 1
            elif file.deleted_at is not None and trash_entry_of(root, file.file_path) is not None:
                file.folder_id = None
        await db.flush()
    except Exception:
        if move is not None:
            await undo_trash_moves([move])
        raise
    if move is not None:
        logger.info(
            "Library tree: moved %s to the trash at %s with %d file row(s); folder id %s",
            directory,
            move.target,
            trashed,
            folder.id,
        )
    return move


async def restore_trashed_file(db: AsyncSession, file: LibraryFile) -> TrashMove | None:
    """Move a file of the tree back out of the trash on the share.

    ``None`` when the row's bytes are not in the tree's trash — a managed
    file, or an external one that never went through it — and there is
    nothing to move. Otherwise the returned move is what the caller undoes if
    its commit fails, and the entry is the caller's to tidy once it did not.

    The file goes back into its folder's directory under the name it had on
    the share, or into the root when the folder is gone or no longer part of
    the tree, and the row then belongs to no folder. A name taken there in
    the meantime is a 409 and changes nothing: whatever somebody put in its
    place is theirs, and a restore overwriting it would be a delete of its own.
    """
    if not file.is_external:
        return None
    root = await configured_storage_root(db)
    if trash_entry_of(root, file.file_path) is None:
        return None
    root = await storage_root_for_write(db)
    located = _located_in_trash(root, file.file_path)
    if located is None:  # pragma: no cover - the root resolved differently between the two reads
        return None
    entry, source = located

    folder: LibraryFolder | None = None
    if file.folder_id is not None:
        folder = (
            await db.execute(select(LibraryFolder).where(LibraryFolder.id == file.folder_id))
        ).scalar_one_or_none()
    directory = None
    if folder is not None and not folder.external_readonly:
        directory = await folder_directory(db, root, folder)
    if directory is None:
        folder, directory = None, root

    if not await asyncio.to_thread(os.path.lexists, source):
        raise HTTPException(
            status_code=400,
            detail=f"{source.name!r} is no longer in the trash on the share ({entry})",
        )
    try:
        target: Path | None = safe_join_under(directory, source.name, http=False)
    except PathTraversalError:
        # The name is one plain component, so the only way out of the
        # directory is a link already sitting under that name.
        target = None
    if target is None or await asyncio.to_thread(os.path.lexists, target):
        raise HTTPException(
            status_code=409,
            detail=(
                f"{source.name!r} already exists in {directory} on the share. "
                "Rename or move that file, then restore this one again"
            ),
        )

    created = await asyncio.to_thread(adopt_or_create_directory, directory, root)
    try:
        await asyncio.to_thread(os.rename, source, target)
    except OSError as exc:
        if created:
            await asyncio.to_thread(_rmdir_quietly, directory)
        if isinstance(exc, FileExistsError):
            raise HTTPException(
                status_code=409,
                detail=f"{source.name!r} already exists in {directory} on the share",
            ) from None
        raise HTTPException(
            status_code=400,
            detail=f"Could not move {source.name!r} out of the trash on the share: {exc}",
        ) from None

    file.file_path = str(target)
    file.folder_id = folder.id if folder is not None else None
    logger.info("Library tree: restored %s from the trash to %s; file id %s", source, target, file.id)
    return TrashMove(source=source, target=target, entry=entry)


def tidy_trash_entry(root: Path, entry: Path) -> bool:
    """Remove *entry* once nothing but empty directories is left in it."""
    return prune_empty_directory(trash_directory(root), entry)


async def tidy_trash_entries(db: AsyncSession, moves: Sequence[TrashMove]) -> None:
    """Remove the entries of *moves* that hold no file, once the rows are committed.

    A folder deleted while empty leaves an entry of empty directories, and a
    restored file the entry it came out of; nothing can point into either.
    Not before the commit: until then, :func:`undo_trash_moves` may still
    need the entry to move back into.
    """
    if not moves:
        return
    root = await configured_storage_root(db)
    if root is None:
        return
    for move in moves:
        await asyncio.to_thread(tidy_trash_entry, root, move.entry)


def unlink_from_trash(root: Path | None, path: Path | str | None) -> bool:
    """Unlink a file from the tree's trash, then its entry once nothing is left.

    Anything that is not inside an entry of the trash is refused: a trashed
    row pointing at a live file on the share must never cost that file.
    Returns whether something was unlinked.
    """
    located = _located_in_trash(root, path)
    if located is None:
        return False
    entry, target = located
    try:
        target.unlink(missing_ok=True)
    except OSError as exc:
        logger.warning("Library trash: failed to unlink %s from the share: %s", target, exc)
        return False
    tidy_trash_entry(root, entry)
    return True


def remove_expired_trash_entries(root: Path, cutoff: datetime, claimed: Iterable[str]) -> int:
    """Remove the trash entries older than *cutoff* that no row points into.

    What a folder delete leaves once its files are purged: the drawings and
    notes the scan never indexed, and the directories they sat in. Nothing has
    a row for them, so the row sweep never reaches them. Only whole entries
    this module named — the stamp is how their age is known — and never one a
    row in *claimed* still points into, trashed or not.
    """
    trash = trash_directory(root)
    if not trash.is_dir():
        return 0
    keep = {entry for entry in (trash_entry_of(root, path) for path in claimed) if entry is not None}
    removed = 0
    for child in trash.iterdir():
        match = _TRASH_ENTRY_NAME.match(child.name)
        if match is None or child.is_symlink() or not child.is_dir():
            continue
        try:
            stamp = datetime.strptime(match["stamp"], _TRASH_STAMP_FORMAT).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
        if stamp >= cutoff:
            continue
        try:
            entry = assert_under(trash, child, http=False)
        except PathTraversalError:
            continue
        if entry in keep:
            continue
        try:
            shutil.rmtree(entry)
        except OSError as exc:
            logger.warning("Library trash: could not remove the expired entry %s: %s", entry, exc)
            continue
        removed += 1
    return removed


# ── Moving an existing library into the tree ────────────────────────────────
# A separate, explicit action, never a side effect of the switch: the switch
# only says where new files go, and somebody who flips it to look at the
# setting has not asked for their library to be moved.


@dataclass(slots=True)
class MigrationMove:
    """One file's move: where it is now and where it is going."""

    file_id: int
    filename: str
    source: Path
    target: Path
    size: int


@dataclass(slots=True)
class MigrationBlocker:
    """One reason the migration will not run, in parts rather than as prose.

    The message is kept for logs and for anything reading the API directly, but
    the parts are what the File Manager renders: a collision is a decision about
    which file gets renamed, and the person making it needs the names in their
    own language, not an English sentence with two paths in it.

    Which is why ``blockers`` stays a list of those messages and the parts ride
    alongside in ``blocker_details``. Changing the field's type instead broke
    every browser tab that was still on the previous build — the old code
    rendered a string and was handed an object — and an upgrade should not
    depend on everybody having reloaded first.
    """

    kind: str
    target: str
    names: list[str]
    message: str

    def as_dict(self) -> dict:
        return {"kind": self.kind, "target": self.target, "names": self.names, "message": self.message}


@dataclass(slots=True)
class MigrationPlan:
    """What a migration would do, worked out before anything is touched.

    ``blockers`` is the important half. A collision cannot be resolved here
    without inventing a name — the managed store allows two files called
    ``part.3mf`` in one folder because their real names are UUIDs, and a
    directory cannot — so the whole run is refused and the user is told which
    files to rename. Silently filing one as ``part (2).3mf`` would leave the
    farm with two files whose names no longer match the drawings they came from.
    """

    moves: list[MigrationMove] = field(default_factory=list)
    folders: list[tuple[int, Path]] = field(default_factory=list)
    blockers: list[MigrationBlocker] = field(default_factory=list)
    missing: list[str] = field(default_factory=list)

    @property
    def total_bytes(self) -> int:
        return sum(move.size for move in self.moves)

    def as_dict(self) -> dict:
        return {
            "file_count": len(self.moves),
            "folder_count": len(self.folders),
            "total_bytes": self.total_bytes,
            "blockers": [blocker.message for blocker in self.blockers],
            "blocker_details": [blocker.as_dict() for blocker in self.blockers],
            "missing": self.missing,
            "moves": [
                {
                    "file_id": move.file_id,
                    "filename": move.filename,
                    "source": str(move.source),
                    "target": str(move.target),
                    "size": move.size,
                }
                for move in self.moves
            ],
        }


async def plan_migration(db: AsyncSession, root: Path) -> MigrationPlan:
    """Work out every directory to make and every file to move, touching nothing.

    Trashed files are left where they are. They would otherwise claim a name in
    the tree that the live file of the same name needs — which is the common
    case, because a file usually lands in the trash after being replaced — and
    "throw the old one away" would then not be an answer to a collision. Their
    bytes stay in the managed store until the trash is emptied.

    Thumbnails are deliberately left where they are. They are Bambuddy's own
    derived data, regenerable from the file, and nobody wants a share full of
    hex-named PNGs next to their drawings.
    """
    plan = MigrationPlan()
    folders = (await db.execute(select(LibraryFolder))).scalars().all()
    by_id = {folder.id: folder for folder in folders}

    def parts_for(folder: LibraryFolder | None) -> list[str] | None:
        """Directory components for *folder*, or None when it is off the tree."""
        parts: list[str] = []
        current = folder
        depth = 0
        while current is not None and depth < _MAX_CHAIN_DEPTH:
            if current.is_external:
                if not is_inside_tree(root, current.external_path):
                    # One of the external folders from #124: it already has a
                    # home of its own, and its files were never in the managed
                    # store.
                    return None
                # Already in the tree: its directory is where its row says, not
                # where its name would put it today. A folder numbered before
                # the number went into directory names is still "EBZ" on the
                # share until it is renamed, and a second run must not invent
                # an empty "001 EBZ" beside it.
                try:
                    anchor = Path(current.external_path).resolve().relative_to(root.resolve()).parts
                except (OSError, ValueError):
                    return None
                parts.reverse()
                return [*anchor, *parts]
            parts.append(folder_component(current))
            current = by_id.get(current.parent_id) if current.parent_id is not None else None
            depth += 1
        parts.reverse()
        return parts

    for folder in folders:
        parts = parts_for(folder)
        if parts is None:
            continue
        plan.folders.append((folder.id, safe_join_under(root, *parts, http=False) if parts else root))

    # Collisions are found on the plan's own targets, not on disk alone: two
    # managed files can share a name inside one folder, and the second copy
    # would silently replace the first.
    claimed: dict[Path, str] = {}
    files = (await db.execute(select(LibraryFile))).scalars().all()
    for file in files:
        if file.is_external or file.deleted_at is not None:
            continue
        source = _managed_source(file)
        if source is None or not source.is_file():
            plan.missing.append(f"{file.filename} (file id {file.id}): its bytes are not in the managed store")
            continue
        folder = by_id.get(file.folder_id) if file.folder_id is not None else None
        if file.folder_id is not None and folder is None:
            plan.missing.append(f"{file.filename} (file id {file.id}): its folder is gone")
            continue
        parts = parts_for(folder)
        if parts is None:
            continue
        target = safe_join_under(root, *parts, file.filename, http=False)
        if target in claimed:
            plan.blockers.append(
                MigrationBlocker(
                    kind="collision",
                    target=str(target),
                    names=[claimed[target], f"{file.filename} (file id {file.id})"],
                    message=(
                        f"{target} would receive both {claimed[target]} and {file.filename} (file id {file.id}). "
                        f"Rename one of them and run the migration again"
                    ),
                )
            )
            continue
        if target.exists():
            plan.blockers.append(
                MigrationBlocker(
                    kind="exists",
                    target=str(target),
                    names=[f"{file.filename} (file id {file.id})"],
                    message=f"{target} already exists on the share. Move or rename it and run again",
                )
            )
            continue
        claimed[target] = f"{file.filename} (file id {file.id})"
        plan.moves.append(
            MigrationMove(
                file_id=file.id,
                filename=file.filename,
                source=source,
                target=target,
                size=source.stat().st_size,
            )
        )
    return plan


def _managed_source(file: LibraryFile) -> Path | None:
    """Where a managed file's bytes are, as an absolute path."""
    from backend.app.api.routes.library import to_absolute_path

    return to_absolute_path(file.file_path)


def _copy_verified(source: Path, target: Path, *, expected_hash: str | None) -> None:
    """Copy *source* to *target* and prove the copy arrived intact.

    Copy, verify, then the caller updates the row, then the source goes: at
    every point in that order a crash leaves a file something can still find.
    The reverse order has a window where the only copy is the one nothing
    points at.
    """
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, target)
    source_size = source.stat().st_size
    copied_size = target.stat().st_size
    if copied_size != source_size:
        target.unlink(missing_ok=True)
        raise OSError(f"the copy is {copied_size} bytes, expected {source_size}")
    if expected_hash:
        digest = hashlib.sha256()
        with open(target, "rb") as fh:
            for block in iter(lambda: fh.read(65536), b""):
                digest.update(block)
        if digest.hexdigest() != expected_hash:
            target.unlink(missing_ok=True)
            raise OSError("the copy does not match the hash on record")


async def run_migration(db: AsyncSession, root: Path) -> dict:
    """Move the managed library into the tree. Idempotent and resumable.

    A second run finds nothing left in the managed store and reports zero moves;
    an interrupted run leaves every file it had not reached where it was, which
    is why the plan is rebuilt from the database on each run rather than stored.
    """
    plan = await plan_migration(db, root)
    if plan.blockers:
        raise HTTPException(
            status_code=409,
            detail={
                "message": "The migration was not started because it would overwrite files",
                "blockers": [blocker.message for blocker in plan.blockers],
                "blocker_details": [blocker.as_dict() for blocker in plan.blockers],
            },
        )

    # The directories first, all of them, including the ones for folders that
    # hold no files yet: a folder without a directory is a folder the next
    # upload cannot write into.
    folders = {folder.id: folder for folder in (await db.execute(select(LibraryFolder))).scalars().all()}
    created = 0
    for folder_id, directory in plan.folders:
        if await asyncio.to_thread(adopt_or_create_directory, directory, root):
            created += 1
        folder = folders.get(folder_id)
        # A folder already in the tree keeps its row exactly as it is: the plan
        # put it where that row says, and rewriting the string could only
        # change its spelling.
        if folder is not None and not (folder.is_external and is_inside_tree(root, folder.external_path)):
            folder.is_external = True
            folder.external_path = str(directory)
            folder.external_readonly = False
    await db.commit()

    moved = 0
    moved_bytes = 0
    failures: list[str] = []
    for move in plan.moves:
        file = (await db.execute(select(LibraryFile).where(LibraryFile.id == move.file_id))).scalar_one_or_none()
        if file is None or file.is_external:
            continue
        try:
            await asyncio.to_thread(_copy_verified, move.source, move.target, expected_hash=file.file_hash)
        except OSError as exc:
            failures.append(f"{move.filename}: {exc}")
            logger.warning("Library migration: %s could not be copied to %s: %s", move.source, move.target, exc)
            continue
        file.file_path = str(move.target)
        file.is_external = True
        await db.commit()
        # Only now, with the row pointing at the copy, is the original
        # redundant. Failing to remove it costs disk space, not data.
        try:
            await asyncio.to_thread(move.source.unlink, True)
        except OSError as exc:
            logger.warning("Library migration: copied %s but could not remove the original: %s", move.source, exc)
        logger.debug("Library migration: %s -> %s", move.source, move.target)
        moved += 1
        moved_bytes += move.size

    summary = {
        "moved": moved,
        "moved_bytes": moved_bytes,
        "directories_created": created,
        "skipped": plan.missing,
        "failures": failures,
    }
    logger.info(
        "Library migration finished: %d file(s), %d byte(s), %d new directory/ies, %d failure(s)",
        moved,
        moved_bytes,
        created,
        len(failures),
    )
    return summary
