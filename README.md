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
Ansible Playbooks  →  Batch Proxy  →  AWS Route53
(uri module)          (queue + dedup + batch)   (≤5 req/s/zone)
```

1. Ansible sends standard Route53 XML via `ansible.builtin.uri` to the proxy
2. Proxy validates the AWS access key from the SigV4 `Authorization` header
3. `ChangeResourceRecordSets` calls are queued in Redis by hosted zone
4. Flush worker deduplicates and sends batched changes every 2s (configurable)
5. Proxy returns a synthetic `BATCH-*` change ID immediately — no waiting
6. `GetChange` lookups map synthetic IDs to the real Route53 change ID after flush

## Ansible Integration

The proxy accepts standard Route53 XML requests, so any Ansible playbook can use
it via `ansible.builtin.uri`. The key requirement is an `Authorization` header
containing the same AWS access key configured on the proxy.

### Required Variables

```yaml
# Proxy endpoint and auth
route53_proxy_url: https://route53-proxy-dev.apps.example.com
route53_aws_access_key_id: AKIA...
route53_aws_region: us-east-1

# Construct the auth header (proxy validates the access key only, not the signature)
route53_auth_header: >-
  AWS4-HMAC-SHA256
  Credential={{ route53_aws_access_key_id }}/20260101/{{ route53_aws_region }}/route53/aws4_request,
  SignedHeaders=host, Signature=proxy
```

### Create a Single A Record

```yaml
- name: Create DNS A record
  ansible.builtin.uri:
    url: "{{ route53_proxy_url }}/2013-04-01/hostedzone/{{ hosted_zone_id }}/rrset/"
    method: POST
    headers:
      Content-Type: application/xml
      Authorization: "{{ route53_auth_header }}"
    body: |
      <?xml version="1.0" encoding="UTF-8"?>
      <ChangeResourceRecordSetsRequest xmlns="https://route53.amazonaws.com/doc/2013-04-01/">
        <ChangeBatch><Changes><Change>
          <Action>UPSERT</Action>
          <ResourceRecordSet>
            <Name>bastion.{{ guid }}.{{ base_domain }}.</Name>
            <Type>A</Type>
            <TTL>300</TTL>
            <ResourceRecords>
              <ResourceRecord><Value>{{ bastion_ip }}</Value></ResourceRecord>
            </ResourceRecords>
          </ResourceRecordSet>
        </Change></Changes></ChangeBatch>
      </ChangeResourceRecordSetsRequest>
    status_code: 200
    validate_certs: false
  retries: 3
  delay: 5
```

### Delete a Single A Record

```yaml
- name: Delete DNS A record
  ansible.builtin.uri:
    url: "{{ route53_proxy_url }}/2013-04-01/hostedzone/{{ hosted_zone_id }}/rrset/"
    method: POST
    headers:
      Content-Type: application/xml
      Authorization: "{{ route53_auth_header }}"
    body: |
      <?xml version="1.0" encoding="UTF-8"?>
      <ChangeResourceRecordSetsRequest xmlns="https://route53.amazonaws.com/doc/2013-04-01/">
        <ChangeBatch><Changes><Change>
          <Action>DELETE</Action>
          <ResourceRecordSet>
            <Name>bastion.{{ guid }}.{{ base_domain }}.</Name>
            <Type>A</Type>
            <TTL>300</TTL>
            <ResourceRecords>
              <ResourceRecord><Value>{{ bastion_ip }}</Value></ResourceRecord>
            </ResourceRecords>
          </ResourceRecordSet>
        </Change></Changes></ChangeBatch>
      </ChangeResourceRecordSetsRequest>
    status_code: 200
    validate_certs: false
```

### Batch Multiple Records in One Request

Send all records for a lab environment in a single request. The proxy queues
them together and the flusher sends them as one Route53 API call.

```yaml
- name: Batch create all DNS records for environment
  ansible.builtin.uri:
    url: "{{ route53_proxy_url }}/2013-04-01/hostedzone/{{ hosted_zone_id }}/rrset/"
    method: POST
    headers:
      Content-Type: application/xml
      Authorization: "{{ route53_auth_header }}"
    body: |
      <?xml version="1.0" encoding="UTF-8"?>
      <ChangeResourceRecordSetsRequest xmlns="https://route53.amazonaws.com/doc/2013-04-01/">
        <ChangeBatch><Changes>
      {% for instance in instances %}
          <Change>
            <Action>UPSERT</Action>
            <ResourceRecordSet>
              <Name>{{ instance.name }}.{{ guid }}.{{ base_domain }}.</Name>
              <Type>A</Type>
              <TTL>300</TTL>
              <ResourceRecords>
                <ResourceRecord><Value>{{ instance.ip }}</Value></ResourceRecord>
              </ResourceRecords>
            </ResourceRecordSet>
          </Change>
      {% endfor %}
          <Change>
            <Action>UPSERT</Action>
            <ResourceRecordSet>
              <Name>*.apps.{{ guid }}.{{ base_domain }}.</Name>
              <Type>CNAME</Type>
              <TTL>300</TTL>
              <ResourceRecords>
                <ResourceRecord><Value>master.{{ guid }}.{{ base_domain }}.</Value></ResourceRecord>
              </ResourceRecords>
            </ResourceRecordSet>
          </Change>
        </Changes></ChangeBatch>
      </ChangeResourceRecordSetsRequest>
    status_code: 200
    validate_certs: false
    return_content: true
  register: batch_result
```

### Check Change Status

The proxy returns a `BATCH-*` change ID immediately. After the flusher sends
the batch to Route53, `GetChange` maps the synthetic ID to the real one.

```yaml
- name: Extract change ID
  ansible.builtin.set_fact:
    change_id: "{{ batch_result.content | regex_search('BATCH-[a-f0-9]+') | default('') }}"

- name: Check change status
  ansible.builtin.uri:
    url: "{{ route53_proxy_url }}/2013-04-01/change/{{ change_id }}"
    method: GET
    headers:
      Authorization: "{{ route53_auth_header }}"
    validate_certs: false
  when: change_id | length > 0
```

### Migrating from `amazon.aws.route53`

If your playbook currently uses the `amazon.aws.route53` module:

**Before (direct Route53):**
```yaml
- name: DNS entry
  amazon.aws.route53:
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
- name: DNS entry
  ansible.builtin.uri:
    url: "{{ route53_proxy_url }}/2013-04-01/hostedzone/{{ route53_aws_zone_id }}/rrset/"
    method: POST
    headers:
      Content-Type: application/xml
      Authorization: "{{ route53_auth_header }}"
    body: |
      <?xml version="1.0" encoding="UTF-8"?>
      <ChangeResourceRecordSetsRequest xmlns="https://route53.amazonaws.com/doc/2013-04-01/">
        <ChangeBatch><Changes><Change>
          <Action>UPSERT</Action>
          <ResourceRecordSet>
            <Name>{{ instance_name }}.{{ base_domain }}.</Name>
            <Type>A</Type>
            <TTL>300</TTL>
            <ResourceRecords>
              <ResourceRecord><Value>{{ instance_ip }}</Value></ResourceRecord>
            </ResourceRecords>
          </ResourceRecordSet>
        </Change></Changes></ChangeBatch>
      </ChangeResourceRecordSetsRequest>
    status_code: 200
    validate_certs: false
  retries: 3
  delay: 5
```

Key differences:
- Replace `amazon.aws.route53` with `ansible.builtin.uri` (no collection dependency)
- Add `Authorization` header with the AWS access key
- Body is standard Route53 XML (same format the AWS SDK sends)
- Fewer retries needed — the proxy handles batching and rate limiting
- Trailing dot on DNS names (e.g., `example.com.`) — required by Route53 XML API
- `state: present` maps to `<Action>UPSERT</Action>`
- `state: absent` maps to `<Action>DELETE</Action>`

## API Endpoints

| Endpoint | Method | Description |
|---|---|---|
| `POST /2013-04-01/hostedzone/{id}/rrset/` | POST | Queue DNS changes (returns `BATCH-*` ID) |
| `GET /2013-04-01/change/{id}` | GET | Check change status (maps synthetic → real ID) |
| `GET /health` | GET | Liveness probe (`{"status": "ok"}`) |
| `GET /ready` | GET | Readiness probe — checks Redis connectivity |
| `GET /metrics` | GET | Prometheus metrics |
| `GET /status` | GET | Queue depths per zone, flush leader status |

## Authentication

The proxy validates the AWS access key from the `Authorization` header on every
proxied request. Clients must use the same `AWS_ACCESS_KEY_ID` configured on the
proxy. The signature itself is not validated — only the access key is checked.

Unauthenticated requests receive a Route53-style `403` XML error:
- Missing header: `MissingAuthenticationToken`
- Wrong key: `InvalidClientTokenId`

Health, ready, metrics, and status endpoints do not require authentication.

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

# Run the proxy
export AWS_ACCESS_KEY_ID=...
export AWS_SECRET_ACCESS_KEY=...
uvicorn src.app:app --host 0.0.0.0 --port 8443

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

# Test with the test playbook
ansible-playbook ansible/test-dns.yml -e env=dev \
  -e guid=mytest -e hosted_zone_id=Z009829317DQYBBJZ02ZU --tags batch
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
