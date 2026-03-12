"""Tests for Route53 XML parsing and generation."""

from src.route53_xml import build_change_batch_xml, build_change_response, parse_change_batch


class TestParseChangeBatch:
    def test_parse_single_delete(self):
        xml = """<?xml version="1.0" encoding="UTF-8"?>
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
          <ChangeBatch><Changes>
            <Change><Action>DELETE</Action><ResourceRecordSet>
              <Name>api.g1.example.com.</Name><Type>A</Type><TTL>300</TTL>
              <ResourceRecords><ResourceRecord><Value>10.0.1.1</Value></ResourceRecord></ResourceRecords>
            </ResourceRecordSet></Change>
            <Change><Action>UPSERT</Action><ResourceRecordSet>
              <Name>apps.g2.example.com.</Name><Type>CNAME</Type><TTL>60</TTL>
              <ResourceRecords><ResourceRecord><Value>lb.example.com</Value></ResourceRecord></ResourceRecords>
            </ResourceRecordSet></Change>
          </Changes></ChangeBatch>
        </ChangeResourceRecordSetsRequest>"""
        changes = parse_change_batch(xml.encode())
        assert len(changes) == 2
        assert changes[0]["action"] == "DELETE"
        assert changes[1]["action"] == "UPSERT"
        assert changes[1]["type"] == "CNAME"

    def test_parse_alias_record(self):
        xml = """<?xml version="1.0" encoding="UTF-8"?>
        <ChangeResourceRecordSetsRequest xmlns="https://route53.amazonaws.com/doc/2013-04-01/">
          <ChangeBatch><Changes><Change>
            <Action>CREATE</Action>
            <ResourceRecordSet>
              <Name>apps.g1.example.com.</Name><Type>A</Type>
              <AliasTarget>
                <HostedZoneId>Z1234</HostedZoneId>
                <DNSName>lb.us-east-1.elb.amazonaws.com.</DNSName>
                <EvaluateTargetHealth>false</EvaluateTargetHealth>
              </AliasTarget>
            </ResourceRecordSet>
          </Change></Changes></ChangeBatch>
        </ChangeResourceRecordSetsRequest>"""
        changes = parse_change_batch(xml.encode())
        assert len(changes) == 1
        assert changes[0]["alias_target"]["hosted_zone_id"] == "Z1234"
        assert changes[0].get("ttl") is None
        assert changes[0].get("values") is None

    def test_parse_no_ttl(self):
        xml = """<?xml version="1.0" encoding="UTF-8"?>
        <ChangeResourceRecordSetsRequest xmlns="https://route53.amazonaws.com/doc/2013-04-01/">
          <ChangeBatch><Changes><Change>
            <Action>DELETE</Action>
            <ResourceRecordSet>
              <Name>api.g1.example.com.</Name><Type>A</Type>
              <ResourceRecords><ResourceRecord><Value>10.0.1.1</Value></ResourceRecord></ResourceRecords>
            </ResourceRecordSet>
          </Change></Changes></ChangeBatch>
        </ChangeResourceRecordSetsRequest>"""
        changes = parse_change_batch(xml.encode())
        assert changes[0].get("ttl") is None


class TestBuildChangeResponse:
    def test_build_response(self):
        xml_str = build_change_response("BATCH-abc123")
        assert "BATCH-abc123" in xml_str
        assert "PENDING" in xml_str
        assert "ChangeResourceRecordSetsResponse" in xml_str


class TestBuildChangeBatchXml:
    def test_roundtrip_single_change(self):
        changes = [{"action": "DELETE", "name": "api.g1.example.com.", "type": "A", "ttl": 300, "values": ["10.0.1.5"]}]
        xml_bytes = build_change_batch_xml(changes)
        reparsed = parse_change_batch(xml_bytes)
        assert len(reparsed) == 1
        assert reparsed[0]["action"] == "DELETE"
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
