"""Tests for change queue and dedup logic."""

import json
import pytest
import pytest_asyncio
import fakeredis.aioredis

from src.batcher import Batcher


@pytest_asyncio.fixture
async def batcher():
    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    b = Batcher(redis)
    yield b
    await redis.aclose()


@pytest.mark.asyncio
class TestEnqueue:
    async def test_enqueue_single_change(self, batcher):
        changes = [{"action": "DELETE", "name": "api.g1.example.com.", "type": "A", "ttl": 300, "values": ["10.0.1.5"]}]
        await batcher.enqueue("Z123", changes, "BATCH-001")
        queued = await batcher.drain("Z123")
        assert len(queued) == 1
        assert queued[0]["action"] == "DELETE"

    async def test_enqueue_multiple_changes(self, batcher):
        changes = [
            {"action": "DELETE", "name": "api.g1.example.com.", "type": "A", "values": ["10.0.1.1"]},
            {"action": "DELETE", "name": "apps.g1.example.com.", "type": "A", "values": ["10.0.1.2"]},
        ]
        await batcher.enqueue("Z123", changes, "BATCH-002")
        queued = await batcher.drain("Z123")
        assert len(queued) == 2

    async def test_enqueue_tracks_change_ids(self, batcher):
        changes = [{"action": "DELETE", "name": "api.g1.example.com.", "type": "A", "values": ["10.0.1.1"]}]
        await batcher.enqueue("Z123", changes, "BATCH-003")
        queued = await batcher.drain("Z123")
        assert queued[0]["_change_id"] == "BATCH-003"

    async def test_drain_empties_queue(self, batcher):
        changes = [{"action": "DELETE", "name": "api.g1.example.com.", "type": "A", "values": ["10.0.1.1"]}]
        await batcher.enqueue("Z123", changes, "BATCH-004")
        await batcher.drain("Z123")
        second = await batcher.drain("Z123")
        assert len(second) == 0

    async def test_multiple_zones_independent(self, batcher):
        await batcher.enqueue("Z1", [{"action": "DELETE", "name": "a.example.com.", "type": "A", "values": ["1.1.1.1"]}], "B1")
        await batcher.enqueue("Z2", [{"action": "DELETE", "name": "b.example.com.", "type": "A", "values": ["2.2.2.2"]}], "B2")
        z1 = await batcher.drain("Z1")
        z2 = await batcher.drain("Z2")
        assert len(z1) == 1 and len(z2) == 1
        assert z1[0]["name"] == "a.example.com."
        assert z2[0]["name"] == "b.example.com."


@pytest.mark.asyncio
class TestDedup:
    async def test_collapse_exact_duplicates(self, batcher):
        change = {"action": "DELETE", "name": "api.g1.example.com.", "type": "A", "values": ["10.0.1.1"]}
        await batcher.enqueue("Z1", [change], "B1")
        await batcher.enqueue("Z1", [change], "B2")
        queued = await batcher.drain("Z1")
        deduped = batcher.dedup(queued)
        assert len(deduped) == 1

    async def test_preserve_different_actions_same_record(self, batcher):
        delete = {"action": "DELETE", "name": "api.g1.example.com.", "type": "A", "values": ["10.0.1.1"]}
        create = {"action": "CREATE", "name": "api.g1.example.com.", "type": "A", "values": ["10.0.1.2"]}
        await batcher.enqueue("Z1", [delete], "B1")
        await batcher.enqueue("Z1", [create], "B2")
        queued = await batcher.drain("Z1")
        deduped = batcher.dedup(queued)
        assert len(deduped) == 2
        assert deduped[0]["action"] == "DELETE"
        assert deduped[1]["action"] == "CREATE"

    async def test_preserve_order(self, batcher):
        c1 = {"action": "DELETE", "name": "a.example.com.", "type": "A", "values": ["1.1.1.1"]}
        c2 = {"action": "DELETE", "name": "b.example.com.", "type": "A", "values": ["2.2.2.2"]}
        c3 = {"action": "CREATE", "name": "c.example.com.", "type": "A", "values": ["3.3.3.3"]}
        await batcher.enqueue("Z1", [c1, c2, c3], "B1")
        queued = await batcher.drain("Z1")
        deduped = batcher.dedup(queued)
        assert [c["name"] for c in deduped] == ["a.example.com.", "b.example.com.", "c.example.com."]


@pytest.mark.asyncio
class TestRequeue:
    async def test_requeue_on_failure(self, batcher):
        changes = [{"action": "DELETE", "name": "a.example.com.", "type": "A", "values": ["1.1.1.1"], "_change_id": "B1"}]
        await batcher.requeue("Z1", changes)
        queued = await batcher.drain("Z1")
        assert len(queued) == 1


@pytest.mark.asyncio
class TestActiveZones:
    async def test_lists_zones_with_pending_changes(self, batcher):
        await batcher.enqueue("Z1", [{"action": "DELETE", "name": "a.example.com.", "type": "A", "values": ["1.1.1.1"]}], "B1")
        await batcher.enqueue("Z2", [{"action": "DELETE", "name": "b.example.com.", "type": "A", "values": ["2.2.2.2"]}], "B2")
        zones = await batcher.active_zones()
        assert set(zones) == {"Z1", "Z2"}

    async def test_empty_when_no_changes(self, batcher):
        zones = await batcher.active_zones()
        assert zones == []


@pytest.mark.asyncio
class TestQueueDepth:
    async def test_returns_count(self, batcher):
        await batcher.enqueue("Z1", [
            {"action": "DELETE", "name": "a.example.com.", "type": "A", "values": ["1.1.1.1"]},
            {"action": "DELETE", "name": "b.example.com.", "type": "A", "values": ["2.2.2.2"]},
        ], "B1")
        depth = await batcher.queue_depth("Z1")
        assert depth == 2
