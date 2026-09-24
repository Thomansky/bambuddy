"""A machine must not be able to spend a one-tap verdict token (#1898).

Both verdict URLs go out as plain text in the notification body for every
channel -- that is what ``DEFAULT_TEMPLATES['print_confirm_request']`` ships.
Telegram fetches the first URL in a message to build a preview card, mail
gateways detonate links before delivery, and browsers prefetch. Any one of
those GETs would record a verdict nobody chose and retire the token, so the
operator's real tap lands on "already answered" and a scrap part is counted as
good for the rest of time.

Two defences, tested here: the prompt goes out without a preview at all, and
the route recognises an unattended request and offers the choice instead of
taking it.
"""

import httpx
import pytest

from backend.app.models.notification_template import DEFAULT_TEMPLATES
from backend.app.services.notification_service import NotificationService
from backend.app.services.print_confirmation import is_unattended_fetch

CONFIG = {"bot_token": "123456:AAbbCC", "chat_id": "-1002520100736"}

# Real link-preview and mail-security fetchers, plus the browsers that must
# keep working. The phone UAs are the ones a notification tap actually opens.
BROWSER_AGENTS = [
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_5 like Mac OS X) AppleWebKit/605.1.15 "
    "(KHTML, like Gecko) Version/17.5 Mobile/15E148 Safari/604.1",
    "Mozilla/5.0 (Linux; Android 14; Pixel 8) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/126.0.0.0 Mobile Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/126.0.0.0 Safari/537.36 Edg/126.0.0.0",
    # The ntfy app performs its action buttons itself, with an HTTP client UA.
    "okhttp/4.12.0",
    "python-httpx/0.27.0",
]

MACHINE_AGENTS = [
    "TelegramBot (like TwitterBot)",
    "Mozilla/5.0 (compatible; Discordbot/2.0; +https://discordapp.com)",
    "Slackbot-LinkExpanding 1.0 (+https://api.slack.com/robots)",
    "facebookexternalhit/1.1",
    "Twitterbot/1.0",
    "WhatsApp/2.23.20.0 A",
    "Mozilla/5.0 (compatible; SkypeUriPreview Preview/0.5)",
    "Mozilla/5.0 (compatible; bingbot/2.0; +http://www.bing.com/bingbot.htm)",
    "Barracuda Sentinel (EE)",
    "Mimecast-Link-Protect",
]


class _Client:
    """Stand-in for httpx.AsyncClient that records what would be sent."""

    def __init__(self):
        self.is_closed = False
        self.calls: list[dict] = []

    async def post(self, url, data=None, files=None, json=None):
        self.calls.append(json if json is not None else (data or {}))
        return httpx.Response(200, json={"ok": True, "result": {}})


class TestUnattendedFetchDetection:
    @pytest.mark.parametrize("agent", BROWSER_AGENTS)
    def test_a_real_browser_is_left_alone(self, agent):
        assert is_unattended_fetch("GET", {"user-agent": agent}) is False

    @pytest.mark.parametrize("agent", MACHINE_AGENTS)
    def test_link_fetchers_are_recognised(self, agent):
        assert is_unattended_fetch("GET", {"user-agent": agent}) is True

    def test_prefetch_hints_count_even_from_a_browser_ua(self):
        """Chrome and Firefox preload links before they are clicked; Safari's
        preview sends the same hint. A finger on a button never does."""
        browser = BROWSER_AGENTS[1]
        assert is_unattended_fetch("GET", {"user-agent": browser, "purpose": "prefetch"}) is True
        assert is_unattended_fetch("GET", {"user-agent": browser, "x-purpose": "preview"}) is True
        assert is_unattended_fetch("GET", {"user-agent": browser, "x-moz": "prefetch"}) is True
        assert is_unattended_fetch("GET", {"user-agent": browser, "sec-purpose": "prefetch;prerender"}) is True

    def test_head_is_never_a_tap(self):
        """The route only lists GET, so FastAPI 405s a HEAD today (pinned in
        the integration tests). This keeps that true if HEAD is ever added:
        a scanner probing the link must not answer the prompt by arriving."""
        assert is_unattended_fetch("HEAD", {"user-agent": BROWSER_AGENTS[0]}) is True

    def test_a_missing_user_agent_is_not_held_against_the_caller(self):
        """Some notification clients send none; the token is still the
        credential, and guessing here would cost real verdicts."""
        assert is_unattended_fetch("GET", {}) is False


class TestPromptCarriesTheLinksInBodyText:
    def test_the_default_template_still_puts_both_urls_in_the_body(self):
        """The premise of this whole file. If this ever stops being true the
        guard is still correct, but the urgency changes — so pin it."""
        template = next(t for t in DEFAULT_TEMPLATES if t["event_type"] == "print_confirm_request")
        assert "{good_url}" in template["body_template"]
        assert "{reject_url}" in template["body_template"]


class TestTelegramDoesNotAskForAPreview:
    @pytest.mark.asyncio
    async def test_send_message_disables_the_link_preview(self):
        """Telegram's servers GET the first URL in the text to build the
        preview card. That fetch is the one that answered the prompt."""
        service = NotificationService()
        client = _Client()
        service._http_client = client

        ok, _ = await service._send_telegram(
            CONFIG,
            "*How did your print come out?*\nX1C: bracket.3mf\nGood: https://host/api/v1/archives/confirm/tok/good",
        )

        assert ok
        assert client.calls[0]["disable_web_page_preview"] is True

    @pytest.mark.asyncio
    async def test_the_inline_buttons_variant_disables_it_too(self):
        """The shape the outcome prompt actually sends on a camera-less
        printer: no photo, so sendMessage rather than sendPhoto."""
        service = NotificationService()
        client = _Client()
        service._http_client = client

        buttons = [
            {"text": "Good", "url": "https://host/api/v1/archives/confirm/tok/good"},
            {"text": "Reject", "url": "https://host/api/v1/archives/confirm/tok/reject"},
        ]
        ok, _ = await service._send_telegram(CONFIG, "*T*\nbody", buttons=buttons)

        assert ok
        assert client.calls[0]["disable_web_page_preview"] is True
        assert client.calls[0]["reply_markup"] == {"inline_keyboard": [buttons]}
