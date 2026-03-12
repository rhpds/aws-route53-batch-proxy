# AWS Route53 Batch Proxy

Batching proxy that intercepts Route53 `ChangeResourceRecordSets` API calls,
queues DNS changes per hosted zone in Redis, and flushes consolidated batches
to stay under Route53's 5 req/s per-zone rate limit.

## Problem

125+ catalog items share one Route53 hosted zone. When workshops retire
simultaneously, the resulting wave of DNS cleanup operations overwhelms Route53's
rate limit, causing cascading failures and retry storms.

## How It Works

```
AAP Execution Environments  →  Batch Proxy  →  AWS Route53
(AWS_ENDPOINT_URL_ROUTE53)     (queue + batch)   (≤5 req/s/zone)
```

1. Ansible's `amazon.aws.route53` module calls the proxy instead of Route53
2. Proxy queues `ChangeResourceRecordSets` calls in Redis by hosted zone
3. Flush worker sends batched changes every 2 seconds (configurable)
4. All other Route53 operations pass through directly

## Local Development

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# Start Redis
docker run -d --name redis -p 6379:6379 redis:7

# Run the proxy
export AWS_ACCESS_KEY_ID=...
export AWS_SECRET_ACCESS_KEY=...
uvicorn src.app:app --host 0.0.0.0 --port 8443

# Run tests
python3 -m pytest tests/ -v
```

## Deployment

```bash
# Dev (main branch)
ansible-playbook playbooks/deploy.yaml -e env=dev

# Prod (production branch)
ansible-playbook playbooks/deploy.yaml -e env=prod
```

Pushes to `main` and `production` auto-trigger OpenShift builds via GitHub webhooks.

## Configuration

| Variable | Default | Description |
|---|---|---|
| `AWS_ACCESS_KEY_ID` | (required) | AWS credentials for Route53 |
| `AWS_SECRET_ACCESS_KEY` | (required) | AWS credentials for Route53 |
| `REDIS_URL` | `redis://localhost:6379` | Redis connection |
| `FLUSH_INTERVAL_SECONDS` | `2` | Flush frequency |
| `MAX_BATCH_SIZE` | `1000` | Max changes per batch |
| `FLUSH_BACKOFF_SECONDS` | `30` | Backoff after consecutive failures |
| `LOG_LEVEL` | `INFO` | Log level |
| `WORKERS` | `4` | Uvicorn worker count |
| `PORT` | `8443` | Listen port |
