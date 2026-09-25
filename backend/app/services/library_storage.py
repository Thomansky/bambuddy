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
import hashlib
import logging
import os
import shutil
from dataclasses import dataclass, field
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


def directory_component(name: str | None, number: str | None, *, fallback_id: int | None = None) -> str:
    """The one directory-name component a folder with this name maps to.

    A folder name is a display string, not a path component: it can hold a
    separator, it can be ``..``, and an order folder filed under a number alone
    can be empty. ``safe_path_component`` reduces all three to something a
    single ``mkdir`` accepts, and the number — then the id — is the fallback so
    the result is never empty.

    Takes the two fields rather than the row because a folder about to be
    created has no row yet, and the directory has to exist before the row is
    worth writing.
    """
    fallback = f"folder-{number or fallback_id or 'unnamed'}"
    return safe_path_component(name or number or "", fallback=fallback)


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


async def folder_directory_for_delete(db: AsyncSession, folder: LibraryFolder) -> Path | None:
    """*folder*'s real directory, read before its row is deleted, or ``None``."""
    root = await configured_storage_root(db)
    if root is None:
        return None
    return await folder_directory(db, root, folder)


def prune_empty_directory(root: Path, directory: Path | None) -> bool:
    """Remove *directory* and any empty directories under it. True if it went.

    A directory still holding files is kept, and that is the whole behaviour:
    the tree is somebody's share, and a folder delete in Bambuddy removing a
    customer's drawings from it is not a trade anyone would accept for tidiness.
    The route says so in its answer, because the files staying means the next
    scan finds them again.
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
    blockers: list[str] = field(default_factory=list)
    missing: list[str] = field(default_factory=list)

    @property
    def total_bytes(self) -> int:
        return sum(move.size for move in self.moves)

    def as_dict(self) -> dict:
        return {
            "file_count": len(self.moves),
            "folder_count": len(self.folders),
            "total_bytes": self.total_bytes,
            "blockers": self.blockers,
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

    Trashed files are included: their bytes are in the managed store too, and a
    migration that leaves them behind never empties it — and restoring one
    afterwards would hand back a row pointing into a store the library no longer
    writes to.

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
            if current.is_external and not is_inside_tree(root, current.external_path):
                # One of the external folders from #124: it already has a home
                # of its own, and its files were never in the managed store.
                return None
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
        if file.is_external:
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
                f"{target} would receive both {claimed[target]} and {file.filename} (file id {file.id}). "
                f"Rename one of them and run the migration again"
            )
            continue
        if target.exists():
            plan.blockers.append(f"{target} already exists on the share. Move or rename it and run again")
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
                "blockers": plan.blockers,
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
        if folder is not None:
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
