"""Change queue management and dedup logic backed by Redis."""

import json
import logging
import redis.asyncio as aioredis

logger = logging.getLogger(__name__)

ZONE_KEY_PREFIX = "zone:"
ZONE_SET_KEY = "zones:active"


class Batcher:
    def __init__(self, redis: aioredis.Redis):
        self.redis = redis

    async def enqueue(self, zone_id: str, changes: list[dict], change_id: str) -> None:
        pipe = self.redis.pipeline()
        for change in changes:
            change["_change_id"] = change_id
            pipe.rpush(f"{ZONE_KEY_PREFIX}{zone_id}:changes", json.dumps(change))
        pipe.sadd(ZONE_SET_KEY, zone_id)
        await pipe.execute()

    async def drain(self, zone_id: str) -> list[dict]:
        key = f"{ZONE_KEY_PREFIX}{zone_id}:changes"
        pipe = self.redis.pipeline()
        pipe.lrange(key, 0, -1)
        pipe.delete(key)
        results = await pipe.execute()
        raw_items = results[0]
        if not raw_items:
            await self.redis.srem(ZONE_SET_KEY, zone_id)
            return []
        return [json.loads(item) for item in raw_items]

    async def requeue(self, zone_id: str, changes: list[dict]) -> None:
        key = f"{ZONE_KEY_PREFIX}{zone_id}:changes"
        pipe = self.redis.pipeline()
        for change in reversed(changes):
            pipe.lpush(key, json.dumps(change))
        pipe.sadd(ZONE_SET_KEY, zone_id)
        await pipe.execute()

    async def active_zones(self) -> list[str]:
        members = await self.redis.smembers(ZONE_SET_KEY)
        return list(members)

    async def queue_depth(self, zone_id: str) -> int:
        return await self.redis.llen(f"{ZONE_KEY_PREFIX}{zone_id}:changes")

    @staticmethod
    def dedup(changes: list[dict]) -> list[dict]:
        seen = set()
        result = []
        for change in changes:
            key_parts = (
                change.get("action"),
                change.get("name"),
                change.get("type"),
                tuple(change.get("values", []) or []),
                json.dumps(change.get("alias_target"), sort_keys=True) if change.get("alias_target") else None,
            )
            if key_parts not in seen:
                seen.add(key_parts)
                result.append(change)
        return result

    async def map_change_id(self, synthetic_id: str, real_id: str, ttl: int = 3600) -> None:
        await self.redis.set(f"change:{synthetic_id}", real_id, ex=ttl)

    async def get_real_change_id(self, synthetic_id: str) -> str | None:
        return await self.redis.get(f"change:{synthetic_id}")

    async def get_fail_count(self, zone_id: str) -> int:
        val = await self.redis.get(f"{ZONE_KEY_PREFIX}{zone_id}:fail_count")
        return int(val) if val else 0

    async def incr_fail_count(self, zone_id: str) -> int:
        return await self.redis.incr(f"{ZONE_KEY_PREFIX}{zone_id}:fail_count")

    async def reset_fail_count(self, zone_id: str) -> None:
        await self.redis.delete(f"{ZONE_KEY_PREFIX}{zone_id}:fail_count")

    async def set_backoff(self, zone_id: str, seconds: float) -> None:
        await self.redis.set(f"{ZONE_KEY_PREFIX}{zone_id}:backoff", "1", ex=int(seconds))

    async def is_backed_off(self, zone_id: str) -> bool:
        return await self.redis.exists(f"{ZONE_KEY_PREFIX}{zone_id}:backoff") > 0
