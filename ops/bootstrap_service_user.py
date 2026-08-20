#!/usr/bin/env python3
"""Create or update the internal Apachiy service user and provider credentials."""
import asyncio
import os
import secrets

from sqlalchemy import select

from app.extensions import async_session_maker
from app.models import User, Role


SERVICE_USERNAME = "apachiy"
SERVICE_EMAIL = "apachiy@local"
DEFAULT_LANGS = ["spa"]


def _env(name: str) -> str:
    return (os.environ.get(name) or "").strip()


async def bootstrap():
    async with async_session_maker() as session:
        result = await session.execute(select(User).filter_by(username=SERVICE_USERNAME))
        user = result.scalar_one_or_none()

        if not user:
            user = User(
                username=SERVICE_USERNAME,
                email=SERVICE_EMAIL,
                active=True,
                email_confirmed=True,
                preferred_languages=list(DEFAULT_LANGS),
            )
            user.set_password(secrets.token_urlsafe(32))
            user.generate_manifest_token()
            session.add(user)
            await session.flush()
        else:
            if not user.preferred_languages:
                user.preferred_languages = list(DEFAULT_LANGS)
            if not user.manifest_token:
                user.generate_manifest_token()

        creds = dict(user.provider_credentials or {})

        subdl_key = _env("SUBDL_API_KEY")
        if subdl_key:
            creds["subdl"] = {"api_key": subdl_key}

        subsource_key = _env("SUBSOURCE_API_KEY")
        if subsource_key:
            creds["subsource"] = {"api_key": subsource_key}

        os_api_key = _env("OPENSUBTITLES_API_KEY")
        os_user = _env("OPENSUBTITLES_USERNAME")
        os_pass = _env("OPENSUBTITLES_PASSWORD")
        if os_api_key or (os_user and os_pass):
            os_creds = creds.get("opensubtitles", {})
            if os_api_key:
                os_creds["api_key"] = os_api_key
            if os_user:
                os_creds["username"] = os_user
            if os_pass:
                os_creds["password"] = os_pass
            creds["opensubtitles"] = os_creds

        user.provider_credentials = creds

        role_result = await session.execute(select(Role).filter_by(name="User"))
        role = role_result.scalar_one_or_none()
        if role and role not in user.roles:
            user.roles.append(role)

        await session.commit()
        print(f"Service user '{SERVICE_USERNAME}' ready (manifest_token={user.manifest_token})")


if __name__ == "__main__":
    asyncio.run(bootstrap())
