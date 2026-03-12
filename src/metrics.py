"""Prometheus metric definitions."""

from prometheus_client import Counter, Gauge, Histogram

CHANGES_QUEUED = Counter(
    "r53proxy_changes_queued_total", "Total changes queued", ["zone_id", "action"],
)
CHANGES_FLUSHED = Counter(
    "r53proxy_changes_flushed_total", "Total changes flushed to Route53", ["zone_id"],
)
FLUSH_BATCH_SIZE = Histogram(
    "r53proxy_flush_batch_size", "Number of changes per flush batch",
    buckets=[1, 5, 10, 25, 50, 100, 250, 500, 1000],
)
FLUSH_DURATION = Histogram(
    "r53proxy_flush_duration_seconds", "Time to send a batch to Route53",
    buckets=[0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0],
)
FLUSH_ERRORS = Counter(
    "r53proxy_flush_errors_total", "Failed flush attempts", ["zone_id"],
)
QUEUE_DEPTH = Gauge(
    "r53proxy_queue_depth", "Current queue depth", ["zone_id"],
)
PASSTHROUGH_TOTAL = Counter(
    "r53proxy_passthrough_total", "Pass-through requests", ["operation"],
)
REQUESTS_TOTAL = Counter(
    "r53proxy_requests_total", "Total incoming requests", ["operation"],
)
