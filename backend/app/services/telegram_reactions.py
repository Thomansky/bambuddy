"""Answer the post-print outcome prompt with a Telegram reaction (#3046).

Reactions arrive as ``message_reaction`` updates, which the Bot API only
hands out through ``getUpdates`` (long polling) or a webhook. Bambuddy
polls: no inbound connectivity, no public URL, no certificate — the same
outbound-only footing every other notification provider works from. One
task per enabled Telegram provider whose verdict mode is not "buttons";
the notifications routes resync the set whenever a provider changes.

Reactions set by bots are never delivered by Telegram, and in groups the
bot has to be an administrator to receive them at all — both documented Bot
API behaviour, nothing to work around here.
"""

import asyncio
import json
import logging
import time
from datetime import timedelta

import httpx
from sqlalchemy import delete, select

from backend.app.models.archive import PrintArchive
from backend.app.models.notification import NotificationProvider, TelegramPendingVerdict
from backend.app.services.notification_service import _USER_AGENT, telegram_markdown_escape
from backend.app.services.print_confirmation import apply_outcome_verdict
from backend.app.utils.local_time import utcnow_naive

logger = logging.getLogger(__name__)

# Telegram holds a getUpdates call open for up to this long before answering
# with an empty list. The HTTP read timeout below has to outlast it.
GET_UPDATES_TIMEOUT = 50
# A 409 means a webhook is set for the bot or a second poller is running.
# Neither clears by retrying, so the loop backs off well beyond the poll
# interval instead of hammering the API every few seconds.
CONFLICT_COOLDOWN = 300
MAX_BACKOFF = 60
PENDING_TTL = timedelta(days=7)
PRUNE_INTERVAL = 3600

REACTION_VERDICTS = {"\U0001f44d": "good", "\U0001f44e": "reject"}
VERDICT_SUFFIX = {"good": "✅ marked as good", "reject": "❌ marked as reject"}


def verdict_from_reaction(new_reaction: list) -> str | None:
    """Map the reaction list of a message_reaction update to a verdict.

    Only plain emoji reactions count; custom emoji and paid reactions are
    ignored. The first thumbs in the list wins when several are set.
    """
    for reaction in new_reaction or []:
        if not isinstance(reaction, dict) or reaction.get("type") != "emoji":
            continue
        verdict = REACTION_VERDICTS.get(reaction.get("emoji", ""))
        if verdict:
            return verdict
    return None


def _provider_reaction_token(provider: NotificationProvider) -> str | None:
    """Bot token of a provider that should be polled, else None."""
    if provider.provider_type != "telegram" or not provider.enabled:
        return None
    if (provider.telegram_verdict_mode or "buttons") == "buttons":
        return None
    config = json.loads(provider.config) if isinstance(provider.config, str) else (provider.config or {})
    token = str(config.get("bot_token") or "").strip()
    return token or None


class TelegramReactionPoller:
    def __init__(self):
        self._tasks: dict[int, asyncio.Task] = {}
        # provider id -> bot token the running task was started with, so a
        # token change on edit restarts the poll instead of keeping the old
        # bot's long-poll open.
        self._tokens: dict[int, str] = {}
        # provider id -> last update_id confirmed. Kept across task restarts
        # within the process so a resync does not re-fetch handled updates.
        self._offsets: dict[int, int] = {}
        self._http_client: httpx.AsyncClient | None = None
        self._session_factory = None

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self):
        await self.sync()

    def stop(self):
        for task in self._tasks.values():
            task.cancel()
        if self._tasks:
            logger.info("Telegram reaction poller stopped (%d provider(s))", len(self._tasks))
        self._tasks.clear()
        self._tokens.clear()

    async def sync(self):
        """Reconcile the running tasks with the providers in the database.

        Called at startup and after every provider create/update/delete.
        Starts a task for each provider that now wants reactions, restarts
        one whose bot token changed, and cancels those that no longer
        qualify (deleted, disabled, or switched back to buttons).
        """
        async with self._sessions()() as db:
            result = await db.execute(
                select(NotificationProvider).where(NotificationProvider.provider_type == "telegram")
            )
            providers = list(result.scalars().all())

        wanted: dict[int, str] = {}
        for provider in providers:
            token = _provider_reaction_token(provider)
            if token:
                wanted[provider.id] = token

        for provider_id in list(self._tasks):
            if wanted.get(provider_id) != self._tokens.get(provider_id):
                self._tasks.pop(provider_id).cancel()
                self._tokens.pop(provider_id, None)
                logger.info("Telegram reaction poll stopped for provider %s", provider_id)

        for provider_id, token in wanted.items():
            if provider_id not in self._tasks:
                self._tasks[provider_id] = asyncio.create_task(
                    self._run(provider_id, token), name=f"telegram-reactions-{provider_id}"
                )
                self._tokens[provider_id] = token
                logger.info("Telegram reaction poll started for provider %s", provider_id)

    def is_polling(self, provider_id: int) -> bool:
        return provider_id in self._tasks

    # ------------------------------------------------------------------
    # Plumbing
    # ------------------------------------------------------------------

    def _sessions(self):
        if self._session_factory is not None:
            return self._session_factory
        from backend.app.core.database import async_session

        return async_session

    async def _get_client(self) -> httpx.AsyncClient:
        if self._http_client is None or self._http_client.is_closed:
            self._http_client = httpx.AsyncClient(
                timeout=httpx.Timeout(GET_UPDATES_TIMEOUT + 10.0, connect=5.0),
                headers={"User-Agent": _USER_AGENT},
            )
        return self._http_client

    async def _sleep(self, seconds: float):
        await asyncio.sleep(seconds)

    async def _run(self, provider_id: int, bot_token: str):
        backoff = 1.0
        last_prune: float | None = None
        while True:
            try:
                if last_prune is None or time.monotonic() - last_prune > PRUNE_INTERVAL:
                    await self.prune_stale()
                    last_prune = time.monotonic()
                status = await self.poll_once(provider_id, bot_token)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.warning("Telegram reaction poll failed for provider %s: %s", provider_id, e)
                await self._sleep(backoff)
                backoff = min(backoff * 2, MAX_BACKOFF)
                continue
            backoff = 1.0
            if status == "conflict":
                await self._sleep(CONFLICT_COOLDOWN)

    # ------------------------------------------------------------------
    # One poll
    # ------------------------------------------------------------------

    async def poll_once(self, provider_id: int, bot_token: str) -> str:
        """One getUpdates round trip. Returns "ok" or "conflict"."""
        params: dict = {"timeout": GET_UPDATES_TIMEOUT, "allowed_updates": ["message_reaction"]}
        offset = self._offsets.get(provider_id)
        if offset is not None:
            params["offset"] = offset + 1

        client = await self._get_client()
        response = await client.post(f"https://api.telegram.org/bot{bot_token}/getUpdates", json=params)

        if response.status_code == 409:
            description = "conflict"
            try:
                description = response.json().get("description") or description
            except Exception:
                pass
            await self._record_conflict(provider_id, description)
            return "conflict"
        if response.status_code != 200:
            raise RuntimeError(f"getUpdates HTTP {response.status_code}")
        payload = response.json()
        if not payload.get("ok"):
            raise RuntimeError(f"getUpdates failed: {payload.get('description', 'unknown error')}")

        for update in payload.get("result") or []:
            update_id = update.get("update_id")
            if isinstance(update_id, int):
                self._offsets[provider_id] = max(self._offsets.get(provider_id, update_id), update_id)
            reaction = update.get("message_reaction")
            if not isinstance(reaction, dict):
                continue
            try:
                await self._handle_reaction(provider_id, bot_token, reaction)
            except Exception as e:
                logger.warning("Telegram reaction on provider %s could not be applied: %s", provider_id, e)
        return "ok"

    async def _record_conflict(self, provider_id: int, description: str):
        message = (
            f"Telegram getUpdates conflict: {description}. Reactions cannot be received while a webhook "
            f"is set for this bot or another poller is running; retrying in {CONFLICT_COOLDOWN // 60} minutes."
        )
        logger.error("Provider %s: %s", provider_id, message)
        try:
            async with self._sessions()() as db:
                provider = await db.get(NotificationProvider, provider_id)
                if provider is not None:
                    provider.last_error = message
                    provider.last_error_at = utcnow_naive()
                    await db.commit()
        except Exception as e:
            logger.warning("Could not record the Telegram conflict on provider %s: %s", provider_id, e)

    async def _handle_reaction(self, provider_id: int, bot_token: str, reaction: dict):
        chat_id = str((reaction.get("chat") or {}).get("id", "")).strip()
        message_id = reaction.get("message_id")
        verdict = verdict_from_reaction(reaction.get("new_reaction") or [])
        if not chat_id or not isinstance(message_id, int) or verdict is None:
            return

        async with self._sessions()() as db:
            pending = await db.scalar(
                select(TelegramPendingVerdict).where(
                    TelegramPendingVerdict.provider_id == provider_id,
                    TelegramPendingVerdict.chat_id == chat_id,
                    TelegramPendingVerdict.message_id == message_id,
                )
            )
            if pending is None:
                return

            archive = await db.get(PrintArchive, pending.archive_id)
            applied = archive is not None and await apply_outcome_verdict(db, archive, verdict)
            has_caption = bool(pending.has_caption)
            text = pending.message_text
            archive_id = pending.archive_id
            # Whatever happened to the archive, this message is answered.
            await db.delete(pending)
            await db.commit()

        if not applied:
            logger.info("Telegram reaction on archive %s ignored: verdict already recorded", archive_id)
            return
        logger.info("[#3046] Telegram reaction marked archive %s as '%s'", archive_id, verdict)
        await self._confirm_on_message(bot_token, chat_id, message_id, has_caption, text, verdict)

    async def _confirm_on_message(
        self, bot_token: str, chat_id: str, message_id: int, has_caption: bool, text: str | None, verdict: str
    ):
        """Append the verdict to the prompt so the chat shows it was taken.

        Editing also drops the inline keyboard in "both" mode — the links
        are dead once a verdict landed. Best effort: a failed edit is logged,
        the verdict itself is already committed.
        """
        suffix = VERDICT_SUFFIX[verdict]
        client = await self._get_client()
        try:
            if text:
                body = telegram_markdown_escape(f"{text}\n\n{suffix}")
                if has_caption:
                    method, field = "editMessageCaption", "caption"
                else:
                    method, field = "editMessageText", "text"
                payload = {"chat_id": chat_id, "message_id": message_id, field: body, "parse_mode": "Markdown"}
            else:
                method = "sendMessage"
                payload = {"chat_id": chat_id, "text": suffix, "reply_parameters": {"message_id": message_id}}
            response = await client.post(f"https://api.telegram.org/bot{bot_token}/{method}", json=payload)
            if response.status_code != 200 or not response.json().get("ok"):
                logger.warning("Telegram %s failed after reaction verdict: HTTP %s", method, response.status_code)
        except Exception as e:
            logger.warning("Telegram confirmation edit failed: %s", e)

    async def prune_stale(self):
        """Forget prompts nobody reacted to within a week."""
        async with self._sessions()() as db:
            await db.execute(
                delete(TelegramPendingVerdict).where(TelegramPendingVerdict.created_at < utcnow_naive() - PENDING_TTL)
            )
            await db.commit()


telegram_reaction_poller = TelegramReactionPoller()
