"""Tests for flush worker."""

import pytest
import pytest_asyncio
import fakeredis.aioredis
from unittest.mock import MagicMock
from src.batcher import Batcher
from src.flusher import Flusher


@pytest_asyncio.fixture
async def redis_client():
    client = fakeredis.aioredis.FakeRedis(decode_responses=True)
    yield client
    await client.aclose()


@pytest_asyncio.fixture
async def batcher(redis_client):
    return Batcher(redis_client)


@pytest_asyncio.fixture
async def flusher(redis_client, batcher):
    mock_r53 = MagicMock()
    mock_r53.change_resource_record_sets = MagicMock(
        return_value={"Id": "/change/REAL123", "Status": "PENDING"}
    )
    mock_r53.changes_to_boto3 = MagicMock(return_value={"Changes": []})
    f = Flusher(redis_client, batcher, mock_r53)
    return f


@pytest.mark.asyncio
class TestFlushOnce:
    async def test_flushes_queued_changes(self, flusher, batcher):
        await batcher.enqueue("Z1", [{"action": "DELETE", "name": "a.example.com.", "type": "A", "values": ["1.1.1.1"]}], "B1")
        await flusher.flush_once()
        flusher.r53_client.change_resource_record_sets.assert_called_once()
        assert flusher.r53_client.change_resource_record_sets.call_args[0][0] == "Z1"

    async def test_no_call_when_empty(self, flusher):
        await flusher.flush_once()
        flusher.r53_client.change_resource_record_sets.assert_not_called()

    async def test_deduplicates_before_flush(self, flusher, batcher):
        change = {"action": "DELETE", "name": "a.example.com.", "type": "A", "values": ["1.1.1.1"]}
        await batcher.enqueue("Z1", [change], "B1")
        await batcher.enqueue("Z1", [change], "B2")
        await flusher.flush_once()
        flusher.r53_client.changes_to_boto3.assert_called_once()
        assert len(flusher.r53_client.changes_to_boto3.call_args[0][0]) == 1

    async def test_maps_change_ids_after_flush(self, flusher, batcher):
        await batcher.enqueue("Z1", [{"action": "DELETE", "name": "a.example.com.", "type": "A", "values": ["1.1.1.1"]}], "BATCH-syn1")
        await flusher.flush_once()
        real_id = await batcher.get_real_change_id("BATCH-syn1")
        assert real_id == "/change/REAL123"

    async def test_requeues_on_failure(self, flusher, batcher):
        from botocore.exceptions import ClientError
        flusher.r53_client.change_resource_record_sets.side_effect = ClientError(
            {"Error": {"Code": "Throttling", "Message": "Rate exceeded"}}, "ChangeResourceRecordSets",
        )
        await batcher.enqueue("Z1", [{"action": "DELETE", "name": "a.example.com.", "type": "A", "values": ["1.1.1.1"]}], "B1")
        await flusher.flush_once()
        depth = await batcher.queue_depth("Z1")
        assert depth == 1

    async def test_skips_backed_off_zone(self, flusher, batcher):
        await batcher.enqueue("Z1", [{"action": "DELETE", "name": "a.example.com.", "type": "A", "values": ["1.1.1.1"]}], "B1")
        await batcher.set_backoff("Z1", 60)
        await flusher.flush_once()
        flusher.r53_client.change_resource_record_sets.assert_not_called()

    async def test_flushes_multiple_zones(self, flusher, batcher):
        await batcher.enqueue("Z1", [{"action": "DELETE", "name": "a.example.com.", "type": "A", "values": ["1.1.1.1"]}], "B1")
        await batcher.enqueue("Z2", [{"action": "DELETE", "name": "b.example.com.", "type": "A", "values": ["2.2.2.2"]}], "B2")
        await flusher.flush_once()
        assert flusher.r53_client.change_resource_record_sets.call_count == 2


@pytest.mark.asyncio
class TestLeaderElection:
    async def test_acquires_lock(self, flusher):
        acquired = await flusher.try_acquire_leader()
        assert acquired is True

    async def test_lock_prevents_second_leader(self, flusher, redis_client):
        await flusher.try_acquire_leader()
        flusher2 = Flusher(redis_client, Batcher(redis_client), MagicMock())
        acquired = await flusher2.try_acquire_leader()
        assert acquired is False
