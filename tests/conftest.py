"""Shared test fixtures."""

import pytest
import fakeredis.aioredis

@pytest.fixture
async def redis_client():
    client = fakeredis.aioredis.FakeRedis(decode_responses=True)
    yield client
    await client.aclose()
