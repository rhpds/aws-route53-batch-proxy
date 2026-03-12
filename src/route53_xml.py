"""Route53 XML parsing and generation."""

import uuid
from datetime import datetime, timezone
from lxml import etree

NS = "https://route53.amazonaws.com/doc/2013-04-01/"
NSMAP = {None: NS}


def _find(el, tag):
    return el.find(f"{{{NS}}}{tag}")


def _findtext(el, tag):
    child = _find(el, tag)
    return child.text if child is not None else None


def parse_change_batch(xml_bytes: bytes) -> list[dict]:
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
        change = {"action": action, "name": name, "type": rtype}
        if ttl is not None:
            change["ttl"] = ttl
        rr_el = _find(rrset, "ResourceRecords")
        if rr_el is not None:
            values = []
            for rr in rr_el.findall(f"{{{NS}}}ResourceRecord"):
                val = _findtext(rr, "Value")
                if val:
                    values.append(val)
            change["values"] = values
        alias_el = _find(rrset, "AliasTarget")
        if alias_el is not None:
            eval_health_text = _findtext(alias_el, "EvaluateTargetHealth")
            change["alias_target"] = {
                "hosted_zone_id": _findtext(alias_el, "HostedZoneId"),
                "dns_name": _findtext(alias_el, "DNSName"),
                "evaluate_target_health": eval_health_text.lower() == "true" if eval_health_text else False,
            }
        changes.append(change)
    return changes


def build_change_response(change_id: str) -> str:
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
    root = etree.Element("ChangeResourceRecordSetsRequest", nsmap=NSMAP)
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
            etree.SubElement(alias_el, "EvaluateTargetHealth").text = str(alias.get("evaluate_target_health", False)).lower()
    return etree.tostring(root, xml_declaration=True, encoding="UTF-8")


def generate_change_id() -> str:
    return f"BATCH-{uuid.uuid4().hex[:12]}"
