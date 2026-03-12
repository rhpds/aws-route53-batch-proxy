"""Request routing — intercept ChangeResourceRecordSets, pass through everything else."""

import logging
import re

from fastapi import Request, Response

from src.batcher import Batcher
from src.config import config
from src.metrics import CHANGES_QUEUED, PASSTHROUGH_TOTAL, REQUESTS_TOTAL
from src.route53_client import Route53Client
from src.route53_xml import build_change_response, generate_change_id, parse_change_batch

logger = logging.getLogger(__name__)

CHANGE_RRSET_PATTERN = re.compile(r"^/2013-04-01/hostedzone/([^/]+)/rrset/?$")
GET_CHANGE_PATTERN = re.compile(r"^/2013-04-01/change/([^/]+)$")
SIGV4_CREDENTIAL_PATTERN = re.compile(r"Credential=([^/]+)/")

MISSING_AUTH_RESPONSE = Response(
    content=(
        '<?xml version="1.0"?>\n'
        '<ErrorResponse xmlns="https://route53.amazonaws.com/doc/2013-04-01/">'
        "<Error><Type>Sender</Type><Code>MissingAuthenticationToken</Code>"
        "<Message>Missing Authentication Token</Message></Error>"
        "<RequestId>proxy</RequestId></ErrorResponse>"
    ),
    status_code=403,
    media_type="application/xml",
)

INVALID_TOKEN_RESPONSE = Response(
    content=(
        '<?xml version="1.0"?>\n'
        '<ErrorResponse xmlns="https://route53.amazonaws.com/doc/2013-04-01/">'
        "<Error><Type>Sender</Type><Code>InvalidClientTokenId</Code>"
        "<Message>The security token included in the request is invalid.</Message></Error>"
        "<RequestId>proxy</RequestId></ErrorResponse>"
    ),
    status_code=403,
    media_type="application/xml",
)


class Proxy:
    def __init__(self, batcher: Batcher, r53_client: Route53Client):
        self.batcher = batcher
        self.r53_client = r53_client

    def _check_auth(self, request: Request) -> Response | None:
        auth = request.headers.get("authorization", "")
        if not auth:
            return MISSING_AUTH_RESPONSE
        match = SIGV4_CREDENTIAL_PATTERN.search(auth)
        if not match or match.group(1) != config.AWS_ACCESS_KEY_ID:
            return INVALID_TOKEN_RESPONSE
        return None

    async def handle_request(self, request: Request) -> Response:
        auth_error = self._check_auth(request)
        if auth_error:
            return auth_error
        path = request.url.path
        method = request.method
        match = CHANGE_RRSET_PATTERN.match(path)
        if match and method == "POST":
            zone_id = match.group(1)
            REQUESTS_TOTAL.labels(operation="ChangeResourceRecordSets").inc()
            return await self._handle_change(request, zone_id)
        match = GET_CHANGE_PATTERN.match(path)
        if match and method == "GET":
            change_id = match.group(1)
            REQUESTS_TOTAL.labels(operation="GetChange").inc()
            return await self._handle_get_change(change_id)
        REQUESTS_TOTAL.labels(operation="passthrough").inc()
        PASSTHROUGH_TOTAL.labels(operation=self._guess_operation(path, method)).inc()
        return await self._handle_passthrough(request)

    async def _handle_change(self, request: Request, zone_id: str) -> Response:
        body = await request.body()
        try:
            changes = parse_change_batch(body)
        except Exception:
            logger.exception("Failed to parse ChangeResourceRecordSets XML")
            return Response(
                content=(
                    "<ErrorResponse><Error><Code>InvalidInput</Code>"
                    "<Message>Failed to parse request XML</Message></Error></ErrorResponse>"
                ),
                status_code=400,
                media_type="application/xml",
            )
        if not changes:
            return Response(
                content=(
                    "<ErrorResponse><Error><Code>InvalidInput</Code>"
                    "<Message>No changes in request</Message></Error></ErrorResponse>"
                ),
                status_code=400,
                media_type="application/xml",
            )
        change_id = generate_change_id()
        try:
            await self.batcher.enqueue(zone_id, changes, change_id)
            for change in changes:
                CHANGES_QUEUED.labels(zone_id=zone_id, action=change["action"]).inc()
            logger.info("Queued %d changes for zone %s (change_id=%s)", len(changes), zone_id, change_id)
        except Exception:
            logger.warning("Redis unavailable, falling back to direct Route53 call for zone %s", zone_id, exc_info=True)
            return await self._direct_change(zone_id, changes)
        return Response(content=build_change_response(change_id), status_code=200, media_type="application/xml")

    async def _direct_change(self, zone_id: str, changes: list[dict]) -> Response:
        try:
            clean = [{k: v for k, v in c.items() if not k.startswith("_")} for c in changes]
            boto3_batch = self.r53_client.changes_to_boto3(clean)
            result = self.r53_client.change_resource_record_sets(zone_id, boto3_batch)
            real_id = result.get("Id", "unknown")
            return Response(
                content=build_change_response(real_id.split("/")[-1]), status_code=200, media_type="application/xml"
            )
        except Exception:
            logger.exception("Direct Route53 call failed for zone %s", zone_id)
            return Response(
                content=(
                    "<ErrorResponse><Error><Code>ServiceUnavailable</Code>"
                    "<Message>Route53 call failed</Message></Error></ErrorResponse>"
                ),
                status_code=503,
                media_type="application/xml",
            )

    async def _handle_get_change(self, change_id: str) -> Response:
        if change_id.startswith("BATCH-"):
            real_id = await self.batcher.get_real_change_id(change_id)
            if real_id is None:
                return Response(content=build_change_response(change_id), status_code=200, media_type="application/xml")
            change_id = real_id.split("/")[-1]
        try:
            result = self.r53_client.get_change(change_id)
            status = result.get("Status", "PENDING")
            xml = (
                f'<?xml version="1.0" encoding="UTF-8"?>\n'
                f'<GetChangeResponse xmlns="https://route53.amazonaws.com/doc/2013-04-01/">\n'
                f"  <ChangeInfo>\n"
                f"    <Id>/change/{change_id}</Id>\n"
                f"    <Status>{status}</Status>\n"
                f"    <SubmittedAt>{result.get('SubmittedAt', '')}</SubmittedAt>\n"
                f"  </ChangeInfo>\n"
                f"</GetChangeResponse>"
            )
            return Response(content=xml, status_code=200, media_type="application/xml")
        except Exception:
            logger.exception("GetChange failed for %s", change_id)
            return Response(
                content=(
                    "<ErrorResponse><Error><Code>ServiceUnavailable</Code>"
                    "<Message>GetChange failed</Message></Error></ErrorResponse>"
                ),
                status_code=503,
                media_type="application/xml",
            )

    async def _handle_passthrough(self, request: Request) -> Response:
        logger.warning("Unhandled Route53 operation: %s %s", request.method, request.url.path)
        return Response(
            content=(
                "<ErrorResponse><Error><Code>NotImplemented</Code>"
                "<Message>This Route53 operation is not proxied</Message></Error></ErrorResponse>"
            ),
            status_code=501,
            media_type="application/xml",
        )

    @staticmethod
    def _guess_operation(path: str, method: str) -> str:
        if "/hostedzone" in path and method == "GET":
            return "ListResourceRecordSets" if "/rrset" in path else "GetHostedZone"
        if "/hostedzone" in path and method == "POST":
            return "CreateHostedZone"
        return f"{method}:{path[:50]}"
