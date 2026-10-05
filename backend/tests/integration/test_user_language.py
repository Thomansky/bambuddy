"""PUT /api/v1/users/me/language -- the UI language saved to the account.

The language picker used to write only the browser's storage (and, for whoever
may change settings, the server-wide language). Signing in on a new device
meant picking the language again. Saved to the account, it follows the user:
/auth/me and the login response carry it and the SPA switches to it.

These tests pin that it is the user's own preference (no settings permission,
no effect on other users or the server-wide language), that the code is
checked, and that an API key gets a 403 instead of the 401 the SPA would read
as a dead session.
"""

import pytest
from httpx import AsyncClient
from sqlalchemy import select

from backend.app.core.auth import generate_api_key
from backend.app.models.api_key import APIKey
from backend.app.models.user import User

ADMIN = {"username": "langadmin", "password": "LangAdmin1!"}
USER = {"username": "languser", "password": "LangUser1!"}


async def _login(async_client: AsyncClient, account: dict) -> str:
    response = await async_client.post("/api/v1/auth/login", json=account)
    assert response.status_code == 200, response.text
    return response.json()["access_token"]


async def _setup(async_client: AsyncClient) -> tuple[str, str]:
    """Enable auth and return (admin token, token of a user in no group)."""
    await async_client.post(
        "/api/v1/auth/setup",
        json={"auth_enabled": True, "admin_username": ADMIN["username"], "admin_password": ADMIN["password"]},
    )
    admin_token = await _login(async_client, ADMIN)
    # No groups: the user holds no permission at all, settings:update included.
    created = await async_client.post(
        "/api/v1/users/",
        headers={"Authorization": f"Bearer {admin_token}"},
        json={"username": USER["username"], "password": USER["password"]},
    )
    assert created.status_code == 201, created.text
    assert created.json()["permissions"] == []
    return admin_token, await _login(async_client, USER)


async def _put(async_client: AsyncClient, token: str | None, body: dict):
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    return await async_client.put("/api/v1/users/me/language", headers=headers, json=body)


@pytest.mark.asyncio
@pytest.mark.integration
async def test_saved_language_comes_back_on_me_and_at_login(async_client: AsyncClient):
    _, user_token = await _setup(async_client)

    response = await _put(async_client, user_token, {"language": "de"})

    assert response.status_code == 200, response.text
    assert response.json()["language"] == "de"
    me = await async_client.get("/api/v1/auth/me", headers={"Authorization": f"Bearer {user_token}"})
    assert me.json()["language"] == "de"
    # A new device signs in and learns the language with the user.
    login = await async_client.post("/api/v1/auth/login", json=USER)
    assert login.json()["user"]["language"] == "de"


@pytest.mark.asyncio
@pytest.mark.integration
async def test_a_user_without_a_choice_reports_none(async_client: AsyncClient):
    _, user_token = await _setup(async_client)

    me = await async_client.get("/api/v1/auth/me", headers={"Authorization": f"Bearer {user_token}"})

    assert me.json()["language"] is None


@pytest.mark.asyncio
@pytest.mark.integration
async def test_region_codes_are_kept(async_client: AsyncClient):
    _, user_token = await _setup(async_client)

    response = await _put(async_client, user_token, {"language": "pt-BR"})

    assert response.status_code == 200, response.text
    assert response.json()["language"] == "pt-BR"


@pytest.mark.asyncio
@pytest.mark.integration
async def test_null_goes_back_to_following_the_device(async_client: AsyncClient):
    _, user_token = await _setup(async_client)
    await _put(async_client, user_token, {"language": "de"})

    response = await _put(async_client, user_token, {"language": None})

    assert response.status_code == 200, response.text
    assert response.json()["language"] is None


@pytest.mark.asyncio
@pytest.mark.integration
async def test_an_empty_body_does_not_clear_the_choice(async_client: AsyncClient, db_session):
    _, user_token = await _setup(async_client)
    await _put(async_client, user_token, {"language": "de"})

    response = await _put(async_client, user_token, {})

    assert response.status_code == 422
    user = (await db_session.execute(select(User).where(User.username == USER["username"]))).scalar_one()
    assert user.language == "de"


@pytest.mark.asyncio
@pytest.mark.integration
@pytest.mark.parametrize("code", ["german", "de_DE", "DE", "<b>de</b>", "zh-Hant-TW-x", ""])
async def test_malformed_codes_are_refused(async_client: AsyncClient, code: str):
    _, user_token = await _setup(async_client)

    response = await _put(async_client, user_token, {"language": code})

    assert response.status_code == 422


@pytest.mark.asyncio
@pytest.mark.integration
async def test_it_is_the_users_own_choice_only(async_client: AsyncClient):
    """Neither the admin's account nor the server-wide language changes."""
    admin_token, user_token = await _setup(async_client)
    before = (await async_client.get("/api/v1/settings/", headers={"Authorization": f"Bearer {admin_token}"})).json()

    await _put(async_client, user_token, {"language": "sv"})

    admin_me = await async_client.get("/api/v1/auth/me", headers={"Authorization": f"Bearer {admin_token}"})
    assert admin_me.json()["language"] is None
    after = (await async_client.get("/api/v1/settings/", headers={"Authorization": f"Bearer {admin_token}"})).json()
    assert after["language"] == before["language"]


@pytest.mark.asyncio
@pytest.mark.integration
async def test_requires_a_signed_in_user(async_client: AsyncClient):
    await _setup(async_client)

    response = await _put(async_client, None, {"language": "de"})

    assert response.status_code == 401


@pytest.mark.asyncio
@pytest.mark.integration
@pytest.mark.parametrize("header", ["bearer", "x-api-key"])
async def test_an_api_key_gets_403_not_the_401_that_ends_a_session(async_client: AsyncClient, db_session, header):
    """A kiosk signed in with a key must keep its key when someone picks a language."""
    await _setup(async_client)
    owner = (await db_session.execute(select(User).where(User.username == ADMIN["username"]))).scalar_one()
    full_key, key_hash, key_prefix = generate_api_key()
    db_session.add(
        APIKey(
            name="kiosk",
            key_hash=key_hash,
            key_prefix=key_prefix,
            enabled=True,
            user_id=owner.id,
            can_read_status=True,
        )
    )
    await db_session.commit()

    if header == "bearer":
        response = await _put(async_client, full_key, {"language": "de"})
    else:
        response = await async_client.put(
            "/api/v1/users/me/language", headers={"X-API-Key": full_key}, json={"language": "de"}
        )

    assert response.status_code == 403
    await db_session.refresh(owner)
    assert owner.language is None
