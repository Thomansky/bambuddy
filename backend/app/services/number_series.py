"""Running numbers for projects and queued jobs.

A print farm's paperwork runs on one identifier: the enquiry becomes a job, the
job is quoted, printed, delivered and invoiced under the same number. This
module owns handing those numbers out.

The only thing that matters here is that a number is never handed out twice and
never silently skipped, so :func:`allocate_number` advances the counter with a
compare-and-swap inside the caller's transaction — see its docstring.
"""

import logging

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.core.db_dialect import is_sqlite
from backend.app.models.number_series import NumberSeries

logger = logging.getLogger(__name__)

SERIES_PROJECT = "project"
SERIES_QUEUE_JOB = "queue_job"

# The two series every install starts with. Seeded disabled so nothing changes
# for an existing install until someone turns one on.
DEFAULT_SERIES_KEYS: tuple[str, ...] = (SERIES_PROJECT, SERIES_QUEUE_JOB)

# Width of projects.number / print_queue.job_number / print_archives.job_number.
# PostgreSQL rejects an over-long value outright and SQLite would store it,
# leaving the same series rendering differently on two installs — so a
# combination that cannot fit is refused when it is saved, not when it is used.
MAX_RENDERED_LENGTH = 32

# A stale read can only lose the compare-and-swap below on a backend that lets
# one happen at all; one extra lap is already more than that needs.
_ALLOCATE_ATTEMPTS = 3


def render_number(prefix: str, value: int, padding: int, suffix: str) -> str:
    """Render *value* the way the series would.

    ``zfill`` never truncates, so a counter that outgrows its padding keeps
    counting and simply gets one character wider.

    Kept in step with ``renderSeriesNumber`` in the frontend, which draws the
    "next: …" preview — the preview is a promise about what the backend will
    produce.
    """
    return f"{prefix or ''}{str(value).zfill(max(padding, 0))}{suffix or ''}"


async def allocate_number(db: AsyncSession, key: str) -> str | None:
    """Consume the next number of series *key* and return it rendered.

    Returns ``None`` when the series is disabled or unknown; a caller that gets
    ``None`` simply stores nothing.

    Runs inside the **caller's** transaction and never commits: a create that
    rolls back afterwards gives the number back instead of burning it.

    Two concurrent creates must never receive the same number. The row is taken
    ``FOR UPDATE`` on PostgreSQL, which makes the second caller wait and then
    re-read; SQLite has no row locks but serialises writers, so the loser of the
    race cannot commit an update written against a snapshot the winner has
    already moved. The advance is a compare-and-swap on the value that was read
    either way, so the counter can only move one step per allocation even on a
    backend whose isolation would otherwise allow a stale read through.
    """
    for _ in range(_ALLOCATE_ATTEMPTS):
        # Columns rather than the ORM entity: nothing here should end up in the
        # identity map, where a later read could serve the pre-advance value.
        stmt = select(
            NumberSeries.enabled,
            NumberSeries.prefix,
            NumberSeries.suffix,
            NumberSeries.next_value,
            NumberSeries.padding,
        ).where(NumberSeries.key == key)
        if not is_sqlite():
            stmt = stmt.with_for_update()
        row = (await db.execute(stmt)).first()

        if row is None or not row.enabled:
            return None

        rendered = render_number(row.prefix, row.next_value, row.padding, row.suffix)
        if len(rendered) > MAX_RENDERED_LENGTH:
            # Refused before the counter moves, so nothing is consumed: the
            # series is simply off until an admin shortens it.
            logger.error(
                "Number series %r would render %r (%d characters, limit %d) — handing out nothing",
                key,
                rendered,
                len(rendered),
                MAX_RENDERED_LENGTH,
            )
            return None

        advanced = await db.execute(
            update(NumberSeries)
            .where(NumberSeries.key == key, NumberSeries.next_value == row.next_value)
            .values(next_value=row.next_value + 1)
        )
        if advanced.rowcount == 1:
            return rendered

        logger.warning("Number series %r was advanced concurrently, retrying allocation", key)

    raise RuntimeError(f"Could not allocate a number from series {key!r} — the counter kept moving under us")
