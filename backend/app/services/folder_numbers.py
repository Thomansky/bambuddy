"""Carrying the order folder's number onto the project made from it.

The number is born on the folder, while the enquiry is still an enquiry (see
``number_series.SERIES_LIBRARY_FOLDER``). When the order is placed and a
project comes out of that folder, the project has to carry the *same* number:
the enquiry, the quote, the print and the invoice are all filed under it, and a
project that drew a fresh one at that point would break the only thing the
number is for.

Two rules bound the handover:

* A project that already has a number keeps it. A number handed out on paper is
  not ours to change, so linking an existing, numbered project to a numbered
  folder never renumbers it — only a project with no number of its own takes
  the folder's.
* A number already on another project is not taken a second time.
  ``projects.number`` is unique, so the caller falls back to the project series
  instead: losing the create over a duplicate identifier would be worse than
  the duplicate itself.
"""

import logging

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.models.library import LibraryFolder
from backend.app.models.project import Project

logger = logging.getLogger(__name__)


async def project_number_taken(db: AsyncSession, number: str, exclude_id: int | None = None) -> bool:
    """Is *number* already on a project other than ``exclude_id``?"""
    query = select(Project.id).where(Project.number == number)
    if exclude_id is not None:
        query = query.where(Project.id != exclude_id)
    return (await db.execute(query.limit(1))).scalar_one_or_none() is not None


async def folder_number_taken(db: AsyncSession, number: str, exclude_id: int | None = None) -> bool:
    """Is *number* already on a library folder other than ``exclude_id``?"""
    query = select(LibraryFolder.id).where(LibraryFolder.number == number)
    if exclude_id is not None:
        query = query.where(LibraryFolder.id != exclude_id)
    return (await db.execute(query.limit(1))).scalar_one_or_none() is not None


async def inherit_folder_number(db: AsyncSession, project: Project, folder: LibraryFolder) -> bool:
    """Give *project* the number of the *folder* it came out of.

    Returns True when the project was numbered by this call. Does nothing — and
    says so — when the folder has no number, when the project already carries
    one, or when that number is already on another project.

    Runs inside the caller's transaction and never commits, so it rides with
    the create or the link that triggered it.
    """
    if not folder.number or project.number:
        return False
    if await project_number_taken(db, folder.number, exclude_id=project.id):
        logger.warning(
            "Folder %d carries number %r, which is already on another project — project %s keeps none",
            folder.id,
            folder.number,
            project.id,
        )
        return False
    project.number = folder.number
    return True
