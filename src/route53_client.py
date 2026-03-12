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

    def change_resource_record_sets(self, zone_id: str, change_batch: dict) -> dict:
        response = self.client.change_resource_record_sets(
            HostedZoneId=zone_id, ChangeBatch=change_batch,
        )
        return response.get("ChangeInfo", {})

    def get_change(self, change_id: str) -> dict:
        response = self.client.get_change(Id=change_id)
        return response.get("ChangeInfo", {})

    @staticmethod
    def changes_to_boto3(changes: list[dict]) -> dict:
        boto3_changes = []
        for change in changes:
            rrset = {"Name": change["name"], "Type": change["type"]}
            if "ttl" in change and change["ttl"] is not None:
                rrset["TTL"] = change["ttl"]
            if "values" in change and change["values"] is not None:
                rrset["ResourceRecords"] = [{"Value": v} for v in change["values"]]
            if "alias_target" in change and change["alias_target"] is not None:
                alias = change["alias_target"]
                rrset["AliasTarget"] = {
                    "HostedZoneId": alias["hosted_zone_id"],
                    "DNSName": alias["dns_name"],
                    "EvaluateTargetHealth": alias.get("evaluate_target_health", False),
                }
            boto3_changes.append({"Action": change["action"], "ResourceRecordSet": rrset})
        return {"Changes": boto3_changes}
