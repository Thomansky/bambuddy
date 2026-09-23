"""Post-print outcome confirmation helpers (#1898)."""

import logging
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.models.archive import PrintArchive
from backend.app.models.print_log import PrintLogEntry

logger = logging.getLogger(__name__)

# How a verdict reached the archive. 'reaction' is written by the Telegram
# reaction handler (#3046), which lives on its own branch — listed here so the
# vocabulary is complete and the UI can label it the day that lands.
VERDICT_SOURCES = ("dialog", "link", "plate_clear", "printer_card", "api", "reaction")


def stamp_verdict(archive: PrintArchive, source: str) -> None:
    """Record a verdict's provenance and the moment it landed (#1898).

    Both fields move together on every verdict write, which is what keeps the
    "already answered" page from pairing a new source with the timestamp of an
    older decision. `retire_confirm_token` is separate on purpose: spending the
    one-tap capability happens once, recording a verdict can happen again.
    """
    archive.user_verdict_source = source
    archive.user_verdict_at = datetime.now(timezone.utc)


def retire_confirm_token(archive: PrintArchive) -> None:
    """Spend the one-tap capability token without destroying it.

    The token is still single-use: once ``confirm_token_used_at`` is stamped,
    no verdict path accepts it again. Keeping the VALUE is what lets the
    one-tap route recognise a link belonging to an already-answered print and
    say so, instead of 404ing as if the link had never been real (the live-farm
    case: the plate-clear default answered the prompt, then the user tapped the
    Telegram button and got "invalid or already used").
    """
    if archive.confirm_token and archive.confirm_token_used_at is None:
        archive.confirm_token_used_at = datetime.now(timezone.utc)


VERDICTS = ("good", "reject")


async def apply_outcome_verdict(
    db: AsyncSession,
    archive: PrintArchive,
    verdict: str,
    *,
    source: str,
    reason: str | None = None,
) -> bool:
    """Record a verdict on an archive that has not been answered yet.

    The one place every unattended verdict path goes through — the one-tap
    capability link, the plate-clear default and the Telegram reaction
    poller (#3046) — so they agree on what a verdict entails: the archive's
    user_verdict, the same value mirrored onto the latest PrintLogEntry
    (verdict-aware statistics read the log, the #1444 mirror), and the
    capability token retired so the push-notification links stop working.

    First verdict wins. An archive that already carries one is left exactly
    as it is and False is returned, so a late reaction or a second tap on an
    old link cannot flip a decision somebody made in the meantime. The
    Edit Archive modal's PATCH route is deliberately not routed through
    here — it is the explicit way to change a verdict afterwards.

    ``source`` says which path recorded it (#1898) and is stamped together
    with the time the verdict landed; the capability token is spent rather
    than deleted, so a later tap on the same link can be told what happened
    instead of being called invalid.

    ``reason`` is an optional failure_reason for a reject. Deliberately does
    NOT commit — callers manage their own transaction.
    """
    if source not in VERDICT_SOURCES:
        raise ValueError(f"Source must be one of {VERDICT_SOURCES}, got {source!r}")
    if verdict not in VERDICTS:
        raise ValueError(f"Verdict must be one of {VERDICTS}, got {verdict!r}")
    if archive.user_verdict is not None:
        return False

    archive.user_verdict = verdict
    stamp_verdict(archive, source)
    retire_confirm_token(archive)
    if reason is not None and verdict == "reject":
        archive.failure_reason = reason

    latest_entry = await db.scalar(
        select(PrintLogEntry).where(PrintLogEntry.archive_id == archive.id).order_by(PrintLogEntry.id.desc()).limit(1)
    )
    if latest_entry is not None:
        latest_entry.user_verdict = verdict
        if reason is not None and verdict == "reject":
            latest_entry.failure_reason = reason
    return True


async def resolve_pending_confirmation_as_good(db: AsyncSession, printer_id: int) -> int | None:
    """Mark the printer's latest pending-confirmation archive as good.

    Backs the opt-in ``confirm_default_good_on_plate_clear`` setting: releasing
    the build plate is the moment the operator moves on to the next job, so an
    unanswered outcome prompt can default to "good part" right there instead of
    lingering as unconfirmed. Only the LATEST pending archive is resolved — the
    plate release refers to the print that just came off the plate, not to
    older unanswered prompts.

    Deliberately does NOT commit — both callers (the clear-plate route and the
    queue dispatcher) manage their own transaction.

    Returns the resolved archive id, or None when nothing was pending.
    """
    archive = await db.scalar(
        select(PrintArchive)
        .where(
            PrintArchive.printer_id == printer_id,
            PrintArchive.status == "completed",
            PrintArchive.confirm_requested.is_(True),
            PrintArchive.user_verdict.is_(None),
        )
        .order_by(PrintArchive.id.desc())
        .limit(1)
    )
    if archive is None:
        return None

    await apply_outcome_verdict(db, archive, "good", source="plate_clear")

    logger.info("[#1898] Plate clear defaulted archive %s to 'good' (printer %s)", archive.id, printer_id)
    return archive.id


async def confirm_outcome_for_new_queue_item(db: AsyncSession, *, started_outside_bambuddy: bool = False) -> bool:
    """The ask-for-outcome flag for a queue item created without the print dialog.

    The dialog seeds its own per-job toggle from ``default_confirm_outcome``.
    Every other queue-creation path -- the virtual printer, the library bulk
    add, the webhook, a pipeline run -- has no toggle to seed and used to leave
    the column at its ``False`` default, so "Ask for Outcome" only ever reached
    jobs queued by hand.

    ``started_outside_bambuddy`` additionally honours
    ``confirm_outcome_external_prints``: a plate sent from Bambu Studio to a
    virtual printer is one of the prints that setting's description names, but
    it arrives with a queue item, so ``on_print_start`` never sees it as
    external and the setting could not otherwise reach it.
    """
    from backend.app.api.routes.settings import get_setting, setting_is_true

    if setting_is_true(await get_setting(db, "default_confirm_outcome")):
        return True
    return started_outside_bambuddy and setting_is_true(await get_setting(db, "confirm_outcome_external_prints"))
