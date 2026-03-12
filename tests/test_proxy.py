"""Integration tests — full HTTP request flow."""

import re
from unittest.mock import AsyncMock, MagicMock, patch

import fakeredis.aioredis
import pytest
from fastapi import FastAPI, Request, Response
from fastapi.testclient import TestClient

from src.batcher import Batcher
from src.config import config
from src.proxy import Proxy
from src.route53_client import Route53Client

AUTH_HEADER = f"AWS4-HMAC-SHA256 Credential={config.AWS_ACCESS_KEY_ID}/20260311/us-east-1/route53/aws4_request"
AUTH_HEADERS = {"Content-Type": "application/xml", "Authorization": AUTH_HEADER}
BAD_AUTH_HEADERS = {"Content-Type": "application/xml", "Authorization": "AWS4-HMAC-SHA256 Credential=BADKEY/x"}


@pytest.fixture
def mock_r53():
    client = MagicMock(spec=Route53Client)
    client.change_resource_record_sets = MagicMock(return_value={"Id": "/change/REAL123", "Status": "PENDING"})
    client.changes_to_boto3 = Route53Client.changes_to_boto3
    client.get_change = MagicMock(
        return_value={"Id": "/change/REAL123", "Status": "INSYNC", "SubmittedAt": "2026-03-11T00:00:00Z"}
    )
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

    @app.api_route("/{path:path}", methods=["GET", "POST", "PUT", "DELETE", "PATCH"])
    async def catch_all(request: Request) -> Response:
        return await request.app.state.proxy.handle_request(request)

    return app


@pytest.fixture
def internal_app():
    from src.app import _shared_state, internal_app

    redis_client = fakeredis.aioredis.FakeRedis(decode_responses=True, connected=True)
    _shared_state["redis"] = redis_client
    _shared_state["batcher"] = Batcher(redis_client)
    _shared_state["flusher"] = MagicMock()
    _shared_state["flusher"].try_acquire_leader = AsyncMock(return_value=False)
    return internal_app


@pytest.fixture
def client(test_app):
    return TestClient(test_app)


@pytest.fixture
def internal_client(internal_app):
    return TestClient(internal_app)


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
        resp = client.post("/2013-04-01/hostedzone/Z123/rrset/", content=SAMPLE_CHANGE_XML, headers=AUTH_HEADERS)
        assert resp.status_code == 200
        assert "BATCH-" in resp.text
        assert "PENDING" in resp.text

    def test_invalid_xml_returns_400(self, client):
        resp = client.post("/2013-04-01/hostedzone/Z123/rrset/", content=b"not xml", headers=AUTH_HEADERS)
        assert resp.status_code == 400

    def test_empty_changes_returns_400(self, client):
        xml = '<?xml version="1.0" encoding="UTF-8"?><ChangeResourceRecordSetsRequest xmlns="https://route53.amazonaws.com/doc/2013-04-01/"><ChangeBatch><Changes></Changes></ChangeBatch></ChangeResourceRecordSetsRequest>'
        resp = client.post("/2013-04-01/hostedzone/Z123/rrset/", content=xml, headers=AUTH_HEADERS)
        assert resp.status_code == 400

    def test_multiple_changes_queued(self, client):
        xml = """<?xml version="1.0" encoding="UTF-8"?>
        <ChangeResourceRecordSetsRequest xmlns="https://route53.amazonaws.com/doc/2013-04-01/">
          <ChangeBatch><Changes>
            <Change><Action>DELETE</Action><ResourceRecordSet><Name>api.g1.example.com.</Name><Type>A</Type><TTL>300</TTL><ResourceRecords><ResourceRecord><Value>10.0.1.1</Value></ResourceRecord></ResourceRecords></ResourceRecordSet></Change>
            <Change><Action>DELETE</Action><ResourceRecordSet><Name>apps.g1.example.com.</Name><Type>A</Type><TTL>300</TTL><ResourceRecords><ResourceRecord><Value>10.0.1.2</Value></ResourceRecord></ResourceRecords></ResourceRecordSet></Change>
          </Changes></ChangeBatch>
        </ChangeResourceRecordSetsRequest>"""
        resp = client.post("/2013-04-01/hostedzone/Z123/rrset/", content=xml, headers=AUTH_HEADERS)
        assert resp.status_code == 200


class TestGetChange:
    def test_synthetic_id_not_flushed_returns_pending(self, client, test_app):
        resp = client.post("/2013-04-01/hostedzone/Z123/rrset/", content=SAMPLE_CHANGE_XML, headers=AUTH_HEADERS)
        match = re.search(r"(BATCH-[a-f0-9]+)", resp.text)
        assert match
        change_id = match.group(1)
        # Mock get_real_change_id to return None (not yet flushed)
        test_app.state.batcher.get_real_change_id = AsyncMock(return_value=None)
        resp = client.get(f"/2013-04-01/change/{change_id}", headers={"Authorization": AUTH_HEADER})
        assert resp.status_code == 200
        assert "PENDING" in resp.text


class TestAuth:
    def test_missing_auth_returns_403(self, client):
        resp = client.post("/2013-04-01/hostedzone/Z123/rrset/", content=SAMPLE_CHANGE_XML)
        assert resp.status_code == 403
        assert "MissingAuthenticationToken" in resp.text

    def test_bad_credentials_returns_403(self, client):
        resp = client.post("/2013-04-01/hostedzone/Z123/rrset/", content=SAMPLE_CHANGE_XML, headers=BAD_AUTH_HEADERS)
        assert resp.status_code == 403
        assert "InvalidClientTokenId" in resp.text


class TestPassthrough:
    @patch("src.proxy.httpx.AsyncClient.request")
    def test_passthrough_forwards_to_route53(self, mock_request, client):
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.content = b"<ListHostedZonesResponse/>"
        mock_response.headers = {"content-type": "application/xml"}
        mock_request.return_value = mock_response
        resp = client.get("/2013-04-01/hostedzone", headers={"Authorization": AUTH_HEADER})
        assert resp.status_code == 200

    @patch("src.proxy.httpx.AsyncClient.request")
    def test_passthrough_forwards_list_rrsets(self, mock_request, client):
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.content = b"<ListResourceRecordSetsResponse/>"
        mock_response.headers = {"content-type": "application/xml"}
        mock_request.return_value = mock_response
        resp = client.get("/2013-04-01/hostedzone/Z123/rrset", headers={"Authorization": AUTH_HEADER})
        assert resp.status_code == 200


class TestInternal:
    def test_health(self, internal_client):
        resp = internal_client.get("/health")
        assert resp.status_code == 200

    def test_ready(self, internal_client):
        resp = internal_client.get("/ready")
        assert resp.status_code == 200

    def test_metrics(self, internal_client):
        resp = internal_client.get("/metrics")
        assert resp.status_code == 200

    def test_status(self, internal_client):
        resp = internal_client.get("/status")
        assert resp.status_code == 200
        assert "active_zones" in resp.json()
