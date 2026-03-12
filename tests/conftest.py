"""Shared test fixtures."""

import fakeredis.aioredis
import pytest

from src.config import config

# Set test credentials so auth checks pass
config.AWS_ACCESS_KEY_ID = "TESTKEY"
config.AWS_SECRET_ACCESS_KEY = "TESTSECRET"


@pytest.fixture
async def redis_client():
    client = fakeredis.aioredis.FakeRedis(decode_responses=True)
    yield client
    await client.aclose()
