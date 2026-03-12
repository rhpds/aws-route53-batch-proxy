# AgnosticD Integration: Route53 Batch Proxy for CNV Destroys

## Background

When CNV-based catalog items are destroyed in bulk (e.g. post-event cleanup), all
DNS record deletions hit the same Route53 hosted zone. Route53 enforces a hard
5 requests/second per-zone limit, which causes cascading throttling failures
during mass destroys.

The Route53 Batch Proxy solves this by intercepting `ChangeResourceRecordSets`
calls, queuing them in Redis, and flushing consolidated batches every 2 seconds.

## Proxy URL

| Environment | URL |
|-------------|-----|
| Dev | `https://route53-proxy-dev.apps.ocpv-infra01.dal12.infra.demo.redhat.com` |
| Prod | `https://route53-proxy.apps.ocpv-infra01.dal12.infra.demo.redhat.com` |

## Strategy

- Add `endpoint_url: "{{ route53_endpoint_url | default(omit) }}"` to the
  `amazon.aws.route53` module calls in destroy-related roles.
- Set a default value for `route53_endpoint_url` in the `openshift_cnv` cloud
  provider destroy playbook so all CNV destroys use the proxy automatically.
- Individual catalog items can override (or disable) via AgnosticV vars.

Using `default(omit)` means any playbook that doesn't define the variable
is completely unaffected — the parameter is simply absent from the module call.

For provision-side (create) changes, see [agnosticd-integration-provision.md](agnosticd-integration-provision.md).

---

## Changes Required in agnosticd-v2

### 1. `ansible/cloud_providers/openshift_cnv/destroy_env.yml`

Set the default proxy URL for all CNV destroys. Add `route53_endpoint_url` as a
play-level var:

```yaml
---
- name: Include Variables
  ansible.builtin.import_playbook: ../../include_vars.yml

- name: Delete Infrastructure
  hosts: localhost
  connection: local
  gather_facts: false
  become: false
  vars:
    route53_endpoint_url: "https://route53-proxy.apps.ocpv-infra01.dal12.infra.demo.redhat.com"
  tasks:
  - name: Run resources role
    vars:
      ACTION: destroy
    ansible.builtin.include_role:
      name: agnosticd.cloud_provider_openshift_cnv.resources

  - name: Run infra_dns role
    when: cluster_dns_server is defined or route53_aws_zone_id is defined
    vars:
      _dns_state: absent
    ansible.builtin.include_role:
      name: infra_dns
```

### 2. `ansible/roles/infra_dns/tasks/nested_loop.yml`

Add `endpoint_url` to the two `state: absent` `amazon.aws.route53` calls.

**Lines 61-68** (delete — main record):
```yaml
  - name: DNS entry ({{ _dns_state | default('present') }})
    amazon.aws.route53:
      state: absent
      endpoint_url: "{{ route53_endpoint_url | default(omit) }}"
      aws_access_key_id: "{{ route53_aws_access_key_id }}"
      aws_secret_access_key: "{{ route53_aws_secret_access_key }}"
      hosted_zone_id: "{{ route53_aws_zone_id }}"
      record: "{{ _instance_name }}.{{ guid }}.{{ cluster_dns_zone }}"
      zone: "{{ cluster_dns_zone }}"
      type: A
```

**Lines 75-82** (delete — alt records):
```yaml
  - name: DNS alternative entry ({{ _dns_state | default('present') }})
    amazon.aws.route53:
      state: absent
      endpoint_url: "{{ route53_endpoint_url | default(omit) }}"
      aws_access_key_id: "{{ route53_aws_access_key_id }}"
      aws_secret_access_key: "{{ route53_aws_secret_access_key }}"
      hosted_zone_id: "{{ route53_aws_zone_id }}"
      record: "{{ _alt_name }}{{ _index }}.{{ guid }}.{{ cluster_dns_zone }}"
      zone: "{{ cluster_dns_zone }}"
      type: A
```

### 3. `ansible/roles/host_ocp4_assisted_destroy/tasks/main.yaml`

Add `endpoint_url` to the route53 call at **line 52**:

```yaml
  - name: Delete DNS records (Route53)
    when: route53_aws_access_key_id is defined
    amazon.aws.route53:
      state: absent
      endpoint_url: "{{ route53_endpoint_url | default(omit) }}"
      aws_access_key_id: "{{ route53_aws_access_key_id }}"
      aws_secret_access_key: "{{ route53_aws_secret_access_key }}"
      hosted_zone_id: "{{ route53_aws_zone_id }}"
      record: "{{ item }}.{{ cluster_name }}.{{ cluster_dns_zone }}"
      zone: "{{ cluster_dns_zone  }}"
      type: A
    loop:
    - "api-int"
    - "api"
    - "*.apps"
```

---

## Changes Required in agnosticd (v1)

The v1 repo has the same pattern with slightly different paths and role naming.

### 1. `ansible/cloud_providers/openshift_cnv_destroy_env.yml`

Add `route53_endpoint_url` as a play-level var (same as v2 above):

```yaml
- name: Delete Infrastructure
  hosts: localhost
  connection: local
  gather_facts: false
  become: false
  vars:
    route53_endpoint_url: "https://route53-proxy.apps.ocpv-infra01.dal12.infra.demo.redhat.com"
  tasks:
    - name: Run infra-openshift-cnv-resources
      ansible.builtin.include_role:
        name: infra-openshift-cnv-resources
      vars:
        ACTION: destroy

    - name: Run infra-dns Role
      when: cluster_dns_server is defined or route53_aws_zone_id is defined
      ansible.builtin.include_role:
        name: infra-dns
      vars:
        _dns_state: absent
```

### 2. `ansible/roles-infra/infra-dns/tasks/nested_loop.yml`

Add `endpoint_url` to the two `state: absent` `amazon.aws.route53` calls.

**Line 164** (delete — main record):
```yaml
        - name: DNS entry ({{ _dns_state | default('present') }})
          when: route53_aws_zone_id is defined
          amazon.aws.route53:
            state: absent
            endpoint_url: "{{ route53_endpoint_url | default(omit) }}"
            aws_access_key_id: "{{ route53_aws_access_key_id }}"
            aws_secret_access_key: "{{ route53_aws_secret_access_key }}"
            hosted_zone_id: "{{ route53_aws_zone_id }}"
            record: "{{ _instance_name }}.{{ base_domain }}"
            type: A
```

**Line 182** (delete — alt records):
```yaml
        - name: DNS alternative entry ({{ _dns_state | default('present') }})
          when: route53_aws_zone_id is defined and _alt_names | length > 0
          loop: "{{ _alt_names }}"
          loop_control:
            loop_var: _alt_name
          amazon.aws.route53:
            state: absent
            endpoint_url: "{{ route53_endpoint_url | default(omit) }}"
            aws_access_key_id: "{{ route53_aws_access_key_id }}"
            aws_secret_access_key: "{{ route53_aws_secret_access_key }}"
            hosted_zone_id: "{{ route53_aws_zone_id }}"
            record: "{{ _alt_name }}{{_index}}.{{ base_domain }}"
            type: A
```

### 3. `ansible/roles/host-ocp4-assisted-destroy/tasks/main.yaml`

Add `endpoint_url` to the route53 call at **line 52**:

```yaml
    - name: Delete dns records
      when: route53_aws_zone_id is defined
      amazon.aws.route53:
        state: absent
        endpoint_url: "{{ route53_endpoint_url | default(omit) }}"
        aws_access_key_id: "{{ route53_aws_access_key_id }}"
        aws_secret_access_key: "{{ route53_aws_secret_access_key }}"
        hosted_zone_id: "{{ route53_aws_zone_id }}"
        record: "{{ item }}.{{ cluster_name }}.{{ cluster_dns_zone }}"
        zone: "{{ cluster_dns_zone  }}"
        type: A
      loop:
        - "api"
        - "*.apps"
```

---

## Catalog Items Requiring SCM Ref Bump

After the agnosticd changes are merged, the following CNV-based catalog items
need their AgnosticV `scm_ref` bumped to pick up the new code. These are the
items that had pending destroy jobs on `aap2-prod-us-east-2`:

### CNV Destroy Catalog Items (must bump)

| AgnosticV Account | Catalog Item | env_type | cloud_provider |
|-------------------|-------------|----------|----------------|
| `agd-v2` | `ocp-cluster-cnv-pools` | ocp4-cluster | openshift_cnv |
| `openshift-cnv` | `ocp-cnv-pools` | ocp4-cluster | openshift_cnv |
| `openshift-cnv` | `ocp4-kasten-cnv` | ocp4-cluster | none |
| `openshift-cnv` | `ocpmulti-single-node-cnv` | ocp4-cluster | openshift_cnv |
| `openshift-cnv` | `osp-on-ocp-cnv` | ocp4-cluster | openshift_cnv |
| `openshift-cnv` | `mig-factory-demo` | ocp4-cluster | openshift_cnv |
| `openshift-cnv` | `virt-aap-day-2` | ocp4-cluster | openshift_cnv |
| `zt-ansiblebu` | `zt-ans-bu-lab-developer-cnv` | zero-touch-base-rhel | openshift_cnv |
| `zt-rhelbu` | `zt-rhel-bu-lab-developer-cnv` | zero-touch-base-rhel | openshift_cnv |

### Other CNV Catalog Items (bump for completeness)

These had pending provision/stop jobs but no pending destroys. They will still
benefit from the proxy when they eventually destroy:

| AgnosticV Account | Catalog Item |
|-------------------|-------------|
| `agd-v2` | `ocp-cluster-cnv` |
| `agd-v2` | `ocp-virt-labs-pool` |
| `enterprise` | `aap-product-demos-cnv-aap25` |
| `openshift-cnv` | `ocp-virt-roadshow-multi-user` |

### AgnosticV Override

Any catalog item can disable the proxy by setting in its AgnosticV vars:

```yaml
route53_endpoint_url: ~
```

YAML null (`~`) will cause `default(omit)` to omit the parameter entirely,
falling back to direct AWS Route53 calls.

---

## Verification

1. Merge the agnosticd PR.
2. Trigger a destroy on a dev-stage CNV catalog item (e.g. `agd-v2.ocp-cluster-cnv-pools`).
3. Check the proxy logs and metrics:
   ```
   KUBECONFIG=~/secrets/ocpv-infra01.dal12.infra.demo.redhat.com.kubeconfig \
     oc logs -n route53-batch-proxy-dev -l app=route53-batch-proxy -f
   ```
4. Confirm DNS records are deleted and the proxy shows batched flushes.
5. After validation, bump `scm_ref` on the prod catalog items listed above.
