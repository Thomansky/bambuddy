"""The archive's job number in Git backup and restore.

``PrintArchive.job_number`` is the queue item's running number, copied at
dispatch, and quotes and invoices are filed under it. The queue row is deleted
once the order is done, so the archive holds the only copy. The collector and
the restore both build their rows from explicit keys, so a column missing from
either is lost on a rebuilt instance.
"""

from datetime import datetime

import pytest
from sqlalchemy import select

from backend.app.models.archive import PrintArchive
from backend.app.services.github_backup import GitHubBackupService
from backend.app.services.github_restore import ARCHIVES_PATH, GitHubRestoreService, _CategoryTally

STARTED_AT = datetime(2026, 3, 1, 10, 0, 0)


def _entry(**overrides):
    entry = {
        "id": 77,
        "filename": "benchy.3mf",
        "file_size": 2048,
        "content_hash": "abc123",
        "started_at": str(STARTED_AT),
        "created_at": str(STARTED_AT),
    }
    entry.update(overrides)
    return entry


async def _local_archive(db, job_number):
    db.add(
        PrintArchive(
            filename="benchy.3mf",
            file_path="/data/benchy.3mf",
            file_size=2048,
            content_hash="abc123",
            started_at=STARTED_AT,
            job_number=job_number,
        )
    )
    await db.commit()


@pytest.mark.asyncio
async def test_job_number_survives_collect_then_restore(db_session):
    """Both halves, end to end, onto an instance where the row is gone."""
    await _local_archive(db_session, "2026-0042")

    files: dict = {}
    await GitHubBackupService()._collect_archives(db_session, files)
    payload = files[ARCHIVES_PATH]
    assert payload["archives"][0]["job_number"] == "2026-0042"

    await db_session.execute(PrintArchive.__table__.delete())
    await db_session.commit()

    tally = _CategoryTally()
    await GitHubRestoreService()._restore_archives(db_session, payload, False, tally, {})
    await db_session.commit()

    row = (await db_session.execute(select(PrintArchive))).scalar_one()
    assert tally.restored == 1
    assert row.job_number == "2026-0042"


@pytest.mark.asyncio
async def test_an_older_backup_without_the_key_keeps_the_live_job_number(db_session):
    """Overwrite is a blanket setattr; an absent key must not become NULL."""
    await _local_archive(db_session, "2026-0042")

    tally = _CategoryTally()
    await GitHubRestoreService()._restore_archives(db_session, {"archives": [_entry()]}, True, tally, {})
    await db_session.commit()

    row = (await db_session.execute(select(PrintArchive))).scalar_one()
    assert tally.restored == 1
    assert row.job_number == "2026-0042"


@pytest.mark.asyncio
async def test_overwrite_takes_the_backups_job_number_when_present(db_session):
    """Present means the backup knows the value, including an explicit null."""
    await _local_archive(db_session, "2026-0042")

    await GitHubRestoreService()._restore_archives(
        db_session, {"archives": [_entry(job_number="2026-0007")]}, True, _CategoryTally(), {}
    )
    await db_session.commit()
    row = (await db_session.execute(select(PrintArchive))).scalar_one()
    assert row.job_number == "2026-0007"

    await GitHubRestoreService()._restore_archives(
        db_session, {"archives": [_entry(job_number=None)]}, True, _CategoryTally(), {}
    )
    await db_session.commit()
    await db_session.refresh(row)
    assert row.job_number is None


@pytest.mark.asyncio
async def test_an_older_backup_without_the_key_inserts_without_a_job_number(db_session):
    """On insert there is nothing to keep, so the row takes the model default."""
    tally = _CategoryTally()
    await GitHubRestoreService()._restore_archives(db_session, {"archives": [_entry()]}, False, tally, {})
    await db_session.commit()

    row = (await db_session.execute(select(PrintArchive))).scalar_one()
    assert tally.restored == 1
    assert row.job_number is None
