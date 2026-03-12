"""Configuration from environment variables."""

import os


class Config:
    AWS_ACCESS_KEY_ID: str = os.environ.get("AWS_ACCESS_KEY_ID", "")
    AWS_SECRET_ACCESS_KEY: str = os.environ.get("AWS_SECRET_ACCESS_KEY", "")
    AWS_REGION: str = os.environ.get("AWS_REGION", "us-east-1")
    REDIS_URL: str = os.environ.get("REDIS_URL", "redis://localhost:6379")
    FLUSH_INTERVAL_SECONDS: float = float(os.environ.get("FLUSH_INTERVAL_SECONDS", "2"))
    MAX_BATCH_SIZE: int = int(os.environ.get("MAX_BATCH_SIZE", "1000"))
    FLUSH_BACKOFF_SECONDS: float = float(os.environ.get("FLUSH_BACKOFF_SECONDS", "30"))
    MAX_FLUSH_FAILURES: int = int(os.environ.get("MAX_FLUSH_FAILURES", "5"))
    LOG_LEVEL: str = os.environ.get("LOG_LEVEL", "INFO")
    PORT: int = int(os.environ.get("PORT", "8443"))
    WORKERS: int = int(os.environ.get("WORKERS", "4"))
    LEADER_LOCK_TTL: int = int(os.environ.get("LEADER_LOCK_TTL", "10"))

config = Config()
