# AgnosticD Integration: Route53 Batch Proxy for CNV Provisioning (Optional)

The throttling problem is primarily during mass destroys, but the proxy can also
be used during provisioning for full coverage. This is optional and lower
priority than the destroy-side changes documented in
[agnosticd-integration.md](agnosticd-integration.md).

## Changes Required in agnosticd-v2

Add `endpoint_url: "{{ route53_endpoint_url | default(omit) }}"` to the
`state: present` `amazon.aws.route53` calls in these files:

### `ansible/roles/infra_dns/tasks/nested_loop.yml`

- **Line 19** — present, main record
- **Line 39** — present, alt records

```yaml
  - name: DNS entry ({{ _dns_state | default('present') }})
    amazon.aws.route53:
      state: present
      endpoint_url: "{{ route53_endpoint_url | default(omit) }}"
      aws_access_key_id: "{{ route53_aws_access_key_id }}"
      aws_secret_access_key: "{{ route53_aws_secret_access_key }}"
      hosted_zone_id: "{{ route53_aws_zone_id }}"
      record: "{{ _instance_name }}.{{ guid }}.{{ cluster_dns_zone }}"
      zone: "{{ cluster_dns_zone }}"
      value: "{{ vars[infra_dns_inventory_var] | json_query(find_ip_query) }}"
      type: A
```

### `ansible/roles/host_ocp4_assisted_installer/tasks/configure_sno_compact_cluster.yml`

3 calls at lines 32, 48, 64 (api, api-int, *.apps records).

### `ansible/roles/host_ocp4_assisted_installer/tasks/configure_full_cluster.yml`

2 calls at lines 22, 57 (api, *.apps records).

### `ansible/roles/host-ocp4-hcp-cnv-install/tasks/main.yaml`

1 call at line 295 (*.apps record).

## Changes Required in agnosticd (v1)

### `ansible/roles-infra/infra-dns/tasks/nested_loop.yml`

- **Line 79** — present, main record
- **Line 102** — present, alt records

## Activation

If the destroy-side changes are already in place with `route53_endpoint_url` set
in `openshift_cnv/destroy_env.yml`, provisioning will NOT automatically use the
proxy — the variable is only set in the destroy playbook.

To enable for provisioning, set `route53_endpoint_url` in one of:

- The `openshift_cnv/infrastructure_deployment.yml` playbook (blanket for all CNV provisions)
- Individual catalog item AgnosticV vars
- AAP job template extra vars
