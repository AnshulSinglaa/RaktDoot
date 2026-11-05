import asyncio

import pytest
from sqlalchemy.exc import IntegrityError

from db import make_engine, make_sessionmaker
from repository import DonorRepository


def test_repository_only_translates_the_email_violation(settings, fresh_db):
    async def go():
        engine = make_engine(settings.database_url, 1, 0)
        try:
            with pytest.raises(IntegrityError):         # the CHECK age >= 18, NOT the email index
                async with make_sessionmaker(engine)() as session, session.begin():
                    await DonorRepository(session).add(
                        {"name": "Kid", "email": "kid@example.com", "age": 5, "blood_group": "O+"}
                    )
        finally:
            await engine.dispose()

    asyncio.run(go())
