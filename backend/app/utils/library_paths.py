"""Where a library file's user photos live on disk (#3077).

Photos are Bambuddy-side metadata, so they sit inside the library data dir
regardless of whether the file itself is managed or external:
``<archive_dir>/library/photos/<file_id>/``. Both the routes and the trash
sweeper derive the directory from here — see ``archive_paths`` for why one
path derived in several places is a bug waiting to happen.
"""

from __future__ import annotations

import logging
import shutil
from pathlib import Path

from backend.app.core.config import settings

logger = logging.getLogger(__name__)


def library_photos_dir(file_id: int) -> Path:
    """The photo directory for library file *file_id* (not created)."""
    library_dir = Path(settings.archive_dir) / "library"
    return library_dir / "photos" / str(file_id)  # SEC-PATH-OK: file_id is an int primary key


def remove_library_photos_dir(file_id: int) -> None:
    """Best-effort removal of a file's photo directory and everything in it."""
    photos_dir = library_photos_dir(file_id)
    if not photos_dir.is_dir():
        return
    try:
        shutil.rmtree(photos_dir)
    except OSError as e:
        logger.warning("Failed to remove library photos dir %s: %s", photos_dir, e)
