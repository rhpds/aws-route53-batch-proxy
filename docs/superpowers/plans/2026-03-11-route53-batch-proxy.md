# Route53 Batch Proxy Implementation Plan

> **For agentic workers:** REQUIRED: Use superpowers:subagent-driven-development (if subagents available) or superpowers:executing-plans to implement this plan. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a batching proxy that intercepts Route53 ChangeResourceRecordSets calls, queues them in Redis per hosted zone, and flushes consolidated batches to stay under Route53's 5 req/s per-zone limit.

**Architecture:** FastAPI service parses Route53 XML API, queues changes in Redis, background flush worker sends batched calls to real Route53. Deployed on Babylon infra cluster with 2 replicas + Redis. GitHub webhooks trigger OpenShift builds (main→dev, production→prod).

**Tech Stack:** Python 3.12, FastAPI, uvicorn, boto3, redis[hiredis], lxml, prometheus-client

---

## File Map

| File | Responsibility |
|---|---|
| `src/config.py` | Load env vars, defaults, validation |
| `src/route53_xml.py` | Parse Route53 XML requests, generate XML responses |
| `src/batcher.py` | Redis queue operations, change dedup logic |
| `src/flusher.py` | Flush worker, leader election, batch sending to Route53 |
| `src/route53_client.py` | boto3 wrapper for real Route53 API calls |
| `src/metrics.py` | Prometheus metric definitions |
| `src/proxy.py` | Route53 URL routing — intercept vs pass-through |
| `src/app.py` | FastAPI app, lifespan hooks, health/status endpoints |
| `tests/conftest.py` | Shared fixtures (fake Redis, mock boto3) |
| `tests/test_route53_xml.py` | XML parsing and generation tests |
| `tests/test_batcher.py` | Queue and dedup logic tests |
| `tests/test_flusher.py` | Flush worker tests with mocked Redis/Route53 |
| `tests/test_proxy.py` | HTTP-level integration tests |
| `Dockerfile` | Multi-stage UBI 9 container image |
| `requirements.txt` | Python dependencies |
| `playbooks/deploy.yaml` | Ansible deployment playbook |
| `playbooks/vars/common.yml` | Shared deployment variables |
| `playbooks/vars/dev.yml.example` | Dev vars template |
| `playbooks/vars/prod.yml.example` | Prod vars template |
| `playbooks/templates/manifests.yaml.j2` | All OpenShift manifests |

---

## Chunk 1: Core Libraries (config, XML, batcher)

### Task 1: Project scaffolding

**Files:**
- Create: `requirements.txt`
- Create: `src/__init__.py`
- Create: `tests/__init__.py`
- Create: `src/config.py`
- Create: `tests/conftest.py`

- [ ] **Step 1: Create requirements.txt**

```
fastapi>=0.115.0
uvicorn[standard]>=0.32.0
boto3>=1.35.0
redis[hiredis]>=5.2.0
prometheus-client>=0.21.0
lxml>=5.3.0
httpx>=0.28.0
pytest>=8.3.0
pytest-asyncio>=0.24.0
moto[route53]>=5.0.0
fakeredis>=2.26.0
```

- [ ] **Step 2: Create src/__init__.py and tests/__init__.py**

Both empty files.

- [ ] **Step 3: Create src/config.py**

```python
"""Configuration from environment variables."""

import os


class Config:
    AWS_ACCESS_KEY_ID: str = os.environ.get("AWS_ACCESS_KEY_ID", "")
    AWS_SECRET_ACCESS_KEY: str = os.environ.get("AWS_SECRET_ACCESS_KEY", "")
    AWS_REGION: str = os.environ.get("AWS_REGION", "us-east-1")
    REDIS_URL: str = os.environ.get("REDIS_URL", "redis://localhost:6379")
    FLUSH_INTERVAL_SECONDS: float = float(
        os.environ.get("FLUSH_INTERVAL_SECONDS", "2")
    )
    MAX_BATCH_SIZE: int = int(os.environ.get("MAX_BATCH_SIZE", "1000"))
    FLUSH_BACKOFF_SECONDS: float = float(
        os.environ.get("FLUSH_BACKOFF_SECONDS", "30")
    )
    MAX_FLUSH_FAILURES: int = int(os.environ.get("MAX_FLUSH_FAILURES", "5"))
    LOG_LEVEL: str = os.environ.get("LOG_LEVEL", "INFO")
    PORT: int = int(os.environ.get("PORT", "8443"))
    WORKERS: int = int(os.environ.get("WORKERS", "4"))
    LEADER_LOCK_TTL: int = int(os.environ.get("LEADER_LOCK_TTL", "10"))


config = Config()
```

- [ ] **Step 4: Create tests/conftest.py**

```python
"""Shared test fixtures."""

import pytest
import fakeredis.aioredis


@pytest.fixture
async def redis_client():
    client = fakeredis.aioredis.FakeRedis(decode_responses=True)
    yield client
    await client.aclose()
```

- [ ] **Step 5: Install dependencies and verify**

Run: `cd ~/aws-route53-batch-proxy && python3 -m venv .venv && source .venv/bin/activate && pip install -r requirements.txt`
Expected: All packages install successfully.

- [ ] **Step 6: Commit**

```bash
git add requirements.txt src/__init__.py tests/__init__.py src/config.py tests/conftest.py
git commit -m "Project scaffolding: deps, config, test fixtures"
```

---

### Task 2: Route53 XML parsing and generation

**Files:**
- Create: `src/route53_xml.py`
- Create: `tests/test_route53_xml.py`

- [ ] **Step 1: Write the failing tests**

```python
"""Tests for Route53 XML parsing and generation."""

import pytest
from src.route53_xml import (
    parse_change_batch,
    build_change_response,
    build_change_batch_xml,
)


class TestParseChangeBatch:
    def test_parse_single_delete(self):
        xml = """<?xml version="1.0" encoding="UTF-8"?>
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
        </ChangeResourceRecordSetsRequest>"""
        changes = parse_change_batch(xml.encode())
        assert len(changes) == 1
        assert changes[0]["action"] == "DELETE"
        assert changes[0]["name"] == "api.guid123.dynamic.redhatworkshops.io."
        assert changes[0]["type"] == "A"
        assert changes[0]["ttl"] == 300
        assert changes[0]["values"] == ["10.0.1.5"]

    def test_parse_multiple_changes(self):
        xml = """<?xml version="1.0" encoding="UTF-8"?>
        <ChangeResourceRecordSetsRequest xmlns="https://route53.amazonaws.com/doc/2013-04-01/">
          <ChangeBatch>
            <Changes>
              <Change>
                <Action>DELETE</Action>
                <ResourceRecordSet>
                  <Name>api.g1.example.com.</Name>
                  <Type>A</Type>
                  <TTL>300</TTL>
                  <ResourceRecords>
                    <ResourceRecord><Value>10.0.1.1</Value></ResourceRecord>
                  </ResourceRecords>
                </ResourceRecordSet>
              </Change>
              <Change>
                <Action>UPSERT</Action>
                <ResourceRecordSet>
                  <Name>apps.g2.example.com.</Name>
                  <Type>CNAME</Type>
                  <TTL>60</TTL>
                  <ResourceRecords>
                    <ResourceRecord><Value>lb.example.com</Value></ResourceRecord>
                  </ResourceRecords>
                </ResourceRecordSet>
              </Change>
            </Changes>
          </ChangeBatch>
        </ChangeResourceRecordSetsRequest>"""
        changes = parse_change_batch(xml.encode())
        assert len(changes) == 2
        assert changes[0]["action"] == "DELETE"
        assert changes[1]["action"] == "UPSERT"
        assert changes[1]["type"] == "CNAME"

    def test_parse_alias_record(self):
        xml = """<?xml version="1.0" encoding="UTF-8"?>
        <ChangeResourceRecordSetsRequest xmlns="https://route53.amazonaws.com/doc/2013-04-01/">
          <ChangeBatch>
            <Changes>
              <Change>
                <Action>CREATE</Action>
                <ResourceRecordSet>
                  <Name>apps.g1.example.com.</Name>
                  <Type>A</Type>
                  <AliasTarget>
                    <HostedZoneId>Z1234</HostedZoneId>
                    <DNSName>lb.us-east-1.elb.amazonaws.com.</DNSName>
                    <EvaluateTargetHealth>false</EvaluateTargetHealth>
                  </AliasTarget>
                </ResourceRecordSet>
              </Change>
            </Changes>
          </ChangeBatch>
        </ChangeResourceRecordSetsRequest>"""
        changes = parse_change_batch(xml.encode())
        assert len(changes) == 1
        assert changes[0]["action"] == "CREATE"
        assert changes[0]["alias_target"]["hosted_zone_id"] == "Z1234"
        assert "lb.us-east-1.elb.amazonaws.com." in changes[0]["alias_target"]["dns_name"]
        assert changes[0].get("ttl") is None
        assert changes[0].get("values") is None

    def test_parse_no_ttl(self):
        xml = """<?xml version="1.0" encoding="UTF-8"?>
        <ChangeResourceRecordSetsRequest xmlns="https://route53.amazonaws.com/doc/2013-04-01/">
          <ChangeBatch>
            <Changes>
              <Change>
                <Action>DELETE</Action>
                <ResourceRecordSet>
                  <Name>api.g1.example.com.</Name>
                  <Type>A</Type>
                  <ResourceRecords>
                    <ResourceRecord><Value>10.0.1.1</Value></ResourceRecord>
                  </ResourceRecords>
                </ResourceRecordSet>
              </Change>
            </Changes>
          </ChangeBatch>
        </ChangeResourceRecordSetsRequest>"""
        changes = parse_change_batch(xml.encode())
        assert changes[0].get("ttl") is None


class TestBuildChangeResponse:
    def test_build_response(self):
        xml_str = build_change_response("BATCH-abc123")
        assert "BATCH-abc123" in xml_str
        assert "PENDING" in xml_str
        assert "ChangeResourceRecordSetsResponse" in xml_str
        assert "ChangeInfo" in xml_str
        assert "SubmittedAt" in xml_str


class TestBuildChangeBatchXml:
    def test_roundtrip_single_change(self):
        changes = [
            {
                "action": "DELETE",
                "name": "api.g1.example.com.",
                "type": "A",
                "ttl": 300,
                "values": ["10.0.1.5"],
            }
        ]
        xml_bytes = build_change_batch_xml(changes)
        reparsed = parse_change_batch(xml_bytes)
        assert len(reparsed) == 1
        assert reparsed[0]["action"] == "DELETE"
        assert reparsed[0]["name"] == "api.g1.example.com."
        assert reparsed[0]["values"] == ["10.0.1.5"]

    def test_roundtrip_alias(self):
        changes = [
            {
                "action": "CREATE",
                "name": "apps.g1.example.com.",
                "type": "A",
                "alias_target": {
                    "hosted_zone_id": "Z1234",
                    "dns_name": "lb.us-east-1.elb.amazonaws.com.",
                    "evaluate_target_health": False,
                },
            }
        ]
        xml_bytes = build_change_batch_xml(changes)
        reparsed = parse_change_batch(xml_bytes)
        assert reparsed[0]["alias_target"]["hosted_zone_id"] == "Z1234"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd ~/aws-route53-batch-proxy && source .venv/bin/activate && python3 -m pytest tests/test_route53_xml.py -v`
Expected: ImportError — `src.route53_xml` does not exist.

- [ ] **Step 3: Implement route53_xml.py**

```python
"""Route53 XML parsing and generation."""

import uuid
from datetime import datetime, timezone

from lxml import etree

NS = "https://route53.amazonaws.com/doc/2013-04-01/"
NSMAP = {None: NS}


def _find(el: etree._Element, tag: str) -> etree._Element | None:
    return el.find(f"{{{NS}}}{tag}")


def _findtext(el: etree._Element, tag: str) -> str | None:
    child = _find(el, tag)
    return child.text if child is not None else None


def parse_change_batch(xml_bytes: bytes) -> list[dict]:
    """Parse a ChangeResourceRecordSetsRequest XML body into a list of change dicts."""
    root = etree.fromstring(xml_bytes)
    changes = []
    change_batch = _find(root, "ChangeBatch")
    if change_batch is None:
        return changes
    changes_el = _find(change_batch, "Changes")
    if changes_el is None:
        return changes
    for change_el in changes_el.findall(f"{{{NS}}}Change"):
        action = _findtext(change_el, "Action")
        rrset = _find(change_el, "ResourceRecordSet")
        if rrset is None:
            continue
        name = _findtext(rrset, "Name")
        rtype = _findtext(rrset, "Type")
        ttl_text = _findtext(rrset, "TTL")
        ttl = int(ttl_text) if ttl_text else None

        change: dict = {"action": action, "name": name, "type": rtype}
        if ttl is not None:
            change["ttl"] = ttl

        # Resource records (normal records)
        rr_el = _find(rrset, "ResourceRecords")
        if rr_el is not None:
            values = []
            for rr in rr_el.findall(f"{{{NS}}}ResourceRecord"):
                val = _findtext(rr, "Value")
                if val:
                    values.append(val)
            change["values"] = values

        # Alias target
        alias_el = _find(rrset, "AliasTarget")
        if alias_el is not None:
            eval_health_text = _findtext(alias_el, "EvaluateTargetHealth")
            change["alias_target"] = {
                "hosted_zone_id": _findtext(alias_el, "HostedZoneId"),
                "dns_name": _findtext(alias_el, "DNSName"),
                "evaluate_target_health": eval_health_text.lower() == "true"
                if eval_health_text
                else False,
            }

        changes.append(change)
    return changes


def build_change_response(change_id: str) -> str:
    """Build a synthetic ChangeResourceRecordSetsResponse XML string."""
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<ChangeResourceRecordSetsResponse xmlns="{NS}">
  <ChangeInfo>
    <Id>/change/{change_id}</Id>
    <Status>PENDING</Status>
    <SubmittedAt>{now}</SubmittedAt>
  </ChangeInfo>
</ChangeResourceRecordSetsResponse>"""


def build_change_batch_xml(changes: list[dict]) -> bytes:
    """Build a ChangeResourceRecordSetsRequest XML body from a list of change dicts.

    Used by the flush worker to send batched changes to real Route53.
    """
    root = etree.Element(
        "ChangeResourceRecordSetsRequest", nsmap=NSMAP
    )
    batch_el = etree.SubElement(root, "ChangeBatch")
    changes_el = etree.SubElement(batch_el, "Changes")

    for change in changes:
        change_el = etree.SubElement(changes_el, "Change")
        etree.SubElement(change_el, "Action").text = change["action"]
        rrset_el = etree.SubElement(change_el, "ResourceRecordSet")
        etree.SubElement(rrset_el, "Name").text = change["name"]
        etree.SubElement(rrset_el, "Type").text = change["type"]

        if "ttl" in change and change["ttl"] is not None:
            etree.SubElement(rrset_el, "TTL").text = str(change["ttl"])

        if "values" in change and change["values"] is not None:
            rrs_el = etree.SubElement(rrset_el, "ResourceRecords")
            for val in change["values"]:
                rr_el = etree.SubElement(rrs_el, "ResourceRecord")
                etree.SubElement(rr_el, "Value").text = val

        if "alias_target" in change and change["alias_target"] is not None:
            alias = change["alias_target"]
            alias_el = etree.SubElement(rrset_el, "AliasTarget")
            etree.SubElement(alias_el, "HostedZoneId").text = alias["hosted_zone_id"]
            etree.SubElement(alias_el, "DNSName").text = alias["dns_name"]
            etree.SubElement(alias_el, "EvaluateTargetHealth").text = str(
                alias.get("evaluate_target_health", False)
            ).lower()

    return etree.tostring(root, xml_declaration=True, encoding="UTF-8")


def generate_change_id() -> str:
    """Generate a synthetic change ID for queued changes."""
    return f"BATCH-{uuid.uuid4().hex[:12]}"
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd ~/aws-route53-batch-proxy && source .venv/bin/activate && python3 -m pytest tests/test_route53_xml.py -v`
Expected: All 7 tests PASS.

- [ ] **Step 5: Commit**

```bash
git add src/route53_xml.py tests/test_route53_xml.py
git commit -m "Add Route53 XML parsing and generation"
```

---

### Task 3: Batcher — Redis queue and dedup logic

**Files:**
- Create: `src/batcher.py`
- Create: `tests/test_batcher.py`

- [ ] **Step 1: Write the failing tests**

```python
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
        assert len(z1) == 1
        assert len(z2) == 1
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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd ~/aws-route53-batch-proxy && source .venv/bin/activate && python3 -m pytest tests/test_batcher.py -v`
Expected: ImportError — `src.batcher` does not exist.

- [ ] **Step 3: Implement batcher.py**

```python
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

    async def enqueue(
        self, zone_id: str, changes: list[dict], change_id: str
    ) -> None:
        """Push changes onto the queue for a hosted zone."""
        pipe = self.redis.pipeline()
        for change in changes:
            change["_change_id"] = change_id
            pipe.rpush(f"{ZONE_KEY_PREFIX}{zone_id}:changes", json.dumps(change))
        pipe.sadd(ZONE_SET_KEY, zone_id)
        await pipe.execute()

    async def drain(self, zone_id: str) -> list[dict]:
        """Atomically drain all pending changes for a zone."""
        key = f"{ZONE_KEY_PREFIX}{zone_id}:changes"
        pipe = self.redis.pipeline()
        pipe.lrange(key, 0, -1)
        pipe.delete(key)
        results = await pipe.execute()
        raw_items = results[0]
        if not raw_items:
            # Remove from active set if empty
            await self.redis.srem(ZONE_SET_KEY, zone_id)
            return []
        return [json.loads(item) for item in raw_items]

    async def requeue(self, zone_id: str, changes: list[dict]) -> None:
        """Push changes back to the front of the queue (for retry on flush failure)."""
        key = f"{ZONE_KEY_PREFIX}{zone_id}:changes"
        pipe = self.redis.pipeline()
        for change in reversed(changes):
            pipe.lpush(key, json.dumps(change))
        pipe.sadd(ZONE_SET_KEY, zone_id)
        await pipe.execute()

    async def active_zones(self) -> list[str]:
        """Return list of zone IDs with pending changes."""
        members = await self.redis.smembers(ZONE_SET_KEY)
        return list(members)

    async def queue_depth(self, zone_id: str) -> int:
        """Return number of pending changes for a zone."""
        return await self.redis.llen(f"{ZONE_KEY_PREFIX}{zone_id}:changes")

    @staticmethod
    def dedup(changes: list[dict]) -> list[dict]:
        """Collapse exact duplicate changes, preserving order.

        Two changes are exact duplicates if they have the same action, name,
        type, and values/alias_target. Different actions on the same record
        (e.g. DELETE then CREATE) are preserved.
        """
        seen = set()
        result = []
        for change in changes:
            # Build a dedup key from the change fields (excluding _change_id)
            key_parts = (
                change.get("action"),
                change.get("name"),
                change.get("type"),
                tuple(change.get("values", []) or []),
                json.dumps(change.get("alias_target"), sort_keys=True)
                if change.get("alias_target")
                else None,
            )
            if key_parts not in seen:
                seen.add(key_parts)
                result.append(change)
        return result

    async def map_change_id(
        self, synthetic_id: str, real_id: str, ttl: int = 3600
    ) -> None:
        """Map a synthetic change ID to a real Route53 change ID."""
        await self.redis.set(f"change:{synthetic_id}", real_id, ex=ttl)

    async def get_real_change_id(self, synthetic_id: str) -> str | None:
        """Look up the real Route53 change ID for a synthetic ID."""
        return await self.redis.get(f"change:{synthetic_id}")

    async def get_fail_count(self, zone_id: str) -> int:
        """Get consecutive flush failure count for a zone."""
        val = await self.redis.get(f"{ZONE_KEY_PREFIX}{zone_id}:fail_count")
        return int(val) if val else 0

    async def incr_fail_count(self, zone_id: str) -> int:
        """Increment and return flush failure count."""
        return await self.redis.incr(f"{ZONE_KEY_PREFIX}{zone_id}:fail_count")

    async def reset_fail_count(self, zone_id: str) -> None:
        """Reset flush failure count on success."""
        await self.redis.delete(f"{ZONE_KEY_PREFIX}{zone_id}:fail_count")

    async def set_backoff(self, zone_id: str, seconds: float) -> None:
        """Set a backoff marker for a zone."""
        await self.redis.set(
            f"{ZONE_KEY_PREFIX}{zone_id}:backoff", "1", ex=int(seconds)
        )

    async def is_backed_off(self, zone_id: str) -> bool:
        """Check if a zone is in backoff."""
        return await self.redis.exists(f"{ZONE_KEY_PREFIX}{zone_id}:backoff") > 0
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd ~/aws-route53-batch-proxy && source .venv/bin/activate && python3 -m pytest tests/test_batcher.py -v`
Expected: All 12 tests PASS.

- [ ] **Step 5: Commit**

```bash
git add src/batcher.py tests/test_batcher.py
git commit -m "Add batcher: Redis queue, dedup, and zone management"
```

---

## Chunk 2: Flush Worker, Route53 Client, Metrics

### Task 4: Route53 client wrapper

**Files:**
- Create: `src/route53_client.py`

- [ ] **Step 1: Create route53_client.py**

```python
"""boto3 wrapper for real Route53 API calls."""

import logging

import boto3
from botocore.exceptions import ClientError

from src.config import config

logger = logging.getLogger(__name__)


class Route53Client:
    def __init__(self):
        self._client = None

    @property
    def client(self):
        if self._client is None:
            self._client = boto3.client(
                "route53",
                aws_access_key_id=config.AWS_ACCESS_KEY_ID,
                aws_secret_access_key=config.AWS_SECRET_ACCESS_KEY,
                region_name=config.AWS_REGION,
            )
        return self._client

    def change_resource_record_sets(
        self, zone_id: str, change_batch: dict
    ) -> dict:
        """Send a ChangeResourceRecordSets call to real Route53.

        Args:
            zone_id: The hosted zone ID (without /hostedzone/ prefix).
            change_batch: Dict with 'Changes' list in boto3 format.

        Returns:
            The ChangeInfo dict from the response.

        Raises:
            ClientError on Route53 API errors.
        """
        response = self.client.change_resource_record_sets(
            HostedZoneId=zone_id,
            ChangeBatch=change_batch,
        )
        return response.get("ChangeInfo", {})

    def get_change(self, change_id: str) -> dict:
        """Get the status of a Route53 change."""
        response = self.client.get_change(Id=change_id)
        return response.get("ChangeInfo", {})

    @staticmethod
    def changes_to_boto3(changes: list[dict]) -> dict:
        """Convert our internal change dicts to boto3 ChangeBatch format."""
        boto3_changes = []
        for change in changes:
            rrset: dict = {
                "Name": change["name"],
                "Type": change["type"],
            }
            if "ttl" in change and change["ttl"] is not None:
                rrset["TTL"] = change["ttl"]
            if "values" in change and change["values"] is not None:
                rrset["ResourceRecords"] = [
                    {"Value": v} for v in change["values"]
                ]
            if "alias_target" in change and change["alias_target"] is not None:
                alias = change["alias_target"]
                rrset["AliasTarget"] = {
                    "HostedZoneId": alias["hosted_zone_id"],
                    "DNSName": alias["dns_name"],
                    "EvaluateTargetHealth": alias.get(
                        "evaluate_target_health", False
                    ),
                }
            boto3_changes.append({
                "Action": change["action"],
                "ResourceRecordSet": rrset,
            })
        return {"Changes": boto3_changes}
```

- [ ] **Step 2: Commit**

```bash
git add src/route53_client.py
git commit -m "Add Route53 client wrapper with boto3 format conversion"
```

---

### Task 5: Prometheus metrics

**Files:**
- Create: `src/metrics.py`

- [ ] **Step 1: Create metrics.py**

```python
"""Prometheus metric definitions."""

from prometheus_client import Counter, Gauge, Histogram

CHANGES_QUEUED = Counter(
    "r53proxy_changes_queued_total",
    "Total changes queued",
    ["zone_id", "action"],
)

CHANGES_FLUSHED = Counter(
    "r53proxy_changes_flushed_total",
    "Total changes flushed to Route53",
    ["zone_id"],
)

FLUSH_BATCH_SIZE = Histogram(
    "r53proxy_flush_batch_size",
    "Number of changes per flush batch",
    buckets=[1, 5, 10, 25, 50, 100, 250, 500, 1000],
)

FLUSH_DURATION = Histogram(
    "r53proxy_flush_duration_seconds",
    "Time to send a batch to Route53",
    buckets=[0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0],
)

FLUSH_ERRORS = Counter(
    "r53proxy_flush_errors_total",
    "Failed flush attempts",
    ["zone_id"],
)

QUEUE_DEPTH = Gauge(
    "r53proxy_queue_depth",
    "Current queue depth",
    ["zone_id"],
)

PASSTHROUGH_TOTAL = Counter(
    "r53proxy_passthrough_total",
    "Pass-through requests",
    ["operation"],
)

REQUESTS_TOTAL = Counter(
    "r53proxy_requests_total",
    "Total incoming requests",
    ["operation"],
)
```

- [ ] **Step 2: Commit**

```bash
git add src/metrics.py
git commit -m "Add Prometheus metric definitions"
```

---

### Task 6: Flush worker with leader election

**Files:**
- Create: `src/flusher.py`
- Create: `tests/test_flusher.py`

- [ ] **Step 1: Write the failing tests**

```python
"""Tests for flush worker."""

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import pytest_asyncio
import fakeredis.aioredis

from src.batcher import Batcher
from src.flusher import Flusher


@pytest_asyncio.fixture
async def redis_client():
    client = fakeredis.aioredis.FakeRedis(decode_responses=True)
    yield client
    await client.aclose()


@pytest_asyncio.fixture
async def batcher(redis_client):
    return Batcher(redis_client)


@pytest_asyncio.fixture
async def flusher(redis_client, batcher):
    mock_r53 = MagicMock()
    mock_r53.change_resource_record_sets = MagicMock(
        return_value={"Id": "/change/REAL123", "Status": "PENDING"}
    )
    mock_r53.changes_to_boto3 = MagicMock(
        return_value={"Changes": []}
    )
    f = Flusher(redis_client, batcher, mock_r53)
    return f


@pytest.mark.asyncio
class TestFlushOnce:
    async def test_flushes_queued_changes(self, flusher, batcher):
        await batcher.enqueue(
            "Z1",
            [{"action": "DELETE", "name": "a.example.com.", "type": "A", "values": ["1.1.1.1"]}],
            "B1",
        )
        await flusher.flush_once()
        flusher.r53_client.change_resource_record_sets.assert_called_once()
        call_args = flusher.r53_client.change_resource_record_sets.call_args
        assert call_args[0][0] == "Z1"  # zone_id

    async def test_no_call_when_empty(self, flusher, batcher):
        await flusher.flush_once()
        flusher.r53_client.change_resource_record_sets.assert_not_called()

    async def test_deduplicates_before_flush(self, flusher, batcher):
        change = {"action": "DELETE", "name": "a.example.com.", "type": "A", "values": ["1.1.1.1"]}
        await batcher.enqueue("Z1", [change], "B1")
        await batcher.enqueue("Z1", [change], "B2")
        await flusher.flush_once()
        flusher.r53_client.changes_to_boto3.assert_called_once()
        call_args = flusher.r53_client.changes_to_boto3.call_args
        assert len(call_args[0][0]) == 1  # deduped to 1

    async def test_maps_change_ids_after_flush(self, flusher, batcher):
        await batcher.enqueue(
            "Z1",
            [{"action": "DELETE", "name": "a.example.com.", "type": "A", "values": ["1.1.1.1"]}],
            "BATCH-syn1",
        )
        await flusher.flush_once()
        real_id = await batcher.get_real_change_id("BATCH-syn1")
        assert real_id == "/change/REAL123"

    async def test_requeues_on_failure(self, flusher, batcher):
        from botocore.exceptions import ClientError

        flusher.r53_client.change_resource_record_sets.side_effect = ClientError(
            {"Error": {"Code": "Throttling", "Message": "Rate exceeded"}},
            "ChangeResourceRecordSets",
        )
        await batcher.enqueue(
            "Z1",
            [{"action": "DELETE", "name": "a.example.com.", "type": "A", "values": ["1.1.1.1"]}],
            "B1",
        )
        await flusher.flush_once()
        # Changes should be back in the queue
        depth = await batcher.queue_depth("Z1")
        assert depth == 1

    async def test_skips_backed_off_zone(self, flusher, batcher):
        await batcher.enqueue(
            "Z1",
            [{"action": "DELETE", "name": "a.example.com.", "type": "A", "values": ["1.1.1.1"]}],
            "B1",
        )
        await batcher.set_backoff("Z1", 60)
        await flusher.flush_once()
        flusher.r53_client.change_resource_record_sets.assert_not_called()

    async def test_flushes_multiple_zones(self, flusher, batcher):
        await batcher.enqueue("Z1", [{"action": "DELETE", "name": "a.example.com.", "type": "A", "values": ["1.1.1.1"]}], "B1")
        await batcher.enqueue("Z2", [{"action": "DELETE", "name": "b.example.com.", "type": "A", "values": ["2.2.2.2"]}], "B2")
        await flusher.flush_once()
        assert flusher.r53_client.change_resource_record_sets.call_count == 2


@pytest.mark.asyncio
class TestLeaderElection:
    async def test_acquires_lock(self, flusher, redis_client):
        acquired = await flusher.try_acquire_leader()
        assert acquired is True

    async def test_lock_prevents_second_leader(self, flusher, redis_client):
        await flusher.try_acquire_leader()
        flusher2 = Flusher(redis_client, Batcher(redis_client), MagicMock())
        acquired = await flusher2.try_acquire_leader()
        assert acquired is False
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd ~/aws-route53-batch-proxy && source .venv/bin/activate && python3 -m pytest tests/test_flusher.py -v`
Expected: ImportError — `src.flusher` does not exist.

- [ ] **Step 3: Implement flusher.py**

```python
"""Flush worker — leader election, batch sending to Route53."""

import asyncio
import logging
import time

from botocore.exceptions import ClientError

from src.batcher import Batcher
from src.config import config
from src.metrics import (
    CHANGES_FLUSHED,
    FLUSH_BATCH_SIZE,
    FLUSH_DURATION,
    FLUSH_ERRORS,
    QUEUE_DEPTH,
)
from src.route53_client import Route53Client

logger = logging.getLogger(__name__)

LEADER_KEY = "flush:leader"


class Flusher:
    def __init__(self, redis, batcher: Batcher, r53_client: Route53Client):
        self.redis = redis
        self.batcher = batcher
        self.r53_client = r53_client
        self._running = False
        self._task: asyncio.Task | None = None

    async def try_acquire_leader(self) -> bool:
        """Try to acquire the flush leader lock."""
        result = await self.redis.set(
            LEADER_KEY, "1", nx=True, ex=config.LEADER_LOCK_TTL
        )
        return result is not None and result is not False

    async def renew_leader(self) -> bool:
        """Renew the leader lock TTL."""
        return await self.redis.expire(LEADER_KEY, config.LEADER_LOCK_TTL)

    async def flush_once(self) -> None:
        """Run one flush cycle across all active zones."""
        zones = await self.batcher.active_zones()
        for zone_id in zones:
            if await self.batcher.is_backed_off(zone_id):
                logger.debug("Zone %s is in backoff, skipping", zone_id)
                continue
            await self._flush_zone(zone_id)

    async def _flush_zone(self, zone_id: str) -> None:
        """Flush pending changes for a single zone."""
        changes = await self.batcher.drain(zone_id)
        if not changes:
            return

        deduped = self.batcher.dedup(changes)
        change_ids = set(c.get("_change_id", "") for c in deduped if c.get("_change_id"))

        # Strip internal fields before sending
        clean = [{k: v for k, v in c.items() if not k.startswith("_")} for c in deduped]

        # Chunk into batches
        for i in range(0, len(clean), config.MAX_BATCH_SIZE):
            batch = clean[i : i + config.MAX_BATCH_SIZE]
            batch_changes = deduped[i : i + config.MAX_BATCH_SIZE]

            FLUSH_BATCH_SIZE.observe(len(batch))
            start = time.monotonic()

            try:
                boto3_batch = self.r53_client.changes_to_boto3(batch)
                result = self.r53_client.change_resource_record_sets(
                    zone_id, boto3_batch
                )
                duration = time.monotonic() - start
                FLUSH_DURATION.observe(duration)
                CHANGES_FLUSHED.labels(zone_id=zone_id).inc(len(batch))

                # Map synthetic change IDs to real change ID
                real_id = result.get("Id", "")
                for cid in change_ids:
                    await self.batcher.map_change_id(cid, real_id)

                await self.batcher.reset_fail_count(zone_id)
                logger.info(
                    "Flushed %d changes for zone %s in %.2fs (real_id=%s)",
                    len(batch),
                    zone_id,
                    duration,
                    real_id,
                )

            except ClientError as e:
                duration = time.monotonic() - start
                FLUSH_DURATION.observe(duration)
                FLUSH_ERRORS.labels(zone_id=zone_id).inc()
                fail_count = await self.batcher.incr_fail_count(zone_id)

                logger.error(
                    "Flush failed for zone %s (attempt %d): %s",
                    zone_id,
                    fail_count,
                    e,
                )

                # Requeue for retry
                await self.batcher.requeue(zone_id, batch_changes)

                if fail_count >= config.MAX_FLUSH_FAILURES:
                    logger.error(
                        "Zone %s hit %d consecutive failures, backing off %ds",
                        zone_id,
                        fail_count,
                        config.FLUSH_BACKOFF_SECONDS,
                    )
                    await self.batcher.set_backoff(
                        zone_id, config.FLUSH_BACKOFF_SECONDS
                    )
                    await self.batcher.reset_fail_count(zone_id)
                break  # Don't continue with remaining chunks on failure

        # Update queue depth metric
        depth = await self.batcher.queue_depth(zone_id)
        QUEUE_DEPTH.labels(zone_id=zone_id).set(depth)

    async def run(self) -> None:
        """Main flush loop. Tries to become leader and flushes periodically."""
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

    def start(self) -> None:
        """Start the flush worker as a background task."""
        self._task = asyncio.create_task(self.run())

    async def stop(self) -> None:
        """Stop the flush worker."""
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd ~/aws-route53-batch-proxy && source .venv/bin/activate && python3 -m pytest tests/test_flusher.py -v`
Expected: All 9 tests PASS.

- [ ] **Step 5: Commit**

```bash
git add src/flusher.py tests/test_flusher.py
git commit -m "Add flush worker with leader election and failure handling"
```

---

## Chunk 3: Proxy Router, FastAPI App, Integration Tests

### Task 7: Proxy router — intercept vs pass-through

**Files:**
- Create: `src/proxy.py`

- [ ] **Step 1: Create proxy.py**

```python
"""Request routing — intercept ChangeResourceRecordSets, pass through everything else."""

import logging
import re

from fastapi import Request, Response

from src.batcher import Batcher
from src.metrics import CHANGES_QUEUED, PASSTHROUGH_TOTAL, REQUESTS_TOTAL
from src.route53_client import Route53Client
from src.route53_xml import (
    build_change_response,
    generate_change_id,
    parse_change_batch,
)

logger = logging.getLogger(__name__)

# Route53 REST URL patterns
# POST /2013-04-01/hostedzone/{zone_id}/rrset/ -> ChangeResourceRecordSets
CHANGE_RRSET_PATTERN = re.compile(
    r"^/2013-04-01/hostedzone/([^/]+)/rrset/?$"
)
# GET /2013-04-01/change/{change_id} -> GetChange
GET_CHANGE_PATTERN = re.compile(
    r"^/2013-04-01/change/([^/]+)$"
)


class Proxy:
    def __init__(self, batcher: Batcher, r53_client: Route53Client):
        self.batcher = batcher
        self.r53_client = r53_client

    async def handle_request(self, request: Request) -> Response:
        """Route an incoming request to the appropriate handler."""
        path = request.url.path
        method = request.method

        # ChangeResourceRecordSets — intercept and batch
        match = CHANGE_RRSET_PATTERN.match(path)
        if match and method == "POST":
            zone_id = match.group(1)
            REQUESTS_TOTAL.labels(operation="ChangeResourceRecordSets").inc()
            return await self._handle_change(request, zone_id)

        # GetChange — map synthetic IDs to real IDs
        match = GET_CHANGE_PATTERN.match(path)
        if match and method == "GET":
            change_id = match.group(1)
            REQUESTS_TOTAL.labels(operation="GetChange").inc()
            return await self._handle_get_change(change_id)

        # Everything else — pass through
        REQUESTS_TOTAL.labels(operation="passthrough").inc()
        PASSTHROUGH_TOTAL.labels(operation=self._guess_operation(path, method)).inc()
        return await self._handle_passthrough(request)

    async def _handle_change(self, request: Request, zone_id: str) -> Response:
        """Queue a ChangeResourceRecordSets request."""
        body = await request.body()
        try:
            changes = parse_change_batch(body)
        except Exception:
            logger.exception("Failed to parse ChangeResourceRecordSets XML")
            return Response(
                content="<ErrorResponse><Error><Code>InvalidInput</Code>"
                "<Message>Failed to parse request XML</Message></Error></ErrorResponse>",
                status_code=400,
                media_type="application/xml",
            )

        if not changes:
            return Response(
                content="<ErrorResponse><Error><Code>InvalidInput</Code>"
                "<Message>No changes in request</Message></Error></ErrorResponse>",
                status_code=400,
                media_type="application/xml",
            )

        change_id = generate_change_id()

        # Try to queue in Redis; fall back to direct call if Redis is unavailable
        try:
            await self.batcher.enqueue(zone_id, changes, change_id)
            for change in changes:
                CHANGES_QUEUED.labels(
                    zone_id=zone_id, action=change["action"]
                ).inc()
            logger.info(
                "Queued %d changes for zone %s (change_id=%s)",
                len(changes),
                zone_id,
                change_id,
            )
        except Exception:
            logger.warning(
                "Redis unavailable, falling back to direct Route53 call "
                "for zone %s",
                zone_id,
                exc_info=True,
            )
            return await self._direct_change(zone_id, changes)

        response_xml = build_change_response(change_id)
        return Response(
            content=response_xml,
            status_code=200,
            media_type="application/xml",
        )

    async def _direct_change(
        self, zone_id: str, changes: list[dict]
    ) -> Response:
        """Send changes directly to Route53 (fallback when Redis is down)."""
        try:
            clean = [
                {k: v for k, v in c.items() if not k.startswith("_")}
                for c in changes
            ]
            boto3_batch = self.r53_client.changes_to_boto3(clean)
            result = self.r53_client.change_resource_record_sets(
                zone_id, boto3_batch
            )
            real_id = result.get("Id", "unknown")
            response_xml = build_change_response(real_id.split("/")[-1])
            return Response(
                content=response_xml,
                status_code=200,
                media_type="application/xml",
            )
        except Exception:
            logger.exception("Direct Route53 call failed for zone %s", zone_id)
            return Response(
                content="<ErrorResponse><Error><Code>ServiceUnavailable</Code>"
                "<Message>Route53 call failed</Message></Error></ErrorResponse>",
                status_code=503,
                media_type="application/xml",
            )

    async def _handle_get_change(self, change_id: str) -> Response:
        """Handle GetChange — map synthetic ID to real ID."""
        # Check if this is a synthetic ID
        if change_id.startswith("BATCH-"):
            real_id = await self.batcher.get_real_change_id(change_id)
            if real_id is None:
                # Not flushed yet — return PENDING
                response_xml = build_change_response(change_id)
                return Response(
                    content=response_xml,
                    status_code=200,
                    media_type="application/xml",
                )
            change_id = real_id.split("/")[-1]

        # Pass through to real Route53
        try:
            result = self.r53_client.get_change(change_id)
            status = result.get("Status", "PENDING")
            xml = f"""<?xml version="1.0" encoding="UTF-8"?>
<GetChangeResponse xmlns="https://route53.amazonaws.com/doc/2013-04-01/">
  <ChangeInfo>
    <Id>/change/{change_id}</Id>
    <Status>{status}</Status>
    <SubmittedAt>{result.get('SubmittedAt', '')}</SubmittedAt>
  </ChangeInfo>
</GetChangeResponse>"""
            return Response(
                content=xml, status_code=200, media_type="application/xml"
            )
        except Exception:
            logger.exception("GetChange failed for %s", change_id)
            return Response(
                content="<ErrorResponse><Error><Code>ServiceUnavailable</Code>"
                "<Message>GetChange failed</Message></Error></ErrorResponse>",
                status_code=503,
                media_type="application/xml",
            )

    async def _handle_passthrough(self, request: Request) -> Response:
        """Pass through any non-intercepted request to real Route53.

        This is a best-effort pass-through. For operations we don't explicitly
        handle, we return 501 rather than trying to proxy arbitrary requests.
        """
        logger.warning(
            "Unhandled Route53 operation: %s %s", request.method, request.url.path
        )
        return Response(
            content="<ErrorResponse><Error><Code>NotImplemented</Code>"
            "<Message>This Route53 operation is not proxied</Message></Error></ErrorResponse>",
            status_code=501,
            media_type="application/xml",
        )

    @staticmethod
    def _guess_operation(path: str, method: str) -> str:
        """Best-effort guess at the Route53 operation name for metrics."""
        if "/hostedzone" in path and method == "GET":
            if "/rrset" in path:
                return "ListResourceRecordSets"
            return "GetHostedZone"
        if "/hostedzone" in path and method == "POST":
            return "CreateHostedZone"
        return f"{method}:{path[:50]}"
```

- [ ] **Step 2: Commit**

```bash
git add src/proxy.py
git commit -m "Add proxy router: intercept changes, pass-through, Redis fallback"
```

---

### Task 8: FastAPI application

**Files:**
- Create: `src/app.py`

- [ ] **Step 1: Create app.py**

```python
"""FastAPI app, lifespan hooks, health and status endpoints."""

import logging
from contextlib import asynccontextmanager

import redis.asyncio as aioredis
from fastapi import FastAPI, Request, Response
from prometheus_client import generate_latest, CONTENT_TYPE_LATEST

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
    """Startup and shutdown hooks."""
    # Startup
    logger.info("Starting Route53 Batch Proxy")
    redis_client = aioredis.from_url(
        config.REDIS_URL, decode_responses=True
    )
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

    # Shutdown
    logger.info("Shutting down")
    await flusher.stop()
    await redis_client.aclose()


app = FastAPI(title="Route53 Batch Proxy", lifespan=lifespan)


@app.api_route(
    "/{path:path}",
    methods=["GET", "POST", "PUT", "DELETE", "PATCH"],
)
async def catch_all(request: Request) -> Response:
    """Catch-all route that delegates to the proxy router."""
    return await request.app.state.proxy.handle_request(request)


@app.get("/health")
async def health():
    """Liveness probe."""
    return {"status": "ok"}


@app.get("/ready")
async def ready(request: Request):
    """Readiness probe — checks Redis connectivity."""
    try:
        await request.app.state.redis.ping()
        return {"status": "ready"}
    except Exception:
        return Response(
            content='{"status": "not ready", "reason": "redis unavailable"}',
            status_code=503,
            media_type="application/json",
        )


@app.get("/metrics")
async def metrics():
    """Prometheus metrics endpoint."""
    return Response(
        content=generate_latest(),
        media_type=CONTENT_TYPE_LATEST,
    )


@app.get("/status")
async def status(request: Request):
    """JSON status: queue depths, flush stats, leader status."""
    batcher: Batcher = request.app.state.batcher
    zones = await batcher.active_zones()
    zone_depths = {}
    for zone_id in zones:
        depth = await batcher.queue_depth(zone_id)
        zone_depths[zone_id] = depth
        QUEUE_DEPTH.labels(zone_id=zone_id).set(depth)

    flusher: Flusher = request.app.state.flusher
    is_leader = await flusher.try_acquire_leader()

    return {
        "active_zones": len(zones),
        "queue_depths": zone_depths,
        "total_pending": sum(zone_depths.values()),
        "is_flush_leader": is_leader,
    }
```

- [ ] **Step 2: Commit**

```bash
git add src/app.py
git commit -m "Add FastAPI app with lifespan, health, metrics, and status"
```

---

### Task 9: Integration tests

**Files:**
- Create: `tests/test_proxy.py`

- [ ] **Step 1: Write integration tests**

```python
"""Integration tests — full HTTP request flow."""

import pytest
import pytest_asyncio
import fakeredis.aioredis
from unittest.mock import MagicMock

from fastapi.testclient import TestClient
from fastapi import FastAPI, Request, Response

from src.batcher import Batcher
from src.proxy import Proxy
from src.route53_client import Route53Client


@pytest.fixture
def mock_r53():
    client = MagicMock(spec=Route53Client)
    client.change_resource_record_sets = MagicMock(
        return_value={"Id": "/change/REAL123", "Status": "PENDING"}
    )
    client.changes_to_boto3 = Route53Client.changes_to_boto3
    client.get_change = MagicMock(
        return_value={"Id": "/change/REAL123", "Status": "INSYNC", "SubmittedAt": "2026-03-11T00:00:00Z"}
    )
    return client


@pytest.fixture
def test_app(mock_r53):
    redis_client = fakeredis.aioredis.FakeRedis(decode_responses=True)
    batcher = Batcher(redis_client)
    proxy = Proxy(batcher, mock_r53)

    app = FastAPI()
    app.state.redis = redis_client
    app.state.batcher = batcher
    app.state.proxy = proxy

    @app.api_route("/{path:path}", methods=["GET", "POST", "PUT", "DELETE", "PATCH"])
    async def catch_all(request: Request) -> Response:
        return await request.app.state.proxy.handle_request(request)

    @app.get("/health")
    async def health():
        return {"status": "ok"}

    @app.get("/ready")
    async def ready(request: Request):
        try:
            await request.app.state.redis.ping()
            return {"status": "ready"}
        except Exception:
            return Response(status_code=503)

    return app


@pytest.fixture
def client(test_app):
    return TestClient(test_app)


SAMPLE_CHANGE_XML = """<?xml version="1.0" encoding="UTF-8"?>
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
</ChangeResourceRecordSetsRequest>"""


class TestChangeResourceRecordSets:
    def test_queues_change_returns_200(self, client):
        resp = client.post(
            "/2013-04-01/hostedzone/Z123/rrset/",
            content=SAMPLE_CHANGE_XML,
            headers={"Content-Type": "application/xml"},
        )
        assert resp.status_code == 200
        assert "BATCH-" in resp.text
        assert "PENDING" in resp.text

    def test_invalid_xml_returns_400(self, client):
        resp = client.post(
            "/2013-04-01/hostedzone/Z123/rrset/",
            content=b"not xml",
            headers={"Content-Type": "application/xml"},
        )
        assert resp.status_code == 400

    def test_empty_changes_returns_400(self, client):
        xml = """<?xml version="1.0" encoding="UTF-8"?>
        <ChangeResourceRecordSetsRequest xmlns="https://route53.amazonaws.com/doc/2013-04-01/">
          <ChangeBatch><Changes></Changes></ChangeBatch>
        </ChangeResourceRecordSetsRequest>"""
        resp = client.post(
            "/2013-04-01/hostedzone/Z123/rrset/",
            content=xml,
            headers={"Content-Type": "application/xml"},
        )
        assert resp.status_code == 400

    def test_multiple_changes_queued(self, client, test_app):
        xml = """<?xml version="1.0" encoding="UTF-8"?>
        <ChangeResourceRecordSetsRequest xmlns="https://route53.amazonaws.com/doc/2013-04-01/">
          <ChangeBatch>
            <Changes>
              <Change>
                <Action>DELETE</Action>
                <ResourceRecordSet>
                  <Name>api.g1.example.com.</Name><Type>A</Type>
                  <TTL>300</TTL>
                  <ResourceRecords><ResourceRecord><Value>10.0.1.1</Value></ResourceRecord></ResourceRecords>
                </ResourceRecordSet>
              </Change>
              <Change>
                <Action>DELETE</Action>
                <ResourceRecordSet>
                  <Name>apps.g1.example.com.</Name><Type>A</Type>
                  <TTL>300</TTL>
                  <ResourceRecords><ResourceRecord><Value>10.0.1.2</Value></ResourceRecord></ResourceRecords>
                </ResourceRecordSet>
              </Change>
            </Changes>
          </ChangeBatch>
        </ChangeResourceRecordSetsRequest>"""
        resp = client.post(
            "/2013-04-01/hostedzone/Z123/rrset/",
            content=xml,
            headers={"Content-Type": "application/xml"},
        )
        assert resp.status_code == 200


class TestGetChange:
    def test_synthetic_id_not_flushed_returns_pending(self, client):
        # First queue a change to create the synthetic ID
        resp = client.post(
            "/2013-04-01/hostedzone/Z123/rrset/",
            content=SAMPLE_CHANGE_XML,
            headers={"Content-Type": "application/xml"},
        )
        # Extract the change ID from response
        import re
        match = re.search(r"(BATCH-[a-f0-9]+)", resp.text)
        assert match
        change_id = match.group(1)

        # GetChange on the synthetic ID before flush
        resp = client.get(f"/2013-04-01/change/{change_id}")
        assert resp.status_code == 200
        assert "PENDING" in resp.text


class TestPassthrough:
    def test_unknown_operation_returns_501(self, client):
        resp = client.get("/2013-04-01/hostedzone/Z123")
        assert resp.status_code == 501

    def test_list_rrsets_returns_501(self, client):
        resp = client.get("/2013-04-01/hostedzone/Z123/rrset")
        assert resp.status_code == 501


class TestHealth:
    def test_health(self, client):
        resp = client.get("/health")
        assert resp.status_code == 200

    def test_ready(self, client):
        resp = client.get("/ready")
        assert resp.status_code == 200
```

- [ ] **Step 2: Run all tests**

Run: `cd ~/aws-route53-batch-proxy && source .venv/bin/activate && python3 -m pytest tests/ -v`
Expected: All tests PASS.

- [ ] **Step 3: Commit**

```bash
git add tests/test_proxy.py
git commit -m "Add integration tests for proxy HTTP endpoints"
```

---

## Chunk 4: Dockerfile, Deployment Playbook, Manifests

### Task 10: Dockerfile

**Files:**
- Create: `Dockerfile`

- [ ] **Step 1: Create Dockerfile**

```dockerfile
FROM registry.access.redhat.com/ubi9/python-312:latest AS builder

USER 0
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY src/ /opt/app-root/src/src/

USER 1001

FROM builder AS verify
COPY tests/ /opt/app-root/src/tests/
RUN python -m pytest tests/ -v --tb=short

FROM builder AS runtime
WORKDIR /opt/app-root/src
ENV PYTHONPATH=/opt/app-root/src
ENV PORT=8443
ENV WORKERS=4
EXPOSE 8443

CMD ["sh", "-c", "uvicorn src.app:app --host 0.0.0.0 --port ${PORT} --workers ${WORKERS}"]
```

- [ ] **Step 2: Commit**

```bash
git add Dockerfile
git commit -m "Add multi-stage UBI 9 Dockerfile with test verification"
```

---

### Task 11: Ansible deployment playbook and manifests

**Files:**
- Create: `playbooks/deploy.yaml`
- Create: `playbooks/vars/common.yml`
- Create: `playbooks/vars/dev.yml.example`
- Create: `playbooks/vars/prod.yml.example`
- Create: `playbooks/templates/manifests.yaml.j2`

- [ ] **Step 1: Create playbooks/vars/common.yml**

```yaml
---
app_name: route53-batch-proxy
image_name: route53-batch-proxy
redis_name: route53-batch-proxy-redis

proxy_replicas: 2
proxy_cpu_request: "2"
proxy_cpu_limit: "4"
proxy_memory_request: "2Gi"
proxy_memory_limit: "4Gi"
proxy_workers: 4
proxy_port: 8443

redis_cpu_request: "1"
redis_cpu_limit: "2"
redis_memory_request: "2Gi"
redis_memory_limit: "4Gi"
redis_storage: "5Gi"

flush_interval_seconds: "2"
max_batch_size: "1000"
flush_backoff_seconds: "30"
log_level: "INFO"

github_repo: "https://github.com/rhpds/aws-route53-batch-proxy.git"
```

- [ ] **Step 2: Create playbooks/vars/dev.yml.example**

```yaml
---
env: dev
namespace: route53-batch-proxy-dev
git_ref: main
aws_access_key_id: "AKIA..."
aws_secret_access_key: "..."
kubeconfig: "~/secrets/parsec-ocpv-infra01.dal12.infra.demo.redhat.com.kubeconfig"
```

- [ ] **Step 3: Create playbooks/vars/prod.yml.example**

```yaml
---
env: prod
namespace: route53-batch-proxy
git_ref: production
aws_access_key_id: "AKIA..."
aws_secret_access_key: "..."
kubeconfig: "~/secrets/parsec-ocpv-infra01.dal12.infra.demo.redhat.com.kubeconfig"
```

- [ ] **Step 4: Create playbooks/templates/manifests.yaml.j2**

```yaml
---
apiVersion: image.openshift.io/v1
kind: ImageStream
metadata:
  name: {{ app_name }}
  namespace: {{ namespace }}
---
apiVersion: build.openshift.io/v1
kind: BuildConfig
metadata:
  name: {{ app_name }}
  namespace: {{ namespace }}
spec:
  source:
    type: Git
    git:
      uri: {{ github_repo }}
      ref: {{ git_ref }}
  strategy:
    type: Docker
    dockerStrategy:
      dockerfilePath: Dockerfile
  output:
    to:
      kind: ImageStreamTag
      name: {{ app_name }}:latest
  triggers:
  - type: GitHub
    github:
      secretReference:
        name: {{ app_name }}-webhook-secret
  - type: ConfigChange
---
apiVersion: v1
kind: ConfigMap
metadata:
  name: {{ app_name }}-config
  namespace: {{ namespace }}
data:
  FLUSH_INTERVAL_SECONDS: "{{ flush_interval_seconds }}"
  MAX_BATCH_SIZE: "{{ max_batch_size }}"
  FLUSH_BACKOFF_SECONDS: "{{ flush_backoff_seconds }}"
  LOG_LEVEL: "{{ log_level }}"
  REDIS_URL: "redis://{{ redis_name }}:6379"
  PORT: "{{ proxy_port }}"
  WORKERS: "{{ proxy_workers }}"
---
apiVersion: apps/v1
kind: Deployment
metadata:
  name: {{ app_name }}
  namespace: {{ namespace }}
  annotations:
    image.openshift.io/triggers: >-
      [{"from":{"kind":"ImageStreamTag","name":"{{ app_name }}:latest"},"fieldPath":"spec.template.spec.containers[?(@.name==\"{{ app_name }}\")].image"}]
spec:
  replicas: {{ proxy_replicas }}
  selector:
    matchLabels:
      app: {{ app_name }}
  template:
    metadata:
      labels:
        app: {{ app_name }}
    spec:
      affinity:
        podAntiAffinity:
          preferredDuringSchedulingIgnoredDuringExecution:
          - weight: 100
            podAffinityTerm:
              labelSelector:
                matchExpressions:
                - key: app
                  operator: In
                  values:
                  - {{ app_name }}
              topologyKey: kubernetes.io/hostname
      containers:
      - name: {{ app_name }}
        image: {{ app_name }}:latest
        ports:
        - containerPort: {{ proxy_port }}
          protocol: TCP
        envFrom:
        - configMapRef:
            name: {{ app_name }}-config
        - secretRef:
            name: {{ app_name }}-aws-creds
        resources:
          requests:
            cpu: "{{ proxy_cpu_request }}"
            memory: "{{ proxy_memory_request }}"
          limits:
            cpu: "{{ proxy_cpu_limit }}"
            memory: "{{ proxy_memory_limit }}"
        livenessProbe:
          httpGet:
            path: /health
            port: {{ proxy_port }}
          initialDelaySeconds: 10
          periodSeconds: 30
        readinessProbe:
          httpGet:
            path: /ready
            port: {{ proxy_port }}
          initialDelaySeconds: 5
          periodSeconds: 10
---
apiVersion: v1
kind: Service
metadata:
  name: {{ app_name }}
  namespace: {{ namespace }}
spec:
  selector:
    app: {{ app_name }}
  ports:
  - port: {{ proxy_port }}
    targetPort: {{ proxy_port }}
    protocol: TCP
---
apiVersion: apps/v1
kind: Deployment
metadata:
  name: {{ redis_name }}
  namespace: {{ namespace }}
spec:
  replicas: 1
  selector:
    matchLabels:
      app: {{ redis_name }}
  template:
    metadata:
      labels:
        app: {{ redis_name }}
    spec:
      containers:
      - name: redis
        image: registry.redhat.io/rhel9/redis-7:latest
        ports:
        - containerPort: 6379
          protocol: TCP
        args:
        - --maxmemory
        - "1gb"
        - --maxmemory-policy
        - allkeys-lru
        - --save
        - ""
        - --appendonly
        - "yes"
        resources:
          requests:
            cpu: "{{ redis_cpu_request }}"
            memory: "{{ redis_memory_request }}"
          limits:
            cpu: "{{ redis_cpu_limit }}"
            memory: "{{ redis_memory_limit }}"
        volumeMounts:
        - name: redis-data
          mountPath: /var/lib/redis/data
        livenessProbe:
          exec:
            command:
            - redis-cli
            - ping
          initialDelaySeconds: 10
          periodSeconds: 30
        readinessProbe:
          exec:
            command:
            - redis-cli
            - ping
          initialDelaySeconds: 5
          periodSeconds: 10
      volumes:
      - name: redis-data
        persistentVolumeClaim:
          claimName: {{ redis_name }}-data
---
apiVersion: v1
kind: PersistentVolumeClaim
metadata:
  name: {{ redis_name }}-data
  namespace: {{ namespace }}
spec:
  accessModes:
  - ReadWriteOnce
  resources:
    requests:
      storage: {{ redis_storage }}
---
apiVersion: v1
kind: Service
metadata:
  name: {{ redis_name }}
  namespace: {{ namespace }}
spec:
  selector:
    app: {{ redis_name }}
  ports:
  - port: 6379
    targetPort: 6379
    protocol: TCP
---
apiVersion: policy/v1
kind: PodDisruptionBudget
metadata:
  name: {{ app_name }}
  namespace: {{ namespace }}
spec:
  minAvailable: 1
  selector:
    matchLabels:
      app: {{ app_name }}
---
apiVersion: networking.k8s.io/v1
kind: NetworkPolicy
metadata:
  name: {{ app_name }}-allow-aap
  namespace: {{ namespace }}
spec:
  podSelector:
    matchLabels:
      app: {{ app_name }}
  policyTypes:
  - Ingress
  ingress:
  - ports:
    - port: {{ proxy_port }}
      protocol: TCP
```

- [ ] **Step 5: Create playbooks/deploy.yaml**

```yaml
---
- name: Deploy Route53 Batch Proxy
  hosts: localhost
  connection: local
  gather_facts: false
  vars_files:
  - vars/common.yml
  - "vars/{{ env }}.yml"

  tasks:
  - name: Create namespace
    kubernetes.core.k8s:
      kubeconfig: "{{ kubeconfig }}"
      state: present
      definition:
        apiVersion: v1
        kind: Namespace
        metadata:
          name: "{{ namespace }}"

  - name: Create AWS credentials secret
    kubernetes.core.k8s:
      kubeconfig: "{{ kubeconfig }}"
      state: present
      definition:
        apiVersion: v1
        kind: Secret
        metadata:
          name: "{{ app_name }}-aws-creds"
          namespace: "{{ namespace }}"
        type: Opaque
        stringData:
          AWS_ACCESS_KEY_ID: "{{ aws_access_key_id }}"
          AWS_SECRET_ACCESS_KEY: "{{ aws_secret_access_key }}"
    tags: [secrets]

  - name: Create webhook secret
    kubernetes.core.k8s:
      kubeconfig: "{{ kubeconfig }}"
      state: present
      definition:
        apiVersion: v1
        kind: Secret
        metadata:
          name: "{{ app_name }}-webhook-secret"
          namespace: "{{ namespace }}"
        type: Opaque
        stringData:
          WebHookSecretKey: "{{ webhook_secret | default(lookup('password', '/dev/null length=32 chars=hexdigits')) }}"
    tags: [secrets]

  - name: Apply manifests
    kubernetes.core.k8s:
      kubeconfig: "{{ kubeconfig }}"
      state: present
      definition: "{{ lookup('template', 'templates/manifests.yaml.j2') | from_yaml_all }}"
    tags: [apply]
```

- [ ] **Step 6: Commit**

```bash
git add playbooks/
git commit -m "Add Ansible deployment playbook and OpenShift manifests"
```

---

### Task 12: README

**Files:**
- Create: `README.md`

- [ ] **Step 1: Create README.md**

```markdown
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
| `LOG_LEVEL` | `INFO` | Log level |
```

- [ ] **Step 2: Commit**

```bash
git add README.md
git commit -m "Add README with setup and deployment instructions"
```

---

Plan complete and saved to `docs/superpowers/plans/2026-03-11-route53-batch-proxy.md`. Ready to execute?