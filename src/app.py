"""FastAPI app, lifespan hooks, health and status endpoints."""

import logging
from contextlib import asynccontextmanager

import redis.asyncio as aioredis
from fastapi import FastAPI, Request, Response
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest

from src.batcher import Batcher
from src.config import config
from src.flusher import Flusher
from src.metrics import QUEUE_DEPTH
from src.proxy import Proxy
from src.route53_client import Route53Client

logging.basicConfig(
    level=config.LOG_LEVEL,
    format='{"time":"%(asctime)s","level":"%(levelname)s","logger":"%(name)s","message":"%(message)s"}',
)
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("Starting Route53 Batch Proxy")
    redis_client = aioredis.from_url(config.REDIS_URL, decode_responses=True)
    batcher = Batcher(redis_client)
    r53_client = Route53Client()
    flusher = Flusher(redis_client, batcher, r53_client)
    proxy = Proxy(batcher, r53_client)
    app.state.redis = redis_client
    app.state.batcher = batcher
    app.state.flusher = flusher
    app.state.proxy = proxy
    flusher.start()
    logger.info("Flush worker started")
    yield
    logger.info("Shutting down")
    await flusher.stop()
    await redis_client.aclose()


app = FastAPI(title="Route53 Batch Proxy", lifespan=lifespan)


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.get("/ready")
async def ready(request: Request):
    try:
        await request.app.state.redis.ping()
        return {"status": "ready"}
    except Exception:
        return Response(
            content='{"status":"not ready","reason":"redis unavailable"}',
            status_code=503,
            media_type="application/json",
        )


@app.get("/metrics")
async def metrics():
    return Response(content=generate_latest(), media_type=CONTENT_TYPE_LATEST)


@app.get("/status")
async def status(request: Request):
    batcher = request.app.state.batcher
    zones = await batcher.active_zones()
    zone_depths = {}
    for zone_id in zones:
        depth = await batcher.queue_depth(zone_id)
        zone_depths[zone_id] = depth
        QUEUE_DEPTH.labels(zone_id=zone_id).set(depth)
    flusher = request.app.state.flusher
    is_leader = await flusher.try_acquire_leader()
    return {
        "active_zones": len(zones),
        "queue_depths": zone_depths,
        "total_pending": sum(zone_depths.values()),
        "is_flush_leader": is_leader,
    }


@app.api_route("/{path:path}", methods=["GET", "POST", "PUT", "DELETE", "PATCH"])
async def catch_all(request: Request) -> Response:
    return await request.app.state.proxy.handle_request(request)
