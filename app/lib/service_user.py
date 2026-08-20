"""Internal service user for tokenless Apachiy API access."""
from sqlalchemy import select
from ..extensions import async_session_maker
from ..models import User

SERVICE_USERNAME = "apachiy"


async def get_service_user() -> User | None:
    async with async_session_maker() as session:
        result = await session.execute(
            select(User).filter_by(username=SERVICE_USERNAME).limit(1)
        )
        return result.scalar_one_or_none()
