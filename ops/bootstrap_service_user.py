#!/usr/bin/env python3
"""Create or update the internal Apachiy service user and provider credentials."""
import asyncio
import os
import secrets

from sqlalchemy import insert, select
from sqlalchemy.orm import selectinload

from app import create_app
from app.models import User, Role, roles_users


SERVICE_USERNAME = "apachiy"
SERVICE_EMAIL = "apachiy@local"
DEFAULT_LANGS = ["spa"]


def _env(name: str) -> str:
    return (os.environ.get(name) or "").strip()


async def bootstrap():
    from app.extensions import async_session_maker

    async with async_session_maker() as session:
        result = await session.execute(
            select(User)
            .filter_by(username=SERVICE_USERNAME)
            .options(selectinload(User.roles))
        )
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
            creds["subdl"] = {"api_key": subdl_key, "active": True}

        subsource_key = _env("SUBSOURCE_API_KEY")
        if subsource_key:
            creds["subsource"] = {"api_key": subsource_key, "active": True}

        os_api_key = _env("OPENSUBTITLES_API_KEY")
        os_user = _env("OPENSUBTITLES_USERNAME")
        os_pass = _env("OPENSUBTITLES_PASSWORD")
        if os_user and os_pass:
            from app.providers.registry import ProviderRegistry

            os_provider = ProviderRegistry.get("opensubtitles")
            if os_provider:
                try:
                    creds["opensubtitles"] = await os_provider.authenticate(
                        user,
                        {"username": os_user, "password": os_pass},
                    )
                except Exception as exc:
                    print(f"Warning: OpenSubtitles bootstrap auth failed: {exc}")
            elif os_api_key:
                creds["opensubtitles"] = {
                    "api_key": os_api_key,
                    "username": os_user,
                    "password": os_pass,
                    "active": True,
                }
        elif os_api_key:
            # API key alone is read from app config at request time; keep a marker cred.
            creds["opensubtitles"] = {"api_key": os_api_key}

        user.provider_credentials = creds

        role_result = await session.execute(select(Role).filter_by(name="User"))
        role = role_result.scalar_one_or_none()
        if role:
            existing_link = await session.execute(
                select(roles_users).filter_by(user_id=user.id, role_id=role.id)
            )
            if existing_link.first() is None:
                await session.execute(
                    insert(roles_users).values(user_id=user.id, role_id=role.id)
                )

        await session.commit()
        print(f"Service user '{SERVICE_USERNAME}' ready (manifest_token={user.manifest_token})")


if __name__ == "__main__":
    create_app()
    asyncio.run(bootstrap())
