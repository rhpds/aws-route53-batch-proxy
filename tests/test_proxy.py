"""Integration tests — full HTTP request flow."""

import re
import pytest
import fakeredis.aioredis
from unittest.mock import AsyncMock, MagicMock, patch
from fastapi.testclient import TestClient
from fastapi import FastAPI, Request, Response
from src.batcher import Batcher
from src.proxy import Proxy
from src.route53_client import Route53Client


@pytest.fixture
def mock_r53():
    client = MagicMock(spec=Route53Client)
    client.change_resource_record_sets = MagicMock(return_value={"Id": "/change/REAL123", "Status": "PENDING"})
    client.changes_to_boto3 = Route53Client.changes_to_boto3
    client.get_change = MagicMock(return_value={"Id": "/change/REAL123", "Status": "INSYNC", "SubmittedAt": "2026-03-11T00:00:00Z"})
    return client


@pytest.fixture
def test_app(mock_r53):
    redis_client = fakeredis.aioredis.FakeRedis(decode_responses=True, connected=True)
    batcher = Batcher(redis_client)
    proxy = Proxy(batcher, mock_r53)
    app = FastAPI()
    app.state.redis = redis_client
    app.state.batcher = batcher
    app.state.proxy = proxy

    @app.get("/health")
    async def health():
        return {"status": "ok"}

    @app.get("/ready")
    async def ready():
        return {"status": "ready"}

    @app.api_route("/{path:path}", methods=["GET", "POST", "PUT", "DELETE", "PATCH"])
    async def catch_all(request: Request) -> Response:
        return await request.app.state.proxy.handle_request(request)

    return app


@pytest.fixture
def client(test_app):
    return TestClient(test_app)


SAMPLE_CHANGE_XML = """<?xml version="1.0" encoding="UTF-8"?>
<ChangeResourceRecordSetsRequest xmlns="https://route53.amazonaws.com/doc/2013-04-01/">
  <ChangeBatch><Changes><Change>
    <Action>DELETE</Action>
    <ResourceRecordSet>
      <Name>api.guid123.dynamic.redhatworkshops.io.</Name>
      <Type>A</Type><TTL>300</TTL>
      <ResourceRecords><ResourceRecord><Value>10.0.1.5</Value></ResourceRecord></ResourceRecords>
    </ResourceRecordSet>
  </Change></Changes></ChangeBatch>
</ChangeResourceRecordSetsRequest>"""


class TestChangeResourceRecordSets:
    def test_queues_change_returns_200(self, client):
        resp = client.post("/2013-04-01/hostedzone/Z123/rrset/", content=SAMPLE_CHANGE_XML, headers={"Content-Type": "application/xml"})
        assert resp.status_code == 200
        assert "BATCH-" in resp.text
        assert "PENDING" in resp.text

    def test_invalid_xml_returns_400(self, client):
        resp = client.post("/2013-04-01/hostedzone/Z123/rrset/", content=b"not xml", headers={"Content-Type": "application/xml"})
        assert resp.status_code == 400

    def test_empty_changes_returns_400(self, client):
        xml = '<?xml version="1.0" encoding="UTF-8"?><ChangeResourceRecordSetsRequest xmlns="https://route53.amazonaws.com/doc/2013-04-01/"><ChangeBatch><Changes></Changes></ChangeBatch></ChangeResourceRecordSetsRequest>'
        resp = client.post("/2013-04-01/hostedzone/Z123/rrset/", content=xml, headers={"Content-Type": "application/xml"})
        assert resp.status_code == 400

    def test_multiple_changes_queued(self, client):
        xml = """<?xml version="1.0" encoding="UTF-8"?>
        <ChangeResourceRecordSetsRequest xmlns="https://route53.amazonaws.com/doc/2013-04-01/">
          <ChangeBatch><Changes>
            <Change><Action>DELETE</Action><ResourceRecordSet><Name>api.g1.example.com.</Name><Type>A</Type><TTL>300</TTL><ResourceRecords><ResourceRecord><Value>10.0.1.1</Value></ResourceRecord></ResourceRecords></ResourceRecordSet></Change>
            <Change><Action>DELETE</Action><ResourceRecordSet><Name>apps.g1.example.com.</Name><Type>A</Type><TTL>300</TTL><ResourceRecords><ResourceRecord><Value>10.0.1.2</Value></ResourceRecord></ResourceRecords></ResourceRecordSet></Change>
          </Changes></ChangeBatch>
        </ChangeResourceRecordSetsRequest>"""
        resp = client.post("/2013-04-01/hostedzone/Z123/rrset/", content=xml, headers={"Content-Type": "application/xml"})
        assert resp.status_code == 200


class TestGetChange:
    def test_synthetic_id_not_flushed_returns_pending(self, client, test_app):
        resp = client.post("/2013-04-01/hostedzone/Z123/rrset/", content=SAMPLE_CHANGE_XML, headers={"Content-Type": "application/xml"})
        match = re.search(r"(BATCH-[a-f0-9]+)", resp.text)
        assert match
        change_id = match.group(1)
        # Mock get_real_change_id to return None (not yet flushed)
        test_app.state.batcher.get_real_change_id = AsyncMock(return_value=None)
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
