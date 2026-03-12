# Route53 Batch Proxy — Design Spec

## Problem

125+ Babylon catalog items share a single Route53 hosted zone (`dynamic.redhatworkshops.io`, `Z009829317DQYBBJZ02ZU`). Route53 enforces a hard limit of 5 requests per second per hosted zone. When large workshops retire simultaneously, the resulting wave of DNS cleanup operations (5+ `ChangeResourceRecordSets` calls per cluster destroy) overwhelms this limit. Failed calls retry, creating a feedback loop that amplifies the storm.

On Mar 11 2026, 84 CNV cluster destroys from 3 workshops caused API call volume to spike from 15k/hr to 200k/hr, with 50-65% of requests throttled for over 7 hours.

## Solution

A batching proxy service that intercepts Route53 `ChangeResourceRecordSets` API calls, queues individual DNS changes per hosted zone, and flushes them in consolidated batches. Deployed on the Babylon infra cluster, serving all 3 AAP clusters.

## Architecture

```
AAP EE (us-east-2)  ──┐
AAP EE (us-west-2)  ──┼──▶  Route53 Batch Proxy  ──▶  AWS Route53
AAP EE (partner0)   ──┘     (Babylon infra cluster)
                             2 replicas + Redis
```

AAP execution environments set `AWS_ENDPOINT_URL_ROUTE53=https://route53-batch-proxy.babylon-infra.svc:8443` to redirect all boto3 Route53 API calls to the proxy. The proxy queues write operations in Redis and flushes them in batches. Read operations pass through directly.

### Scope

95% of the Route53 traffic comes from Ansible's `amazon.aws.route53` module, which uses boto3. The remaining 5% comes from `openshift-install` (Go binary, AWS Go SDK), which does not respect `AWS_ENDPOINT_URL_ROUTE53` and continues to call Route53 directly. This is acceptable — the proxy covers the operations causing the throttling.

## Components

### 1. FastAPI Application

Python FastAPI service that implements the Route53 REST API surface.

**Intercepted operations:**

| Operation | Behavior |
|---|---|
| `ChangeResourceRecordSets` | Queue changes in Redis, return synthetic response immediately |
| All other operations | Pass through to real Route53 via boto3 |

**Request flow for `ChangeResourceRecordSets`:**

1. Parse incoming XML request body to extract the `ChangeBatch` (list of CREATE/UPSERT/DELETE operations)
2. Push each change onto a Redis list keyed by hosted zone ID
3. Return a synthetic XML response immediately with a generated change ID and status `PENDING`

**Request flow for pass-through operations:**

1. Parse the Route53 REST URL to determine operation and parameters
2. Call the corresponding boto3 Route53 method with the proxy's own AWS credentials
3. Return the real Route53 response, re-serialized as XML

### 2. Flush Worker

A background asyncio task that runs on one replica (leader election via Redis lock).

**Flush loop (every 2 seconds, configurable):**

1. Acquire Redis leader lock (short TTL, auto-renewed)
2. For each hosted zone with pending changes:
   a. Pop all pending changes from the Redis list (atomic LRANGE + LTRIM)
   b. Collapse exact duplicate operations (same action + record name + record type + value)
   c. Preserve ordering of non-duplicate operations (DELETE then CREATE for the same record is valid)
   d. Chunk into batches of up to 1,000 changes (Route53 API limit)
   e. Call real Route53 `ChangeResourceRecordSets` for each batch
3. Log flush results (zone, batch size, duration, success/failure)

**Flush rate limiting:**

At a 2-second flush interval with one batch per zone, the proxy makes at most 0.5 requests/second per zone — well under the 5 req/s limit. Even with multiple zones flushing simultaneously, the total stays manageable.

**Flush failure handling:**

If a batch call to Route53 fails:
- Log the error with full batch contents
- Push the failed changes back onto the Redis list for retry on the next flush cycle
- Increment a failure counter metric
- If a zone has failed 5 consecutive flushes, emit an alert-level log and skip that zone for 30 seconds (backoff)

### 3. Redis

Shared state store for change queues and leader election.

**Data structures:**

| Key | Type | Purpose |
|---|---|---|
| `zone:{zone_id}:changes` | List | Pending changes as JSON-serialized dicts |
| `flush:leader` | String | Leader lock for flush worker (SET NX EX) |
| `zone:{zone_id}:fail_count` | String | Consecutive flush failure count |
| `change:{change_id}` | String | Maps synthetic change ID to real change ID (TTL 1 hour) |

**Sizing:** Redis should be provisioned with at least 1GB memory. Each queued change is ~500 bytes of JSON. At peak (1,000 concurrent destroys x 5 changes each = 5,000 changes), that's ~2.5MB — well within capacity. The generous sizing is for headroom during extreme spikes.

### 4. Route53 XML API Compatibility

The Route53 REST API uses XML, not JSON. The proxy must:

- Parse incoming XML request bodies (`ChangeResourceRecordSets` requests)
- Generate valid XML responses that boto3 can parse
- Handle the URL routing scheme: `POST /2013-04-01/hostedzone/{zone_id}/rrset/` for changes
- Pass through the XML responses from real Route53 for non-intercepted operations

Key XML structures:

**Request (ChangeResourceRecordSets):**
```xml
<?xml version="1.0" encoding="UTF-8"?>
<ChangeResourceRecordSetsRequest xmlns="https://route53.amazonaws.com/doc/2013-04-01/">
  <ChangeBatch>
    <Changes>
      <Change>
        <Action>DELETE</Action>
        <ResourceRecordSet>
          <Name>api.guid123.dynamic.redhatworkshops.io.</Name>
          <Type>A</Type>
          <TTL>300</TTL>
          <ResourceRecords>
            <ResourceRecord><Value>10.0.1.5</Value></ResourceRecord>
          </ResourceRecords>
        </ResourceRecordSet>
      </Change>
    </Changes>
  </ChangeBatch>
</ChangeResourceRecordSetsRequest>
```

**Response (synthetic):**
```xml
<?xml version="1.0" encoding="UTF-8"?>
<ChangeResourceRecordSetsResponse xmlns="https://route53.amazonaws.com/doc/2013-04-01/">
  <ChangeInfo>
    <Id>/change/BATCH-xxxxxxxx</Id>
    <Status>PENDING</Status>
    <SubmittedAt>2026-03-11T17:22:00Z</SubmittedAt>
  </ChangeInfo>
</ChangeResourceRecordSetsResponse>
```

### 5. Authentication

**Incoming requests:** The Ansible `amazon.aws.route53` module sends SigV4-signed requests. The proxy does NOT validate SigV4 signatures — it trusts requests arriving on the cluster network. Access is controlled by k8s NetworkPolicy (only AAP namespaces can reach the proxy service).

**Outgoing requests:** The proxy uses its own AWS credentials (env vars `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`) to call real Route53. These credentials need `route53:ChangeResourceRecordSets`, `route53:ListResourceRecordSets`, `route53:GetHostedZone`, `route53:GetChange` permissions.

### 6. GetChange Mapping

Although `wait: false` is the default (confirmed in AgnosticD), the proxy supports `GetChange` in case it's ever used:

1. When the flush worker sends a real batch to Route53, it gets back a real change ID
2. The worker maps all synthetic change IDs in that batch to the real change ID (stored in Redis with 1-hour TTL)
3. When a `GetChange` request arrives for a synthetic ID, the proxy looks up the real ID and passes the request through to Route53
4. If the synthetic ID hasn't been flushed yet (still queued), the proxy returns status `PENDING`

## Deployment

### Pod Sizing

These pods need to handle hundreds of concurrent connections from 3 AAP clusters during spike conditions.

**Proxy pods (2 replicas):**
- CPU: 2 cores request / 4 cores limit
- Memory: 2Gi request / 4Gi limit
- Uvicorn workers: 4 per pod (8 total across replicas)
- Max concurrent connections: configured at 1,000 per pod

**Redis pod (1 replica, persistent):**
- CPU: 1 core request / 2 cores limit
- Memory: 2Gi request / 4Gi limit
- Storage: 5Gi PVC (RBD or CephFS)
- `maxmemory-policy: allkeys-lru`

### Environments & Branching

Two environments deployed to separate namespaces on the Babylon infra cluster:

| Branch | Namespace | Purpose |
|---|---|---|
| `main` | `route53-batch-proxy-dev` | Development — test against non-production AAP clusters |
| `production` | `route53-batch-proxy` | Production — serves all 3 AAP production clusters |

**Pushes to `main` and `production` auto-trigger OpenShift builds** via GitHub webhooks (same pattern as Parsec). The BuildConfig's `image.openshift.io/triggers` annotation auto-triggers a rollout when the new image lands in the ImageStream. Do NOT manually trigger builds with `oc start-build`.

### Kubernetes Resources (per namespace)

- Namespace: `route53-batch-proxy-dev` / `route53-batch-proxy`
- BuildConfig: `route53-batch-proxy` (GitHub webhook trigger, Docker strategy)
- ImageStream: `route53-batch-proxy`
- Deployment: `route53-batch-proxy` (2 replicas, anti-affinity)
- Service: `route53-batch-proxy` (ClusterIP, port 8443)
- Deployment: `route53-batch-proxy-redis` (1 replica, persistent)
- Service: `route53-batch-proxy-redis` (ClusterIP, port 6379)
- Secret: `route53-batch-proxy-aws-creds` (AWS credentials for Route53 access)
- Secret: `route53-batch-proxy-tls` (TLS cert/key for HTTPS — boto3 talks HTTPS to Route53)
- ConfigMap: `route53-batch-proxy-config` (flush interval, batch size, log level)
- NetworkPolicy: allow ingress only from AAP cluster source IPs / namespaces
- PodDisruptionBudget: `minAvailable: 1`

### TLS

boto3 expects to talk HTTPS to Route53. The proxy must terminate TLS. Options:
- Self-signed CA cert distributed to AAP execution environments (added to trust store)
- OpenShift service serving cert (auto-generated, auto-rotated) if the proxy is on an OpenShift cluster

The AAP execution environments need the proxy's CA cert in their trust store. This can be injected via a ConfigMap mount or baked into the EE image.

### Configuration

Environment variables on the proxy pods:

| Variable | Default | Description |
|---|---|---|
| `AWS_ACCESS_KEY_ID` | (required) | AWS credentials for real Route53 calls |
| `AWS_SECRET_ACCESS_KEY` | (required) | AWS credentials for real Route53 calls |
| `REDIS_URL` | `redis://route53-batch-proxy-redis:6379` | Redis connection URL |
| `FLUSH_INTERVAL_SECONDS` | `2` | How often the flush worker runs |
| `MAX_BATCH_SIZE` | `1000` | Max changes per Route53 batch call |
| `FLUSH_BACKOFF_SECONDS` | `30` | Backoff duration after 5 consecutive flush failures |
| `LOG_LEVEL` | `INFO` | Logging level |
| `WORKERS` | `4` | Uvicorn worker count |
| `PORT` | `8443` | Listen port |

### AAP Execution Environment Configuration

Add to the EE's environment variables (via AAP settings or k8s pod spec):

```
AWS_ENDPOINT_URL_ROUTE53=https://route53-batch-proxy.babylon-infra.svc:8443
# Or if using a cross-cluster route:
AWS_ENDPOINT_URL_ROUTE53=https://route53-batch-proxy.apps.babylon-cluster.example.com
```

If cross-cluster (AAP clusters are separate from Babylon), expose the proxy via an OpenShift Route with TLS passthrough, or use a direct pod IP / NodePort if clusters share a network.

## Observability

### Metrics (Prometheus)

| Metric | Type | Description |
|---|---|---|
| `r53proxy_changes_queued_total` | Counter | Total changes queued, labeled by zone_id and action |
| `r53proxy_changes_flushed_total` | Counter | Total changes flushed to Route53, labeled by zone_id |
| `r53proxy_flush_batch_size` | Histogram | Number of changes per flush batch |
| `r53proxy_flush_duration_seconds` | Histogram | Time to send a batch to Route53 |
| `r53proxy_flush_errors_total` | Counter | Failed flush attempts, labeled by zone_id |
| `r53proxy_queue_depth` | Gauge | Current queue depth per zone_id |
| `r53proxy_passthrough_total` | Counter | Pass-through requests by operation type |
| `r53proxy_requests_total` | Counter | Total incoming requests by operation |

### Logging

Structured JSON logs:
- Every incoming request: operation, zone_id, change count, source IP
- Every flush: zone_id, batch_size, duration, success/failure
- Errors: full change details on flush failure for debugging

### Health Endpoints

| Endpoint | Purpose |
|---|---|
| `GET /health` | Liveness — returns 200 if the process is running |
| `GET /ready` | Readiness — returns 200 if Redis is connected and flush loop is active |
| `GET /metrics` | Prometheus metrics |
| `GET /status` | JSON status: queue depths per zone, flush stats, leader status |

## Failure Modes

| Scenario | Behavior |
|---|---|
| Redis briefly unavailable | Proxy falls back to pass-through (direct Route53 calls). Logs warning. Resumes batching when Redis returns. |
| Redis permanently down | All requests pass through directly. No batching protection, but no data loss. Same as not having the proxy. |
| Flush leader dies | Other replica acquires leader lock within one flush interval. Queued changes in Redis are preserved. |
| Real Route53 returns throttling error | Flush worker retries on next cycle. Changes stay in Redis queue. Backoff after 5 consecutive failures. |
| Real Route53 returns validation error | Log the error with full batch contents. Discard the invalid change (don't retry forever). Alert-level log. |
| Proxy pod OOM / crash | K8s restarts it. Queued changes are in Redis, not lost. Other replica continues serving. |
| All proxy pods down | AAP jobs fail their Route53 calls. Operators unset `AWS_ENDPOINT_URL_ROUTE53` to bypass. |

## Project Structure

```
aws-route53-batch-proxy/
  src/
    app.py              # FastAPI app, lifespan, health endpoints
    config.py           # Configuration from env vars
    proxy.py            # Request routing — intercept vs pass-through
    batcher.py          # Change queue management, Redis operations
    flusher.py          # Flush worker, leader election, batch sending
    route53_xml.py      # XML parsing and generation for Route53 API
    route53_client.py   # boto3 wrapper for real Route53 calls
    metrics.py          # Prometheus metrics
  tests/
    test_batcher.py     # Queue and dedup logic
    test_flusher.py     # Flush worker with mocked Redis/Route53
    test_route53_xml.py # XML parsing/generation
    test_proxy.py       # Integration tests with fake Route53
    conftest.py         # Shared fixtures
  playbooks/
    deploy.yaml         # Ansible deployment playbook
    vars/
      common.yml        # Shared variables (committed)
      dev.yml.example   # Dev vars template
      prod.yml.example  # Prod vars template
    templates/
      manifests.yaml.j2 # All OpenShift manifests (Jinja2 template)
  Dockerfile
  requirements.txt
  README.md
```

### Deployment

Deployment is managed by an Ansible playbook (`playbooks/deploy.yaml`), same pattern as Parsec. All manifests are rendered from a single Jinja2 template (`playbooks/templates/manifests.yaml.j2`).

```bash
# Dev deploy (main branch)
ansible-playbook playbooks/deploy.yaml -e env=dev

# Prod deploy (production branch)
ansible-playbook playbooks/deploy.yaml -e env=prod
```

The playbook creates:
1. Namespace (`route53-batch-proxy-dev` or `route53-batch-proxy`)
2. BuildConfig with GitHub webhook trigger (source: `main` or `production` branch)
3. ImageStream
4. Secrets (AWS creds, TLS)
5. ConfigMap
6. Redis deployment + service + PVC
7. Proxy deployment + service
8. NetworkPolicy + PDB

## Dependencies

- Python 3.12+
- FastAPI + uvicorn
- boto3 (Route53 client)
- redis (async redis client, `redis[hiredis]`)
- prometheus-client
- lxml (XML parsing)
- httpx (async HTTP for pass-through)

## Testing Strategy

- **Unit tests:** XML parsing, change deduplication, queue operations (mocked Redis)
- **Integration tests:** Full request flow with mocked Route53 endpoint, verify batching behavior
- **Load test:** Simulate 500 concurrent `ChangeResourceRecordSets` calls, verify batching reduces actual Route53 calls to < 5/second
- **Failure tests:** Redis disconnect, Route53 throttling, pod restart — verify graceful degradation

## Out of Scope

- Intercepting `openshift-install` binary Route53 calls (Go SDK, 5% of traffic)
- Multi-region Route53 failover
- Caching of Route53 read operations
- SigV4 signature validation on incoming requests
