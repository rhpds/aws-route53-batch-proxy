"""Flush worker — leader election, batch sending to Route53."""

import asyncio
import logging
import time

from botocore.exceptions import ClientError

from src.batcher import Batcher
from src.config import config
from src.metrics import CHANGES_FLUSHED, FLUSH_BATCH_SIZE, FLUSH_DURATION, FLUSH_ERRORS, QUEUE_DEPTH
from src.route53_client import Route53Client

logger = logging.getLogger(__name__)
LEADER_KEY = "flush:leader"


class Flusher:
    def __init__(self, redis, batcher: Batcher, r53_client: Route53Client):
        self.redis = redis
        self.batcher = batcher
        self.r53_client = r53_client
        self._running = False
        self._task = None

    async def try_acquire_leader(self):
        result = await self.redis.set(LEADER_KEY, "1", nx=True, ex=config.LEADER_LOCK_TTL)
        return result is not None and result is not False

    async def renew_leader(self):
        return await self.redis.expire(LEADER_KEY, config.LEADER_LOCK_TTL)

    async def flush_once(self):
        zones = await self.batcher.active_zones()
        for zone_id in zones:
            if await self.batcher.is_backed_off(zone_id):
                logger.debug("Zone %s is in backoff, skipping", zone_id)
                continue
            await self._flush_zone(zone_id)

    async def _flush_zone(self, zone_id):
        changes = await self.batcher.drain(zone_id)
        if not changes:
            return
        deduped = self.batcher.dedup(changes)
        change_ids = set(c.get("_change_id", "") for c in deduped if c.get("_change_id"))
        clean = [{k: v for k, v in c.items() if not k.startswith("_")} for c in deduped]

        for i in range(0, len(clean), config.MAX_BATCH_SIZE):
            batch = clean[i : i + config.MAX_BATCH_SIZE]
            batch_changes = deduped[i : i + config.MAX_BATCH_SIZE]
            FLUSH_BATCH_SIZE.observe(len(batch))
            start = time.monotonic()
            try:
                boto3_batch = self.r53_client.changes_to_boto3(batch)
                result = self.r53_client.change_resource_record_sets(zone_id, boto3_batch)
                duration = time.monotonic() - start
                FLUSH_DURATION.observe(duration)
                CHANGES_FLUSHED.labels(zone_id=zone_id).inc(len(batch))
                real_id = result.get("Id", "")
                for cid in change_ids:
                    await self.batcher.map_change_id(cid, real_id)
                await self.batcher.reset_fail_count(zone_id)
                logger.info(
                    "Flushed %d changes for zone %s in %.2fs (real_id=%s)", len(batch), zone_id, duration, real_id
                )
            except ClientError as e:
                duration = time.monotonic() - start
                FLUSH_DURATION.observe(duration)
                FLUSH_ERRORS.labels(zone_id=zone_id).inc()
                fail_count = await self.batcher.incr_fail_count(zone_id)
                logger.error("Flush failed for zone %s (attempt %d): %s", zone_id, fail_count, e)
                await self.batcher.requeue(zone_id, batch_changes)
                if fail_count >= config.MAX_FLUSH_FAILURES:
                    logger.error(
                        "Zone %s hit %d consecutive failures, backing off %ds",
                        zone_id,
                        fail_count,
                        config.FLUSH_BACKOFF_SECONDS,
                    )
                    await self.batcher.set_backoff(zone_id, config.FLUSH_BACKOFF_SECONDS)
                    await self.batcher.reset_fail_count(zone_id)
                break
        depth = await self.batcher.queue_depth(zone_id)
        QUEUE_DEPTH.labels(zone_id=zone_id).set(depth)

    async def run(self):
        self._running = True
        logger.info("Flush worker starting")
        while self._running:
            try:
                is_leader = await self.try_acquire_leader()
                if is_leader:
                    await self.renew_leader()
                    await self.flush_once()
                else:
                    logger.debug("Not the flush leader, sleeping")
            except Exception:
                logger.exception("Error in flush loop")
            await asyncio.sleep(config.FLUSH_INTERVAL_SECONDS)

    def start(self):
        self._task = asyncio.create_task(self.run())

    async def stop(self):
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
