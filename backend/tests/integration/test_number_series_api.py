"""Running numbers for projects and queued jobs.

The whole feature is one promise: a number is handed out once, to one thing,
and never skipped. Most of what follows pins that promise rather than the API
shape around it.
"""

import asyncio

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

import backend.app.models  # noqa: F401 - populate Base.metadata
from backend.app.core.database import Base
from backend.app.models.number_series import NumberSeries
from backend.app.models.project import Project
from backend.app.services.number_series import (
    SERIES_PROJECT,
    SERIES_QUEUE_JOB,
    allocate_number,
    render_number,
)

pytestmark = pytest.mark.integration


async def _seed_series(db: AsyncSession, key: str, **values) -> NumberSeries:
    """Create (or update) one series row. The app seeds these at startup; tests
    build them directly so they don't depend on init_db having run."""
    series = (await db.execute(select(NumberSeries).where(NumberSeries.key == key))).scalar_one_or_none()
    if series is None:
        series = NumberSeries(key=key)
        db.add(series)
    series.enabled = values.get("enabled", True)
    series.prefix = values.get("prefix", "")
    series.suffix = values.get("suffix", "")
    series.next_value = values.get("next_value", 1)
    series.padding = values.get("padding", 0)
    await db.commit()
    await db.refresh(series)
    return series


async def _next_value(db: AsyncSession, key: str) -> int:
    return (await db.execute(select(NumberSeries.next_value).where(NumberSeries.key == key))).scalar_one()


def _watch_for_locking_reads(db: AsyncSession, monkeypatch) -> list:
    """Record every statement this session runs that asks for a row lock."""
    locked: list = []
    original = db.execute

    async def _spy(statement, *args, **kwargs):
        if getattr(statement, "_for_update_arg", None) is not None:
            locked.append(statement)
        return await original(statement, *args, **kwargs)

    monkeypatch.setattr(db, "execute", _spy)
    return locked


class TestRendering:
    def test_prefix_padding_and_suffix_are_all_applied(self):
        assert render_number("A-", 35, 5, "/26") == "A-00035/26"

    def test_padding_never_truncates(self):
        """The spec's own example: a counter that outgrew its padding keeps
        every digit rather than losing the leading ones."""
        assert render_number("A-", 1125035, 5, "") == "A-1125035"

    def test_no_padding_is_the_bare_number(self):
        assert render_number("", 7, 0, "") == "7"


class TestAllocation:
    @pytest.mark.asyncio
    async def test_the_rendered_number_matches_the_series(self, db_session):
        await _seed_series(db_session, SERIES_PROJECT, prefix="A-", padding=5, suffix="/26", next_value=35)

        assert await allocate_number(db_session, SERIES_PROJECT) == "A-00035/26"

    @pytest.mark.asyncio
    async def test_two_allocations_differ_and_advance_by_exactly_one(self, db_session):
        await _seed_series(db_session, SERIES_PROJECT, prefix="P", padding=3, next_value=9)

        first = await allocate_number(db_session, SERIES_PROJECT)
        second = await allocate_number(db_session, SERIES_PROJECT)
        await db_session.commit()

        assert (first, second) == ("P009", "P010")
        assert await _next_value(db_session, SERIES_PROJECT) == 11

    @pytest.mark.asyncio
    async def test_a_disabled_series_hands_out_nothing(self, db_session):
        await _seed_series(db_session, SERIES_PROJECT, enabled=False, next_value=5)

        assert await allocate_number(db_session, SERIES_PROJECT) is None
        assert await _next_value(db_session, SERIES_PROJECT) == 5

    @pytest.mark.asyncio
    async def test_an_unknown_series_hands_out_nothing(self, db_session):
        assert await allocate_number(db_session, "no-such-series") is None

    @pytest.mark.asyncio
    async def test_a_combination_too_long_for_the_column_is_refused_without_consuming(self, db_session):
        """Thirty-two characters is what the columns hold. Refused before the
        counter moves, so nothing is burnt on a number that cannot be stored."""
        await _seed_series(db_session, SERIES_PROJECT, prefix="X" * 16, suffix="Y" * 16, next_value=4)

        assert await allocate_number(db_session, SERIES_PROJECT) is None
        assert await _next_value(db_session, SERIES_PROJECT) == 4


@pytest.fixture
async def concurrent_db(tmp_path):
    """A file-backed SQLite database two independent sessions can both open.

    ``:memory:`` is pooled onto one connection, which would serialise the two
    callers and prove nothing about the race this guards.
    """
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'series.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    session_maker = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with session_maker() as db:
        await _seed_series(db, SERIES_PROJECT, prefix="A-", padding=4, next_value=1)
    try:
        yield session_maker
    finally:
        await engine.dispose()


class TestConcurrency:
    """Two creates at the same instant must never be given the same number."""

    @staticmethod
    async def _allocate(session_maker) -> str | None:
        """One caller's whole transaction. A caller that loses the write race
        gets an error, which is a create that fails — never a duplicate."""
        async with session_maker() as db:
            try:
                number = await allocate_number(db, SERIES_PROJECT)
                await db.commit()
                return number
            except OperationalError:
                await db.rollback()
                return None

    @pytest.mark.asyncio
    async def test_concurrent_allocations_never_collide(self, concurrent_db):
        results = await asyncio.gather(
            self._allocate(concurrent_db),
            self._allocate(concurrent_db),
            self._allocate(concurrent_db),
            self._allocate(concurrent_db),
        )
        handed_out = [n for n in results if n is not None]

        assert len(set(handed_out)) == len(handed_out), f"the same number went out twice: {results}"
        async with concurrent_db() as db:
            # And the counter moved exactly once per number handed out: no
            # allocation is lost, and none is silently skipped either.
            assert await _next_value(db, SERIES_PROJECT) == 1 + len(handed_out)

    @pytest.mark.asyncio
    async def test_a_counter_that_moves_between_the_read_and_the_write_is_not_reused(
        self, concurrent_db, tmp_path, monkeypatch
    ):
        """The exact hazard the compare-and-swap exists for: a caller reads the
        counter, someone else consumes that very value and commits, and the
        caller then writes its advance against what it read.

        The competing commit is wedged into that window deliberately.  Left to
        timing it never lands there: a bare SELECT on pysqlite does not even
        open a transaction, so a session that reads first just re-reads the new
        value and the window is never entered -- which is how the earlier
        version of this test passed without the guard ever being exercised.
        """
        import sqlite3

        import backend.app.services.number_series as number_series

        real_render = number_series.render_number
        read_values: list[int] = []

        def _render_then_let_the_other_caller_in(prefix, value, padding, suffix):
            # Called between the read and the compare-and-swap, once: the
            # second lap has to find the counter where the other caller left it.
            if not read_values:
                read_values.append(value)
                raw = sqlite3.connect(tmp_path / "series.db")
                try:
                    raw.execute(
                        "UPDATE number_series SET next_value = ? WHERE key = ? AND next_value = ?",
                        (value + 1, SERIES_PROJECT, value),
                    )
                    raw.commit()
                finally:
                    raw.close()
            return real_render(prefix, value, padding, suffix)

        monkeypatch.setattr(number_series, "render_number", _render_then_let_the_other_caller_in)

        async with concurrent_db() as db:
            number = await allocate_number(db, SERIES_PROJECT)
            await db.commit()

        assert read_values == [1], "the competing commit never landed inside the window"
        # A-0001 was taken while this caller held it, so the write against that
        # read has to be refused and the allocation come back with the next one.
        assert number == "A-0002"
        async with concurrent_db() as db:
            assert await _next_value(db, SERIES_PROJECT) == 3

    @pytest.mark.asyncio
    async def test_a_disabled_series_is_never_locked(self, db_session, monkeypatch):
        """A PostgreSQL ``FOR UPDATE`` row lock is held until the caller's
        transaction ends, not until the statement ends. Both series ship
        disabled, so locking before reading ``enabled`` would queue every
        project create and every queue add behind one row on installs that
        never switched the feature on."""
        monkeypatch.setattr("backend.app.services.number_series.is_sqlite", lambda: False)
        await _seed_series(db_session, SERIES_PROJECT, enabled=False)
        locked = _watch_for_locking_reads(db_session, monkeypatch)

        assert await allocate_number(db_session, SERIES_PROJECT) is None

        assert locked == [], "a disabled series was locked FOR UPDATE"

    @pytest.mark.asyncio
    async def test_an_enabled_series_is_still_locked(self, db_session, monkeypatch):
        """The other half: the lock has not simply been dropped."""
        monkeypatch.setattr("backend.app.services.number_series.is_sqlite", lambda: False)
        await _seed_series(db_session, SERIES_PROJECT, prefix="A-", padding=4, next_value=1)
        locked = _watch_for_locking_reads(db_session, monkeypatch)

        assert await allocate_number(db_session, SERIES_PROJECT) == "A-0001"

        assert len(locked) == 1


class TestSeriesAPI:
    @pytest.mark.asyncio
    async def test_series_round_trip_and_preview(self, async_client: AsyncClient, db_session):
        await _seed_series(db_session, SERIES_PROJECT, enabled=False)

        response = await async_client.patch(
            f"/api/v1/number-series/{SERIES_PROJECT}",
            json={"enabled": True, "prefix": "A-", "padding": 5, "next_value": 1125},
        )

        assert response.status_code == 200
        body = response.json()
        assert body["enabled"] is True
        assert body["prefix"] == "A-"
        assert body["next_value"] == 1125
        # The preview is what the next create will really receive.
        assert body["preview"] == "A-01125"

        listing = await async_client.get("/api/v1/number-series/")
        assert listing.status_code == 200
        assert any(s["key"] == SERIES_PROJECT and s["preview"] == "A-01125" for s in listing.json())

    @pytest.mark.asyncio
    async def test_an_unknown_series_is_a_404(self, async_client: AsyncClient):
        response = await async_client.patch("/api/v1/number-series/nope", json={"enabled": True})
        assert response.status_code == 404

    @pytest.mark.asyncio
    async def test_a_combination_too_long_for_the_column_is_rejected(self, async_client: AsyncClient, db_session):
        await _seed_series(db_session, SERIES_PROJECT)

        response = await async_client.patch(
            f"/api/v1/number-series/{SERIES_PROJECT}",
            json={"prefix": "X" * 16, "suffix": "Y" * 16},
        )

        assert response.status_code == 400


class TestProjectNumbers:
    @pytest.mark.asyncio
    async def test_a_created_project_gets_the_next_number(self, async_client: AsyncClient, db_session):
        await _seed_series(db_session, SERIES_PROJECT, prefix="P-", padding=4, next_value=7)

        response = await async_client.post("/api/v1/projects/", json={"name": "Bracket run"})

        assert response.status_code == 200
        assert response.json()["number"] == "P-0007"
        assert await _next_value(db_session, SERIES_PROJECT) == 8

    @pytest.mark.asyncio
    async def test_a_disabled_series_leaves_the_project_unnumbered(self, async_client: AsyncClient, db_session):
        await _seed_series(db_session, SERIES_PROJECT, enabled=False)

        response = await async_client.post("/api/v1/projects/", json={"name": "Bracket run"})

        assert response.status_code == 200
        assert response.json()["number"] is None

    @pytest.mark.asyncio
    async def test_a_number_the_caller_typed_wins_and_the_counter_stays_put(
        self, async_client: AsyncClient, db_session
    ):
        await _seed_series(db_session, SERIES_PROJECT, prefix="P-", padding=4, next_value=7)

        response = await async_client.post("/api/v1/projects/", json={"name": "Bracket run", "number": "RMA-9"})

        assert response.status_code == 200
        assert response.json()["number"] == "RMA-9"
        assert await _next_value(db_session, SERIES_PROJECT) == 7

    @pytest.mark.asyncio
    async def test_a_duplicate_number_answers_409_not_500(self, async_client: AsyncClient):
        first = await async_client.post("/api/v1/projects/", json={"name": "First", "number": "A-1"})
        assert first.status_code == 200

        clash = await async_client.post("/api/v1/projects/", json={"name": "Second", "number": "A-1"})

        assert clash.status_code == 409
        assert "A-1" in clash.json()["detail"]

    @pytest.mark.asyncio
    async def test_editing_a_project_onto_a_taken_number_answers_409(self, async_client: AsyncClient):
        await async_client.post("/api/v1/projects/", json={"name": "First", "number": "A-1"})
        second = await async_client.post("/api/v1/projects/", json={"name": "Second", "number": "A-2"})

        clash = await async_client.patch(f"/api/v1/projects/{second.json()['id']}", json={"number": "A-1"})

        assert clash.status_code == 409

    @pytest.mark.asyncio
    async def test_a_project_keeps_its_own_number_on_an_unrelated_edit(self, async_client: AsyncClient):
        created = await async_client.post("/api/v1/projects/", json={"name": "First", "number": "A-1"})

        renamed = await async_client.patch(f"/api/v1/projects/{created.json()['id']}", json={"name": "Renamed"})

        assert renamed.status_code == 200
        assert renamed.json()["number"] == "A-1"

    @pytest.mark.asyncio
    async def test_the_number_is_editable_and_can_be_cleared(self, async_client: AsyncClient):
        created = await async_client.post("/api/v1/projects/", json={"name": "First", "number": "A-1"})
        project_id = created.json()["id"]

        edited = await async_client.patch(f"/api/v1/projects/{project_id}", json={"number": "B-2"})
        assert edited.json()["number"] == "B-2"

        cleared = await async_client.patch(f"/api/v1/projects/{project_id}", json={"number": ""})
        assert cleared.json()["number"] is None

    @pytest.mark.asyncio
    async def test_nothing_is_renumbered_retroactively(self, async_client: AsyncClient, db_session):
        """A project that predates the series keeps its NULL; turning the series
        on only affects what is created next."""
        db_session.add(Project(name="Older than the feature"))
        await db_session.commit()
        await _seed_series(db_session, SERIES_PROJECT, prefix="P-", next_value=1)

        listing = await async_client.get("/api/v1/projects/")

        older = next(p for p in listing.json() if p["name"] == "Older than the feature")
        assert older["number"] is None

    @pytest.mark.asyncio
    async def test_the_list_and_detail_response_maps_carry_the_number(self, async_client: AsyncClient, db_session):
        await _seed_series(db_session, SERIES_PROJECT, prefix="P-", padding=3, next_value=1)
        created = await async_client.post("/api/v1/projects/", json={"name": "Bracket run"})
        project_id = created.json()["id"]

        detail = await async_client.get(f"/api/v1/projects/{project_id}")
        listing = await async_client.get("/api/v1/projects/")

        assert detail.json()["number"] == "P-001"
        assert next(p for p in listing.json() if p["id"] == project_id)["number"] == "P-001"

    @pytest.mark.asyncio
    async def test_a_project_from_a_template_gets_its_own_number(self, async_client: AsyncClient, db_session):
        await _seed_series(db_session, SERIES_PROJECT, prefix="P-", padding=3, next_value=1)
        source = await async_client.post("/api/v1/projects/", json={"name": "Source"})
        template = await async_client.post(f"/api/v1/projects/{source.json()['id']}/create-template")
        # A template is a shape to start from, not a job — it carries no number.
        assert template.json()["number"] is None

        made = await async_client.post(f"/api/v1/projects/from-template/{template.json()['id']}")

        assert made.status_code == 200
        assert made.json()["number"] == "P-002"

    @pytest.mark.asyncio
    async def test_a_number_already_on_a_project_is_stepped_over(self, async_client: AsyncClient, db_session):
        """The create takes the next free number and keeps the counter past the
        taken one.

        Answering 409 instead would roll the advance back with the failed
        create, so the next create would be handed the same taken number and
        fail the same way — project creation would stay dead until an admin
        raised the start number by hand.
        """
        await _seed_series(db_session, SERIES_PROJECT, prefix="P-", padding=3, next_value=4)
        typed = await async_client.post("/api/v1/projects/", json={"name": "By hand", "number": "P-004"})
        assert typed.status_code == 200

        created = await async_client.post("/api/v1/projects/", json={"name": "Auto"})

        assert created.status_code == 200
        assert created.json()["number"] == "P-005"
        assert await _next_value(db_session, SERIES_PROJECT) == 6

    @pytest.mark.asyncio
    async def test_a_whole_block_of_legacy_numbers_is_stepped_over(self, async_client: AsyncClient, db_session):
        """A farm types its old numbers onto existing projects, then switches
        the series on at the start of that block."""
        await _seed_series(db_session, SERIES_PROJECT, enabled=False)
        for n in range(4, 9):
            typed = await async_client.post("/api/v1/projects/", json={"name": f"Legacy {n}", "number": f"P-{n:03d}"})
            assert typed.status_code == 200
        await _seed_series(db_session, SERIES_PROJECT, prefix="P-", padding=3, next_value=4)

        created = await async_client.post("/api/v1/projects/", json={"name": "First new one"})

        assert created.json()["number"] == "P-009"
        assert await _next_value(db_session, SERIES_PROJECT) == 10

    @pytest.mark.asyncio
    async def test_a_project_from_a_template_steps_over_a_taken_number(self, async_client: AsyncClient, db_session):
        await _seed_series(db_session, SERIES_PROJECT, prefix="P-", padding=3, next_value=1)
        source = await async_client.post("/api/v1/projects/", json={"name": "Source", "number": "P-002"})
        template = await async_client.post(f"/api/v1/projects/{source.json()['id']}/create-template")

        made = await async_client.post(f"/api/v1/projects/from-template/{template.json()['id']}")

        assert made.status_code == 200
        assert made.json()["number"] == "P-001"
        second = await async_client.post(f"/api/v1/projects/from-template/{template.json()['id']}")
        assert second.json()["number"] == "P-003"

    @pytest.mark.asyncio
    async def test_a_run_of_taken_numbers_longer_than_the_step_limit_says_which_series_is_stuck(
        self, async_client: AsyncClient, db_session, monkeypatch
    ):
        """Stepping is bounded — a create has to answer. The 409 names the
        series rather than a number the caller never sent."""
        monkeypatch.setattr("backend.app.services.number_series.MAX_COLLISION_SKIPS", 2)
        await _seed_series(db_session, SERIES_PROJECT, enabled=False)
        for n in range(1, 6):
            await async_client.post("/api/v1/projects/", json={"name": f"Legacy {n}", "number": f"P-{n:03d}"})
        await _seed_series(db_session, SERIES_PROJECT, prefix="P-", padding=3, next_value=1)

        rejected = await async_client.post("/api/v1/projects/", json={"name": "No room"})

        assert rejected.status_code == 409
        assert "series" in rejected.json()["detail"]
        # Every number it stepped over came back with the rejected create, so
        # giving the series room by hand is all the admin has to do.
        assert await _next_value(db_session, SERIES_PROJECT) == 1

    @pytest.mark.asyncio
    async def test_a_create_that_fails_after_the_number_was_taken_gives_it_back(
        self, async_client: AsyncClient, db_session, monkeypatch
    ):
        """The counter advance rides in the create's own transaction, so a
        create that dies once the number is in hand has to give it back.

        The failure is forced at the flush because that is the real one: the
        unique index is what actually guards the column, and two creates that
        both passed the pre-check meet there. Rejecting before the allocator
        runs -- an unknown parent, a bad payload -- proves nothing about this,
        because the allocator was never reached.
        """
        from fastapi import HTTPException

        await _seed_series(db_session, SERIES_PROJECT, prefix="P-", padding=3, next_value=4)

        async def _the_index_says_no(db, number):
            raise HTTPException(status_code=409, detail=f"Project number '{number}' is already in use")

        monkeypatch.setattr("backend.app.api.routes.projects._flush_project_number", _the_index_says_no)

        rejected = await async_client.post("/api/v1/projects/", json={"name": "Loser"})

        assert rejected.status_code == 409
        assert "P-004" in rejected.json()["detail"], "the create got as far as taking a number"
        monkeypatch.undo()
        assert await _next_value(db_session, SERIES_PROJECT) == 4
        # And the number that was almost handed out is still the next one.
        created = await async_client.post("/api/v1/projects/", json={"name": "Next"})
        assert created.json()["number"] == "P-004"

    @pytest.mark.asyncio
    async def test_a_create_rejected_before_the_allocator_never_reaches_the_counter(
        self, async_client: AsyncClient, db_session
    ):
        """The parent lookup runs first, so a bad parent costs nothing at all."""
        await _seed_series(db_session, SERIES_PROJECT, prefix="P-", padding=3, next_value=4)

        rejected = await async_client.post("/api/v1/projects/", json={"name": "Orphan", "parent_id": 999_999})

        assert rejected.status_code == 400
        assert await _next_value(db_session, SERIES_PROJECT) == 4


class TestQueueJobNumbers:
    @pytest.fixture
    async def archive(self, db_session):
        from backend.app.models.archive import PrintArchive

        archive = PrintArchive(
            filename="benchy.gcode.3mf",
            print_name="Benchy",
            file_path="/tmp/benchy.gcode.3mf",
            file_size=1024,
            status="completed",
        )
        db_session.add(archive)
        await db_session.commit()
        await db_session.refresh(archive)
        return archive

    @pytest.mark.asyncio
    async def test_a_queued_job_gets_a_number_from_its_own_series(self, async_client: AsyncClient, db_session, archive):
        await _seed_series(db_session, SERIES_QUEUE_JOB, prefix="J", padding=4, next_value=12)

        response = await async_client.post("/api/v1/queue/", json={"archive_id": archive.id})

        assert response.status_code == 200
        assert response.json()["job_number"] == "J0012"

    @pytest.mark.asyncio
    async def test_every_copy_of_a_multi_quantity_add_gets_its_own_number(
        self, async_client: AsyncClient, db_session, archive
    ):
        await _seed_series(db_session, SERIES_QUEUE_JOB, prefix="J", padding=4, next_value=1)

        await async_client.post("/api/v1/queue/", json={"archive_id": archive.id, "quantity": 3})

        listing = await async_client.get("/api/v1/queue/")
        numbers = [item["job_number"] for item in listing.json()]
        assert sorted(numbers) == ["J0001", "J0002", "J0003"]

    @pytest.mark.asyncio
    async def test_a_disabled_series_leaves_the_job_unnumbered(self, async_client: AsyncClient, db_session, archive):
        await _seed_series(db_session, SERIES_QUEUE_JOB, enabled=False)

        response = await async_client.post("/api/v1/queue/", json={"archive_id": archive.id})

        assert response.status_code == 200
        assert response.json()["job_number"] is None

    @pytest.mark.asyncio
    async def test_the_archive_response_map_carries_the_job_number(
        self, async_client: AsyncClient, db_session, archive
    ):
        archive.job_number = "J0042"
        await db_session.commit()

        response = await async_client.get(f"/api/v1/archives/{archive.id}")

        assert response.status_code == 200
        assert response.json()["job_number"] == "J0042"

    @pytest.mark.asyncio
    async def test_every_run_of_one_archive_reaches_the_log_under_its_own_number(
        self, async_client: AsyncClient, db_session, archive
    ):
        """A quantity>1 order is several runs of one archive, each with its own
        number. The archive can only carry one of them, so reading the log's
        number back off the archive filed every copy under the first copy's
        number — a run invoiced as J0003 displayed and searched as J0001.
        """
        from backend.app.services.print_log import write_log_entry

        await _seed_series(db_session, SERIES_QUEUE_JOB, prefix="J", padding=4, next_value=1)
        await async_client.post("/api/v1/queue/", json={"archive_id": archive.id, "quantity": 3})
        queued = sorted((await async_client.get("/api/v1/queue/")).json(), key=lambda i: i["job_number"])

        for item in queued:
            await write_log_entry(
                db_session,
                status="completed",
                archive_id=archive.id,
                queue_item_id=item["id"],
                print_name="Benchy",
            )
        await db_session.commit()

        listed = (
            await async_client.get("/api/v1/print-log/", params={"sort_by": "job_number", "sort_dir": "asc"})
        ).json()
        assert [row["job_number"] for row in listed["items"]] == ["J0001", "J0002", "J0003"]

    @pytest.mark.asyncio
    async def test_a_later_run_is_not_relabelled_with_the_first_runs_number(
        self, async_client: AsyncClient, db_session, archive
    ):
        """A reprint reuses the source archive row on purpose (#730), so the
        archive keeps the number it first printed under while the reprint's own
        number is the one the log has to show."""
        from backend.app.models.print_queue import PrintQueueItem
        from backend.app.services.print_log import write_log_entry

        archive.job_number = "J0001"
        reprint = PrintQueueItem(archive_id=archive.id, status="pending", job_number="J0099")
        db_session.add(reprint)
        await db_session.commit()

        await write_log_entry(
            db_session,
            status="completed",
            archive_id=archive.id,
            queue_item_id=reprint.id,
            print_name="Benchy",
        )
        await db_session.commit()

        response = await async_client.get("/api/v1/print-log/")

        assert response.json()["items"][0]["job_number"] == "J0099"

    @pytest.mark.asyncio
    async def test_the_number_outlives_the_queue_row_and_the_archive(
        self, async_client: AsyncClient, db_session, archive
    ):
        """Both rows it was read from are deleted in the normal course of
        things; the number is what the paperwork is filed under, so it has to
        survive them."""
        from backend.app.models.archive import PrintArchive
        from backend.app.models.print_queue import PrintQueueItem
        from backend.app.services.print_log import write_log_entry

        item = PrintQueueItem(archive_id=archive.id, status="pending", job_number="J0007")
        db_session.add(item)
        await db_session.commit()
        await write_log_entry(
            db_session,
            status="completed",
            archive_id=archive.id,
            queue_item_id=item.id,
            print_name="Benchy",
        )
        await db_session.commit()

        await db_session.delete(await db_session.get(PrintQueueItem, item.id))
        await db_session.delete(await db_session.get(PrintArchive, archive.id))
        await db_session.commit()

        response = await async_client.get("/api/v1/print-log/")

        assert [row["job_number"] for row in response.json()["items"]] == ["J0007"]

    @pytest.mark.asyncio
    async def test_the_print_log_can_be_searched_and_sorted_by_job_number(
        self, async_client: AsyncClient, db_session, archive
    ):
        from backend.app.models.print_log import PrintLogEntry

        db_session.add(
            PrintLogEntry(archive_id=archive.id, print_name="Benchy", status="completed", job_number="J0042")
        )
        db_session.add(PrintLogEntry(archive_id=None, print_name="Something else", status="completed"))
        await db_session.commit()

        found = await async_client.get("/api/v1/print-log/", params={"search": "J0042"})
        assert [item["print_name"] for item in found.json()["items"]] == ["Benchy"]
        assert found.json()["total"] == 1

        sorted_rows = await async_client.get("/api/v1/print-log/", params={"sort_by": "job_number"})
        assert sorted_rows.status_code == 200

    @pytest.mark.asyncio
    async def test_a_printer_started_run_has_no_number_to_show(self, async_client: AsyncClient, db_session, archive):
        """No queue row, no job -- and nothing to borrow from the archive."""
        from backend.app.services.print_log import write_log_entry

        archive.job_number = "J0001"
        await db_session.commit()
        await write_log_entry(db_session, status="completed", archive_id=archive.id, print_name="Benchy")
        await db_session.commit()

        response = await async_client.get("/api/v1/print-log/")

        assert response.json()["items"][0]["job_number"] is None
