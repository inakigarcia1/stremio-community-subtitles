import asyncio
import json
import logging
import time
from types import SimpleNamespace

import aiohttp
from quart import Quart
from sqlalchemy import select

import app.extensions as extensions
from app.extensions import init_async_db
from app.models import Base, User
from app.providers.base import ProviderDownloadError
from app.providers.opensubtitles.provider import OpenSubtitlesProvider

DEAD_TOKEN = "os-bearer-dead-1b6d0c33a9e84f12"
NEW_TOKEN = "os-bearer-new-7c1e9a44b0d24f6a"
API_KEY = "os-api-key-55aa19c0e7b34d28"
ENV_USERNAME = "subtitle-user"
ENV_PASSWORD = "os-password-env-e3c91a77b5d24f60"
STORED_PASSWORD = "os-password-stored-88aa11c2d4e64b70"
DOWNLOAD_LINK = "https://cdn.example/sub.vtt"
BASE_URL = "vip-api.opensubtitles.com"


class _CollectHandler(logging.Handler):
    def __init__(self):
        super().__init__()
        self.messages = []

    def emit(self, record):
        self.messages.append(record.getMessage())


class _User:
    def __init__(self, token_timestamp):
        self.id = 7
        self.provider_credentials = {
            "opensubtitles": {
                "token": DEAD_TOKEN,
                "base_url": BASE_URL,
                "active": True,
                "username": ENV_USERNAME,
                "password": STORED_PASSWORD,
                "token_timestamp": token_timestamp,
            }
        }


def _error_body(token):
    return json.dumps(
        {
            "message": "Cannot download subtitle",
            "token": token,
            "Api-Key": API_KEY,
            "Authorization": f"Bearer {token}",
            "password": ENV_PASSWORD,
        }
    )


class _Response:
    def __init__(self, status, payload=None, text_body=""):
        self.status = status
        self._payload = payload
        self._text = text_body
        self.reason = {200: "OK", 401: "Unauthorized", 403: "Forbidden", 406: "Not Acceptable"}.get(
            status, "Error"
        )

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def text(self):
        return self._text

    async def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status >= 400:
            raise aiohttp.ClientResponseError(
                request_info=SimpleNamespace(real_url=f"https://{BASE_URL}/api/v1/download"),
                history=(),
                status=self.status,
                message=self.reason,
            )


def _install_http(monkeypatch, user, download_statuses, login_status=200):
    calls = []
    downloads_done = {"n": 0}

    class _Session:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        def post(self, url, headers=None, json=None, timeout=None):
            creds = user.provider_credentials["opensubtitles"]
            calls.append(
                {
                    "url": url,
                    "headers": dict(headers or {}),
                    "json": json,
                    "stored_token": creds.get("token"),
                }
            )
            if url.endswith("/login"):
                if login_status != 200:
                    return _Response(login_status, text_body=_error_body(DEAD_TOKEN))
                return _Response(
                    200,
                    {
                        "token": NEW_TOKEN,
                        "base_url": BASE_URL,
                        "user": {"username": ENV_USERNAME},
                    },
                )
            if url.endswith("/download"):
                index = downloads_done["n"]
                downloads_done["n"] += 1
                status = download_statuses[index]
                token_used = (headers or {}).get("Authorization", "").removeprefix("Bearer ").strip()
                if status == 200:
                    return _Response(200, {"link": DOWNLOAD_LINK})
                return _Response(status, text_body=_error_body(token_used))
            raise AssertionError(f"unexpected url {url}")

    monkeypatch.setattr(
        "app.providers.opensubtitles.client.aiohttp.ClientSession",
        _Session,
    )
    return calls


def _kinds(calls):
    kinds = []
    for call in calls:
        if call["url"].endswith("/login"):
            kinds.append("login")
        elif call["url"].endswith("/download"):
            kinds.append("download")
    return kinds


async def _download(monkeypatch, user, download_statuses, login_status=200):
    monkeypatch.setenv("OPENSUBTITLES_USERNAME", ENV_USERNAME)
    monkeypatch.setenv("OPENSUBTITLES_PASSWORD", ENV_PASSWORD)
    monkeypatch.setenv("OPENSUBTITLES_API_KEY", API_KEY)

    calls = _install_http(monkeypatch, user, download_statuses, login_status=login_status)
    app = Quart("opensubtitles-406-test")
    app.config["OPENSUBTITLES_API_KEY"] = API_KEY
    handler = _CollectHandler()
    provider = OpenSubtitlesProvider()

    async with app.app_context():
        app.logger.setLevel(logging.INFO)
        app.logger.addHandler(handler)
        try:
            try:
                result = await provider.get_download_url(user, "4242")
                error = None
            except ProviderDownloadError as exc:
                result = None
                error = exc
        finally:
            app.logger.removeHandler(handler)

    return result, error, calls, "\n".join(handler.messages)


def _assert_log_has_no_secrets(logged):
    for secret in (DEAD_TOKEN, NEW_TOKEN, API_KEY, ENV_PASSWORD, STORED_PASSWORD):
        assert secret not in logged


def test_download_406_relogin_once_and_redacts_body(monkeypatch):
    user = _User(token_timestamp=int(time.time()))
    result, error, calls, logged = asyncio.run(
        _download(monkeypatch, user, [406, 200])
    )

    assert error is None
    assert result == DOWNLOAD_LINK
    assert _kinds(calls) == ["download", "login", "download"]

    login = calls[1]
    assert login["stored_token"] is None
    assert login["json"]["username"] == ENV_USERNAME
    assert login["json"]["password"] == ENV_PASSWORD
    assert login["headers"]["Api-Key"] == API_KEY

    assert calls[0]["headers"]["Authorization"] == f"Bearer {DEAD_TOKEN}"
    assert calls[0]["json"] == {"file_id": 4242, "sub_format": "webvtt"}
    assert calls[2]["headers"]["Authorization"] == f"Bearer {NEW_TOKEN}"
    assert user.provider_credentials["opensubtitles"]["token"] == NEW_TOKEN

    assert "Cannot download subtitle" in logged
    assert "[REDACTED]" in logged
    _assert_log_has_no_secrets(logged)


def test_download_406_retry_does_not_login_again(monkeypatch):
    user = _User(token_timestamp=int(time.time()))
    result, error, calls, logged = asyncio.run(
        _download(monkeypatch, user, [406, 406])
    )

    assert result is None
    assert error is not None
    assert error.status_code == 406
    assert _kinds(calls) == ["download", "login", "download"]
    assert calls[2]["headers"]["Authorization"] == f"Bearer {NEW_TOKEN}"
    assert "Cannot download subtitle" in logged
    _assert_log_has_no_secrets(logged)


def test_download_401_still_refreshes_with_stored_password(monkeypatch):
    user = _User(token_timestamp=int(time.time()))
    result, error, calls, logged = asyncio.run(
        _download(monkeypatch, user, [401, 200])
    )

    assert error is None
    assert result == DOWNLOAD_LINK
    assert _kinds(calls) == ["download", "login", "download"]
    assert calls[1]["stored_token"] == DEAD_TOKEN
    assert calls[1]["json"]["password"] == STORED_PASSWORD
    _assert_log_has_no_secrets(logged)


def test_failed_relogin_keeps_the_previous_bearer(monkeypatch):
    user = _User(token_timestamp=int(time.time()))
    result, error, calls, logged = asyncio.run(
        _download(monkeypatch, user, [406], login_status=401)
    )

    assert result is None
    assert error is not None
    assert error.status_code == 406
    assert _kinds(calls) == ["download", "login"]
    assert user.provider_credentials["opensubtitles"]["token"] == DEAD_TOKEN
    assert "Cannot download subtitle" in logged
    _assert_log_has_no_secrets(logged)


def test_download_406_replaces_bearer_in_the_database(monkeypatch, tmp_path):
    asyncio.run(_assert_bearer_replaced_in_database(monkeypatch, tmp_path))


async def _assert_bearer_replaced_in_database(monkeypatch, tmp_path):
    app = Quart("opensubtitles-406-persist")
    app.config["SQLALCHEMY_DATABASE_URI"] = f"sqlite:///{tmp_path / 'subs.db'}"
    app.config["SQLALCHEMY_ECHO"] = False
    app.config["OPENSUBTITLES_API_KEY"] = API_KEY

    previous_maker = extensions.async_session_maker
    previous_engine = extensions.async_engine
    engine = None
    try:
        engine, maker = init_async_db(app)
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

        async with maker() as session:
            session.add(
                User(
                    username="apachiy",
                    email="apachiy@local",
                    password_hash="not-used",
                    active=True,
                    provider_credentials={
                        "opensubtitles": {
                            "token": DEAD_TOKEN,
                            "base_url": BASE_URL,
                            "active": True,
                            "username": ENV_USERNAME,
                            "password": STORED_PASSWORD,
                            "token_timestamp": int(time.time()),
                        }
                    },
                )
            )
            await session.commit()

        async with maker() as session:
            user = (await session.execute(select(User).filter_by(username="apachiy"))).scalar_one()

        result, error, calls, logged = await _download(monkeypatch, user, [406, 200])

        assert error is None
        assert result == DOWNLOAD_LINK
        assert _kinds(calls) == ["download", "login", "download"]
        _assert_log_has_no_secrets(logged)

        async with maker() as session:
            stored = (await session.execute(select(User).filter_by(username="apachiy"))).scalar_one()
            creds = stored.provider_credentials["opensubtitles"]
            assert creds["token"] == NEW_TOKEN
            assert creds["password"] == ENV_PASSWORD
            assert DEAD_TOKEN not in json.dumps(creds)
    finally:
        if engine is not None:
            await engine.dispose()
        extensions.async_session_maker = previous_maker
        extensions.async_engine = previous_engine


def test_old_token_still_refreshes_before_download(monkeypatch):
    user = _User(token_timestamp=int(time.time()) - (47 * 3600))
    result, error, calls, logged = asyncio.run(
        _download(monkeypatch, user, [200])
    )

    assert error is None
    assert result == DOWNLOAD_LINK
    assert _kinds(calls) == ["login", "download"]
    assert calls[0]["json"]["password"] == STORED_PASSWORD
    assert calls[1]["headers"]["Authorization"] == f"Bearer {NEW_TOKEN}"
    _assert_log_has_no_secrets(logged)
