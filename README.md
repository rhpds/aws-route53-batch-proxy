# AWS Route53 Batch Proxy

Batching proxy that intercepts Route53 `ChangeResourceRecordSets` API calls,
queues DNS changes per hosted zone in Redis, deduplicates, and flushes
consolidated batches to stay under Route53's 5 req/s per-zone rate limit.

Read-only operations (`ListResourceRecordSets`, `GetHostedZone`, etc.) are
forwarded to Route53 directly.

## Problem

125+ catalog items share one Route53 hosted zone. When workshops retire
simultaneously, the resulting wave of DNS cleanup operations overwhelms Route53's
rate limit, causing cascading failures and retry storms.

## How It Works

```
Ansible / boto3 / AWS CLI  →  Batch Proxy  →  AWS Route53
(endpoint_url override)       (queue + dedup + batch writes)
                              (forward reads)
```

1. Point `endpoint_url` (or `AWS_ENDPOINT_URL_ROUTE53`) at the proxy
2. Clients use standard AWS credentials and SDK calls — no code changes
3. **Writes** (`ChangeResourceRecordSets`) are queued in Redis by hosted zone
4. **Reads** (`ListResourceRecordSets`, `GetHostedZone`, etc.) are forwarded to Route53
5. Flush worker deduplicates and sends batched writes every 2s (configurable)
6. Proxy returns a synthetic `BATCH-*` change ID immediately
7. `GetChange` maps synthetic IDs to the real Route53 change ID after flush

## Ansible Integration

The proxy is a drop-in replacement for the Route53 API. Use the standard
`amazon.aws.route53` module — just add `endpoint_url` pointing at the proxy.

### Create a Record

```yaml
- name: Create DNS A record
  amazon.aws.route53:
    state: present
    aws_access_key_id: "{{ route53_aws_access_key_id }}"
    aws_secret_access_key: "{{ route53_aws_secret_access_key }}"
    endpoint_url: "{{ route53_proxy_url }}"
    validate_certs: false
    hosted_zone_id: "{{ route53_aws_zone_id }}"
    record: "bastion.{{ guid }}.{{ base_domain }}"
    type: A
    ttl: 300
    value: "{{ bastion_ip }}"
    overwrite: true
```

### Delete a Record

```yaml
- name: Delete DNS A record
  amazon.aws.route53:
    state: absent
    aws_access_key_id: "{{ route53_aws_access_key_id }}"
    aws_secret_access_key: "{{ route53_aws_secret_access_key }}"
    endpoint_url: "{{ route53_proxy_url }}"
    validate_certs: false
    hosted_zone_id: "{{ route53_aws_zone_id }}"
    record: "bastion.{{ guid }}.{{ base_domain }}"
    type: A
    ttl: 300
    value: "{{ bastion_ip }}"
```

### Loop Over Instances

```yaml
- name: Create DNS records for all instances
  amazon.aws.route53:
    state: present
    aws_access_key_id: "{{ route53_aws_access_key_id }}"
    aws_secret_access_key: "{{ route53_aws_secret_access_key }}"
    endpoint_url: "{{ route53_proxy_url }}"
    validate_certs: false
    hosted_zone_id: "{{ route53_aws_zone_id }}"
    record: "{{ item.name }}.{{ guid }}.{{ base_domain }}"
    type: A
    ttl: 300
    value: "{{ item.ip }}"
    overwrite: true
  loop: "{{ instances }}"
  retries: 3
  delay: 5
```

### Migrating Existing Playbooks

Add `endpoint_url` and `validate_certs: false`. Everything else stays the same.

**Before (direct Route53):**
```yaml
- amazon.aws.route53:
    state: present
    aws_access_key_id: "{{ route53_aws_access_key_id }}"
    aws_secret_access_key: "{{ route53_aws_secret_access_key }}"
    hosted_zone_id: "{{ route53_aws_zone_id }}"
    record: "{{ instance_name }}.{{ base_domain }}"
    value: "{{ instance_ip }}"
    type: A
  retries: 10
  delay: "{{ 60 | random(start=5) }}"
```

**After (via proxy):**
```yaml
- amazon.aws.route53:
    state: present
    aws_access_key_id: "{{ route53_aws_access_key_id }}"
    aws_secret_access_key: "{{ route53_aws_secret_access_key }}"
    endpoint_url: "{{ route53_proxy_url }}"          # ← add this
    validate_certs: false                             # ← add this
    hosted_zone_id: "{{ route53_aws_zone_id }}"
    record: "{{ instance_name }}.{{ base_domain }}"
    value: "{{ instance_ip }}"
    type: A
  retries: 3                                          # ← fewer retries needed
  delay: 5
```

### Using with the AWS CLI

```bash
# Set the endpoint override
export AWS_ENDPOINT_URL_ROUTE53=https://route53-proxy-dev.apps.example.com

# Use the CLI normally — writes are batched, reads are forwarded
aws route53 change-resource-record-sets \
  --hosted-zone-id Z009829317DQYBBJZ02ZU \
  --change-batch file://changes.json

aws route53 list-resource-record-sets \
  --hosted-zone-id Z009829317DQYBBJZ02ZU
```

### Using with boto3

```python
import boto3

client = boto3.client(
    "route53",
    endpoint_url="https://route53-proxy-dev.apps.example.com",
    verify=False,
)

# This write is batched through the proxy
client.change_resource_record_sets(
    HostedZoneId="Z009829317DQYBBJZ02ZU",
    ChangeBatch={"Changes": [...]},
)

# This read is forwarded to Route53 directly
client.list_resource_record_sets(
    HostedZoneId="Z009829317DQYBBJZ02ZU",
)
```

## What Gets Batched

| Operation | Behavior |
|---|---|
| `ChangeResourceRecordSets` | **Batched** — queued in Redis, deduped, flushed every 2s |
| `GetChange` | Handled locally — maps `BATCH-*` IDs to real change IDs |
| Everything else | **Forwarded** to Route53 with re-signed SigV4 credentials |

## API Endpoints

The proxy listens on two ports:

**Port 8443 — External (exposed via Route, all requests require AWS auth):**

| Endpoint | Method | Description |
|---|---|---|
| `POST /2013-04-01/hostedzone/{id}/rrset/` | POST | Queue DNS changes (returns `BATCH-*` ID) |
| `GET /2013-04-01/change/{id}` | GET | Check change status |
| `* /*` | * | Forward to Route53 |

**Port 9090 — Internal (cluster-only, no Route, no auth):**

| Endpoint | Method | Description |
|---|---|---|
| `GET /health` | GET | Liveness probe |
| `GET /ready` | GET | Readiness probe (checks Redis) |
| `GET /metrics` | GET | Prometheus metrics |
| `GET /status` | GET | Queue depths per zone, flush leader status |

## Authentication

The proxy validates the AWS access key from the SigV4 `Authorization` header
on all requests to port 8443. Clients must use the same `AWS_ACCESS_KEY_ID`
configured on the proxy. The signature is not validated — the proxy re-signs
requests to Route53 with its own credentials for passthrough operations.

Port 9090 (health, ready, metrics, status) has no authentication and is only
accessible within the cluster via the ClusterIP Service.

## Metrics

Prometheus metrics at `/metrics`:

| Metric | Type | Labels | Description |
|---|---|---|---|
| `r53proxy_changes_queued_total` | Counter | zone_id, action | Changes queued |
| `r53proxy_changes_flushed_total` | Counter | zone_id | Changes flushed to Route53 |
| `r53proxy_flush_batch_size` | Histogram | — | Changes per flush batch |
| `r53proxy_flush_duration_seconds` | Histogram | — | Time to send a batch |
| `r53proxy_flush_errors_total` | Counter | zone_id | Failed flush attempts |
| `r53proxy_queue_depth` | Gauge | zone_id | Current queue depth |
| `r53proxy_requests_total` | Counter | operation | Total incoming requests |
| `r53proxy_passthrough_total` | Counter | operation | Pass-through requests |

## Local Development

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# Start Redis
docker run -d --name redis -p 6379:6379 redis:7

# Run the proxy (starts both ports: 8443 external, 9090 internal)
export AWS_ACCESS_KEY_ID=...
export AWS_SECRET_ACCESS_KEY=...
python3 -m src.server

# Run tests
python3 -m pytest tests/ -v

# Lint
python3 -m ruff check src/ tests/
python3 -m ruff format --check src/ tests/
```

Pre-commit hooks (ruff + pytest) are configured in `.githooks/`. Activate with:
```bash
git config core.hooksPath .githooks
```

## OpenShift Deployment

Deployed via Ansible playbooks using `kubernetes.core` modules.

```bash
# First time: create vars file from template
cp ansible/vars/dev.yml.example ansible/vars/dev.yml
# Edit ansible/vars/dev.yml with AWS creds, kubeconfig path, webhook secret

# Full deploy (namespace + pull secret + manifests + build)
ansible-playbook ansible/deploy.yml -e env=dev

# Apply manifests only (no build wait)
ansible-playbook ansible/deploy.yml -e env=dev --tags apply
```

Pushes to `main` auto-trigger OpenShift builds via GitHub webhook.

## Configuration

| Variable | Default | Description |
|---|---|---|
| `AWS_ACCESS_KEY_ID` | (required) | AWS credentials for Route53 + auth validation |
| `AWS_SECRET_ACCESS_KEY` | (required) | AWS credentials for Route53 |
| `AWS_REGION` | `us-east-1` | AWS region |
| `REDIS_URL` | `redis://localhost:6379` | Redis connection |
| `FLUSH_INTERVAL_SECONDS` | `2` | How often the flusher runs |
| `MAX_BATCH_SIZE` | `1000` | Max changes per Route53 API call |
| `FLUSH_BACKOFF_SECONDS` | `30` | Backoff duration after consecutive failures |
| `MAX_FLUSH_FAILURES` | `5` | Consecutive failures before backoff |
| `LEADER_LOCK_TTL` | `10` | Redis leader lock TTL (seconds) |
| `LOG_LEVEL` | `INFO` | Log level |
| `PORT` | `8443` | Listen port |
| `WORKERS` | `4` | Uvicorn worker count |
