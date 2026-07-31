# Cluster Builder — Implementation Plan

Build a Kubernetes cluster from VMs selected out of vCenter: connect KubeSight to vSphere,
pick VMs from a dropdown, assign roles (control plane / worker / load balancer), set a few
options, and KubeSight prepares the nodes, runs `kubeadm`, installs a CNI, verifies health,
and auto-registers the result as a managed cluster.

Status: **IMPLEMENTED 2026-07-22** — backend + frontend + tests (30 feature tests green;
full suite 662 passed with only the 9 known pre-existing failures). Remaining before
production use: real-VM validation ladder (§12) and `pip install paramiko` in the
backend environment. Cilium stays behind KUBESIGHT_ENABLE_CILIUM. This document now
serves as the architecture reference.

---

## 1. Target flow

```
Settings → vSphere connection (vCenter URL, credentials, TLS)
     ↓
New Cluster Build
  1. Basics      name, k8s version, CNI, CIDRs, build profile, topology (single | HA)
  2. Nodes       pick VMs from the vCenter dropdown → assign roles
                   2 × load balancer   (KubeSight installs haproxy + keepalived)
                   3 × control plane   (HA, stacked etcd)
                   N × worker
  3. VIP         an unused address on the control-plane L2 segment
  4. Preflight   vCenter-side checks + per-node SSH checks, traffic-light table
  5. Build       LBs → VIP live → kubeadm init through the VIP → join CPs → CNI
                 → join workers → verify → cluster appears in KubeSight
```

### In scope (v1)

- VM selection from vCenter inventory. **KubeSight does not create VMs** — they must already exist.
- Single control plane **and** stacked-HA (3 control planes, stacked etcd).
- **KubeSight-managed haproxy + keepalived on 2 dedicated LB VMs — the primary,
  production-tested path.** No dependency on an existing F5 or external LB.
- Secondary endpoint modes: existing external LB, or a manually configured API endpoint.
- containerd as the CRI.
- Ubuntu 22.04/24.04 (primary) and Rocky/RHEL 9 (validated secondary), via OS adapters.
- Repository modes: internet, internal mirror, fully offline bundle.
- Calico as the production CNI; Flannel as a lab option; Cilium behind a flag.
- Day-2: add workers; queued workers can be removed before they join. Removing
  a live worker (drain, delete, and infrastructure cleanup) is deferred.
- Auto-onboarding: the finished cluster appears in KubeSight with no manual kubeconfig step.

### Explicit non-goals (v1)

- VM **creation** or cloning from template (vCenter is read-only to us — see §3.3).
- kube-vip (deferred — see §5.2), external-etcd topology, Windows nodes.
- Non-vSphere inventory sources (bare metal / cloud) — manual host entry stays available
  as the fallback path, so nothing is blocked.
- Whole-cluster teardown (per-node `kubeadm reset` only).

---

## 2. What we reuse

| Existing | Reuse |
|---|---|
| `services/registry_client.py`, `jenkins_client.py` | **urllib-only HTTP client style — no `requests` anywhere in this codebase.** The vSphere client follows it exactly, adding zero dependencies. |
| `services/registry_service.py` + `RegistryConnection` | Exact template for `VSphereConnection`: encrypted secret, `test_connection`, recorded status/error/last-tested. |
| `upgrade_executor.py` | apt/dnf/yum install scripts, `_APT_SHELL_PREAMBLE`, lock waiting, retry and transient-API-error handling. **Extracted into OS adapters and shared with upgrades** (task §4.1). |
| `services/mobile_app_service.py` | The durable-job pattern: DB-backed state, scheduler tick, orphan recovery after restart, synchronous execution under `TESTING`. This is the model for the build executor — *not* `upgrade_jobs.py`, which is a process-local dict. |
| `cluster_store.py` | Final phase writes `admin.conf` and registers the `Cluster` row. |
| `secret_encryption.py` | Fernet for SSH secrets, sudo passwords, vCenter password, registry credentials, cert key. |
| `migrate_rbac.py` | `run_migrations()` + `_add_column_if_missing` idempotent migration convention. |
| `services/alert_policy_scheduler.py` | Where `advance_cluster_builds()` is registered as a tick. |
| `rbac_data.py`, `services/audit_service.py` | New permissions and audit entries. |
| `pages/UpgradeSafeModePage.jsx` | Live step/progress UI shape. |

The one true gap: **no SSH transport anywhere in the backend.**

---

## 3. vSphere integration

### 3.1 Client

`services/vsphere_client.py`, stdlib `urllib`, vSphere Automation REST API (vCenter 7/8):

```
POST /api/session                              → session token (header vmware-api-session-id)
GET  /api/vcenter/vm                           → list: vm moid, name, power_state, cpu, memory
GET  /api/vcenter/vm/{vm}                      → hardware detail, disks, nics
GET  /api/vcenter/vm/{vm}/guest/identity       → hostname, guest OS, IP address
GET  /api/vcenter/vm/{vm}/guest/local-filesystem → disk free space
GET  /api/vcenter/host, /api/vcenter/datastore  → placement, for anti-affinity checks
```

Session token cached with TTL; TLS verification on by default with an explicit
`skip_tls_verify` escape hatch and optional CA PEM, mirroring `Cluster.skip_tls_verify`.

### 3.2 What vCenter gives us for free

The dropdown is the visible benefit, but the real win is **preflight checks we could not
otherwise do**, before a single SSH connection:

| Check | Severity |
|---|---|
| VM powered on | fail |
| VMware Tools running | **warn** — Tools is recommended, not required. When present the management IP is filled automatically; when absent the wizard requires a manual IP override before the node can be used. |
| vCPU / RAM below role minimum (CP: 2 vCPU / 2 GB, worker: 2/2) | fail |
| Disk free below threshold | fail |
| Guest OS outside the supported matrix | fail |
| **The 3 control-plane VMs sit on the same ESXi host** | **fail** |
| **The 2 LB VMs sit on the same ESXi host** | **fail** |
| Control-plane VMs share a datastore | warn |
| CPU/memory reservation absent on control planes | warn |
| VM snapshot present (etcd + snapshots = corruption risk) | warn |

The anti-affinity checks are the ones that matter. A "highly available" control plane whose
three VMs live on one ESXi host is the single most common way an HA cluster turns out not to
be — and it is invisible from inside the guest. This check alone justifies the integration.

### 3.3 Read-only by design

v1 only reads vCenter inventory. No power operations, no cloning, no reconfiguration. It keeps
the required vCenter role to **Read-Only**, which is a far easier approval to get from an
infrastructure team than anything that can mutate VMs.

> v2 candidates: create a DRS anti-affinity rule for the control-plane group; clone VMs from a
> template so KubeSight provisions the VMs too.

---

## 4. Node preparation architecture

### 4.1 OS adapters

```python
class OsAdapter(Protocol):
    id: str                        # "debian" | "rhel"
    validated_versions: tuple      # ("22.04", "24.04")

    def matches(facts: OsFacts) -> bool
    def preflight_checks(ctx) -> list[Check]
    def script_configure_proxy_and_ca(ctx) -> str
    def script_disable_swap(ctx) -> str
    def script_kernel_modules_and_sysctl(ctx) -> str
    def script_install_containerd(ctx) -> str        # SystemdCgroup=true
    def script_install_kube_packages(ctx, version) -> str
    def script_install_haproxy_keepalived(ctx) -> str
    def script_hold_packages(ctx) -> str
    def script_reset_node(ctx) -> str
```

Detection runs in preflight (`/etc/os-release` + `uname -m`), cross-checked against the guest
OS vCenter reports. No adapter match ⇒ **hard preflight failure** naming the supported matrix.
No generic fallback commands.

> **Refactor note:** the apt/dnf/yum logic currently inline in `upgrade_executor.py` moves into
> `debian.py` / `rhel.py`, and the upgrade path calls the adapters. One implementation serving
> both features, but it touches working code — `backend/tests/test_upgrades.py` must stay green,
> and it lands as its own commit before the build engine depends on it.

### 4.2 CNI plugin descriptor

```python
@dataclass(frozen=True)
class CniDescriptor:
    id, display_name, support_tier            # production | lab | experimental
    versions, k8s_version_range
    default_pod_cidr, allowed_pod_cidr_prefixes
    required_images
    def preflight_checks(ctx) -> list[Check]
    def render_manifests(ctx) -> list[str]
    def readiness_checks(ctx) -> list[Check]
    def uninstall(ctx) -> None
    advanced_params_schema: dict
```

Calico = production default, Flannel = lab, Cilium = experimental behind an env flag until its
kernel and kube-proxy prerequisites are validated. Manifests are **bundled versioned files**
under `backend/api/data/cni/<plugin>/<version>/` — not URLs. That makes offline mode work with
nothing to fetch and keeps the UI version list a closed set.

### 4.3 Repository modes

`BuildProfile` is a reusable named record — configure the internal mirror once, reference it
from every build.

| Mode | Behaviour | Preflight |
|---|---|---|
| `internet` | `pkgs.k8s.io` / `registry.k8s.io` defaults. Dev/test only. | reachability probe **from each node** |
| `mirror` | All repo/registry URLs from the profile. Production default. | probe every configured URL from each node; registry auth check |
| `offline` | Bundle pre-staged on nodes, or pushed by KubeSight over SFTP. | verify presence **and sha256** of every package, image and manifest before phase 2 |

Required images derive from `kubeadm config images list --kubernetes-version=X` plus the CNI
descriptor's `required_images` — never a hand-maintained list.

### 4.4 Add-ons (plugins)

Add-ons follow the CNI descriptor model exactly: a closed catalog, version-pinned manifests,
SHA-256 integrity, image rewriting to `addon_image_registry`, and a readiness gate. They install
in phase 12, after the cluster is registered, so an add-on failure never costs you the cluster.

| Add-on | Tier | Configuration | Proven by |
|---|---|---|---|
| Metrics Server | supported | none | a scoped kubelet-serving CSR approver, then `kubectl top nodes` must report **every** node |
| NGINX Ingress Controller | supported | none | `ingressclass/nginx` exists, the Service is NodePort, and that NodePort answers HTTP from a node |
| MetalLB (native L2) | best-effort | **address pool, required** | the pool is applied as `IPAddressPool` + `L2Advertisement`, then a throwaway `type: LoadBalancer` Service must receive an address **inside the pool** |

MetalLB's pool is part of selecting it, not a post-build chore — without one, every LoadBalancer
Service stays `<pending>`. The pool is validated for overlap with itself, with the API VIP, and
with every node address, and TCP/UDP 7946 is added to the port preflight on cluster nodes.

### 4.5 The bundle directory

Manifests are read from `backend/api/data/{cni,addons}/<id>/<version>/` first and only fetched
from the pinned URL when the repo mode allows it, so a populated bundle directory is what makes
`offline` mode — and any network that cannot reach GitHub — work. It is populated by:

```
python tools/fetch_cluster_build_bundles.py            # download + verify all
python tools/fetch_cluster_build_bundles.py --verify   # check what is bundled
python tools/fetch_cluster_build_bundles.py --from-dir DIR   # air-gapped import
python tools/fetch_cluster_build_bundles.py --cilium   # helm-render Cilium
```

Digests are enforced on every path, including `--from-dir`, and again on every read at build
time. The catalog's manifests are committed, so a fresh checkout can build offline; the script is
what adds a new version or repairs the directory. Cilium is the one entry with no upstream
single-file manifest — `--cilium` renders it with `helm template` (helm is already in the backend
image) and `apply_pod_cidr` rewrites `cluster-pool-ipv4-cidr` per build, refusing a manifest it
cannot find that key in.

---

## 5. High availability

### 5.1 Topology

`topology_type` ∈ `single_cp | stacked_ha`. Stacked HA = 3 control planes each running etcd.
Enforced: control-plane count must be **1 or 3** (5 allowed but flagged); 2 is rejected with an
explanation of quorum.

`--control-plane-endpoint` is set on **every** build including single-CP, so the single→HA
migration path stays open. Choosing an endpoint that resolves to one node's IP shows an explicit
warning: migrating that cluster to HA later requires certificate regeneration and a kubeconfig
rewrite on every node.

### 5.2 Endpoint modes — recommendation

| Mode | v1 | Notes |
|---|---|---|
| `managed_haproxy` | ✅ **default** | 2 VMs given the `loadbalancer` role; KubeSight installs and configures haproxy + keepalived. **The production-tested path — no external LB dependency.** |
| `external_lb` | ✅ | User supplies an existing VIP / DNS / F5 / NSX VIP. Supported, not required. |
| `manual_endpoint` | ✅ | User types a `host:port` KubeSight does not manage at all. Escape hatch; validated for reachability only. |
| `kube_vip` | ❌ v2 | No extra VMs, but see below. |
| `dns_rr` | ❌ | Round-robin DNS to the CPs — no health checking. Rejected. |

**Why kube-vip is deferred despite being the tidier answer:** it runs as a static pod on the
control planes, so the VIP that `--control-plane-endpoint` points at does not exist until after
`kubeadm init` has already used it — a genuine bootstrap ordering problem, and its workaround
shifted with the `super-admin.conf` change in kubeadm 1.29. haproxy + keepalived is plain
systemd, comes up **before** kubeadm runs, and behaves identically across Kubernetes versions.
For a first HA release that is the right trade. kube-vip returns in v2 once the base path is
proven.

### 5.3 Managed haproxy + keepalived

Two LB VMs, both running:

- **haproxy** — TCP mode, frontend `VIP:6443`, backend = all 3 control planes on 6443.
- **keepalived** — VRRP, one MASTER (higher priority) and one BACKUP, sharing the VIP.

Config is templated from the build record: VIP, interface, VRRP router ID, auth pass, and the
CP backend list. The VIP must be a **free address on the same L2 segment** as the CPs.

Bootstrap order matters and is baked into the phase machine: LBs are configured and the VIP is
verified live **before** `kubeadm init` runs, because `init` writes that endpoint into the
certificates.

**Two details that decide whether this works or is merely flaky:**

1. **haproxy backend health checks are mandatory, not optional.** At `kubeadm init` time only
   CP1 exists — CP2 and CP3 are dead backends. A naive round-robin config would send init's own
   API calls to hosts that aren't listening and fail intermittently. The backend uses an explicit
   health check (`option httpchk GET /healthz` over SSL against 6443, or at minimum a TCP `check`
   with a short `fall`), so dead control planes are removed from rotation until they join. This is
   the single most likely cause of a "sometimes init hangs" bug in this design.

2. **keepalived must track haproxy, not just the node.** Without a `vrrp_script chk_haproxy`
   lowering priority when haproxy stops, the VIP stays on a node whose haproxy has died and the
   API becomes unreachable while both LB VMs are still up. The tracking script is part of the
   template, and the failover test in §12 kills haproxy specifically — not the VM — to prove it.

Dedicated LB VMs also sidestep a port conflict that colocating haproxy on the control planes
would create, where haproxy and the API server both want 6443 and one has to move to a
non-standard port.

> **The vSphere-specific risk is the portgroup, not the config** — see §11.3.

---

## 6. Data model

New tables via `db.create_all()`, with forward-compatible column adds in `migrate_rbac.py`.

```
VSphereConnection
  id, name, base_url, username, password_cipher, skip_tls_verify, ca_pem,
  datacenter_filter, folder_filter, is_active,
  last_connection_status, last_connection_error, last_tested_at

SshCredential
  id, name, username, auth_method(key|password),
  secret_cipher, key_passphrase_cipher, port,
  sudo_mode(root|nopasswd|password), sudo_password_cipher

SshConnectionProfile
  id, name, credential_id FK,
  route_mode(direct|bastion), bastion_host, bastion_port, bastion_credential_id FK,
  host_key_policy(strict|tofu|pinned),
  connect_timeout_s, command_timeout_s, retry_count, retry_backoff_s

SshHostKey
  id, host, port, key_type, fingerprint_sha256,
  source(preapproved|tofu), approved_by_user_id, approved_at

BuildProfile
  id, name, repo_mode(internet|mirror|offline),
  k8s_pkg_repo_url, k8s_pkg_gpg_key_url, cri_pkg_repo_url,
  k8s_image_registry, cni_image_registry, addon_image_registry,
  registry_username, registry_password_cipher,
  http_proxy, https_proxy, no_proxy, extra_ca_certs_pem,
  offline_bundle_path, offline_bundle_checksum

ClusterBuild
  id, name, status, k8s_version, cri,
  topology_type(single_cp|stacked_ha),
  control_plane_endpoint,
  endpoint_mode(managed_haproxy|external_lb|manual_endpoint),
  vip_address, vip_interface, vrrp_router_id, vrrp_auth_pass_cipher, lb_config_json,
  vip_source(manual|ipam), ipam_reservation_id,          ← ipam fields reserved for v2
  cert_key_cipher, cert_key_expires_at,
  cni_plugin, cni_version, pod_cidr, service_cidr, cni_params_json,
  vsphere_connection_id FK, build_profile_id FK, connection_profile_id FK,
  result_cluster_id, error, created_by, started_at, finished_at

ClusterBuildNode
  id, build_id FK, hostname, address,
  address_source(vmware_tools|manual),                                 ← Tools optional
  role(control_plane|worker|loadbalancer), is_primary_cp, is_lb_master,
  vsphere_vm_moid, vsphere_vm_name, vsphere_host, vsphere_datastore,   ← anti-affinity source
  vsphere_tools_status, vsphere_power_state, vsphere_cpu, vsphere_memory_mb,
  connection_profile_id FK (per-node override, for mixed direct/bastion routes),
  os_family, os_version, arch, status, preflight_json, error, position

ClusterBuildStep
  id, build_id FK, node_id FK(nullable), phase, status,
  attempt, started_at, finished_at, log_tail, error
```

`status` lifecycle: `draft → preflighting → preflight_passed | preflight_failed → building →
completed | failed`, plus `cancelled`.

---

## 7. Phase machine

Each phase writes a `ClusterBuildStep`. Restart-safe: completed steps are skipped, so a backend
restart resumes rather than restarts.

| # | Phase | Target | Notes |
|---|---|---|---|
| 1 | `vsphere_preflight` | vCenter | power, Tools, sizing, guest OS, **anti-affinity**, snapshots |
| 2 | `node_preflight` | all, parallel | OS detect, swap, modules, ports, unique hostname + `product_uuid`, sudo, clock skew, repo reachability, free disk on the build's checked path (default `/var`), `crictl` present or `cri-tools` obtainable, **etcd disk fsync latency on CPs** |
| 3 | `base_prep` | all, parallel | proxy + CA, swap off (+`fstab`), `br_netfilter`/`overlay`, sysctl, containerd `SystemdCgroup=true`, kube packages pinned + held, `cri-tools` for `crictl` (unpinned) |
| 4 | `loadbalancer` | LB VMs | haproxy + keepalived, then **verify the VIP answers and fails over** |
| 5 | `pull_images` | all | `kubeadm config images pull` on CPs, `crictl pull` for CNI/add-on images — fails early and visibly on registry problems |
| 6 | `init` | primary CP | `kubeadm init` from a rendered `ClusterConfiguration` file (not flags), `--upload-certs`; capture join command + cert key |
| 7 | `cni` | primary CP | render bundled manifests with pod CIDR + registry overrides, apply, wait for readiness |
| 8 | `join_cp` | CPs 2 and 3, **serial** | `kubeadm join --control-plane`; **wait for etcd quorum health between each** |
| 9 | `join_workers` | workers, batched | `kubeadm join` |
| 10 | `verify` | cluster | all nodes Ready, CoreDNS running, **etcd 3-member healthy**, smoke pod, **API reachable through the VIP** |
| 11 | `onboard` | backend | fetch `admin.conf`, rewrite server to `control_plane_endpoint`, `cluster_store` save, create `Cluster` row |
| 12 | `addons` | optional | selected plugins in catalog order, each configured and then **functionally proven** — see §4.4 |

Control planes join **serially, never in parallel** — concurrent etcd member additions are a
reliable way to lose quorum on a fresh cluster.

Retry is per node: `kubeadm reset -f` + CNI/iptables cleanup, then re-run from phase 3 for that
node only. For a failed control plane, the retry must also `etcdctl member remove` the dead
member before rejoining, or the join fails on a stale member entry.

---

## 8. Security requirements

Requirements, not nice-to-haves:

1. **Log scrubbing.** `kubeadm init` output contains the bootstrap token and certificate key,
   and step logs render in the UI. A scrubbing pass runs before *any* log text is persisted.
   Dedicated unit test.
2. **Cert key** Fernet-encrypted with a 2h TTL, nulled once joins complete.
3. **VRRP auth pass** encrypted; never rendered back to the UI.
4. **Host keys.** TOFU only when the profile explicitly allows it; production profiles use
   pre-approved `SshHostKey` fingerprints. `strict` with no matching record ⇒ connection refused.
5. **Sudo passwords** never on the command line — stdin only.
6. **vCenter account** requires Read-Only. Documented, and the connection test reports the
   effective privilege set.
7. **New RBAC permissions** in `rbac_data.py`: `cluster_builds:view`, `cluster_builds:create`,
   `cluster_builds:execute`, `ssh_credentials:manage`, `vsphere:manage`. Admin-only by default;
   `execute` separable from `create` so a reviewer can gate the run.
8. **Audit** every create / preflight / start / retry / cancel / credential change.

---

## 9. API

```
GET/POST  /api/vsphere-connections            POST /api/vsphere-connections/:id/test
GET       /api/vsphere-connections/:id/vms         # the dropdown; filterable, cached

GET/POST  /api/ssh-credentials
GET/POST  /api/ssh-connection-profiles        POST /api/ssh-connection-profiles/:id/test
GET/POST  /api/build-profiles                 POST /api/build-profiles/:id/validate

GET/POST  /api/cluster-builds
GET       /api/cluster-builds/:id
POST      /api/cluster-builds/:id/preflight
POST      /api/cluster-builds/:id/start
GET       /api/cluster-builds/:id/logs?node=&since=
POST      /api/cluster-builds/:id/retry       POST /api/cluster-builds/:id/cancel
POST      /api/cluster-builds/:id/nodes            # day-2 add
DELETE    /api/cluster-builds/:id/nodes/:nodeId    # day-2 drain + reset + remove
GET       /api/cluster-builds/options              # k8s versions, CNI descriptors, OS matrix
```

Blueprint registered in `backend/api/routes/__init__.py`.

---

## 10. Frontend

`pages/ClusterBuilderPage.jsx` + `components/clusters/builder/`, lazy-loaded and routed in
`App.jsx`, permission-gated via `lib/permissionCatalog.js` / `utils/authz.js`, coach marks in
`tours/tourDefinitions.js`. Signal design system.

1. **Basics** — name, k8s version, topology preset, endpoint mode (managed haproxy default) +
   VIP, pod/service CIDR, CNI with tier badges, build profile.

   Two presets, since these are the only two shapes v1 builds:
   - **Highly available (recommended)** — 2 LB, 3 control plane, N workers
   - **Single control plane** — 1 control plane, N workers

2. **Nodes** — vCenter VM picker: searchable table with **name, power state, IP, vCPU, memory,
   guest OS, VMware Tools status, ESXi host**. Assign roles inline. Live counters against the
   preset ("2 LB ✓ · 3 control plane ✓ · 4 workers"). Rows whose Tools status is not running show
   an inline **management IP** field instead of a reported address — usable, just not
   auto-filled. The ESXi host column is deliberately visible so co-location is obvious while
   choosing, not after preflight complains. Manual host entry remains available for non-vCenter
   nodes.
3. **Preflight** — traffic-light table, node × check, with fix hints. vCenter checks grouped
   separately from SSH checks. Hard fails block; warnings overridable with a recorded ack.
4. **Build** — per-node phase timeline, live polling, log drawer, retry/cancel, link to the new
   cluster on success.

vSphere connections, SSH credentials, connection profiles and build profiles live in **Settings**
— they are reusable infrastructure records, not per-build input.

---

## 11. Open risks

1. **Backend → VM:22 reachability.** If the backend runs in-cluster, egress to the VM subnet must
   be permitted. Bastion support mitigates but does not remove it. *Proven or disproven in P0 —
   deliberately the first thing built.*
2. **VMware Tools not installed ⇒ no guest IP** from vCenter. Handled by allowing a manual IP
   override per selected VM, but a fleet without Tools makes the dropdown much less useful.
3. **vSphere portgroup security settings are the most likely HA failure.** keepalived needs VRRP
   (multicast 224.0.0.18) to pass on the segment, and if VRRP virtual MACs are used the portgroup
   must accept **Forged Transmits** and **MAC Address Changes**. Some NSX / hardened portgroups
   block this by default, and it cannot be reliably detected from inside the guest. Preflight will
   verify the VIP is free and that the two LB nodes see each other's VRRP advertisements — which
   catches it, but the fix is a vSphere networking change, not something KubeSight can apply.
4. **etcd disk latency.** Slow or contended datastores are the top cause of flaky stacked-etcd
   clusters. Preflight runs an fsync latency probe on control-plane nodes and warns above the
   etcd-recommended threshold.
5. **Mirror completeness.** A mirror missing one package version fails the build mid-flight.
   Mitigated by probing every URL in preflight rather than at install time.
6. **Half-failed `kubeadm init` or `join --control-plane`** leaves a dirty node and possibly a
   stale etcd member. The reset-and-retry path needs its own tests — it is the most likely source
   of "worked the second time" bugs.
7. **Restart mid-build** is handled by resume, but the orphan-recovery window must be tuned so a
   running build is not declared failed prematurely.

---

## 12. Testing

- `FakeSshTransport` records `(host, script)` and returns canned output; `FakeVSphere` serves
  recorded vCenter JSON. Every phase, both OS adapters and all preflight checks are unit-tested
  without a VM or a vCenter.
- `backend/tests/test_cluster_build_*.py`: phase machine incl. resume-after-restart, OS detection
  + unsupported-distro failure, anti-affinity detection, quorum enforcement (reject 2 CPs), HA
  serial-join ordering, offline bundle validation, **log scrubbing**, RBAC on every endpoint.
- `test_upgrades.py` must stay green through the OS-adapter extraction.
- Manual validation ladder:
  1. 3 Ubuntu VMs, single control plane, internet profile — proves the base path.
  2. 2 LB + 3 CP + 2 workers HA — the production shape.
  3. **Failover proof, two separate tests:** `systemctl stop haproxy` on the MASTER (proves the
     keepalived tracking script works), then power off the MASTER VM entirely (proves VRRP
     failover). The first is the one naive configs fail.
  4. Kill one control plane, confirm the API stays up through the VIP and etcd holds quorum.
  5. 1 Rocky 9 node for the RHEL adapter.
  6. One build against the internal mirror profile.

---

## 13. Delivery phases

| Phase | Days | Content |
|---|---|---|
| **P0 — SSH foundation** | 2 | paramiko dep, `services/ssh/`, credential + connection-profile + host-key models, CRUD, Settings UI, bastion, "Test connection" |
| **P1 — vSphere** | 3 | `vsphere_client.py`, `VSphereConnection` + Settings UI, VM inventory endpoint + caching, VM picker UI, vCenter-side preflight incl. anti-affinity |
| **P2 — Preflight** | 3 | OS adapters (incl. extraction from `upgrade_executor.py`), `BuildProfile` + repo modes, node preflight engine + API + results UI |
| **P3 — Build engine (single CP)** | 4 | build models + migrations, phase state machine, scheduler tick + orphan recovery, init, containerd, image pull, Calico, worker join, verify, auto-onboard |
| **P4 — HA** | 3 | `loadbalancer` role, haproxy + keepalived templating, VIP verification + failover test, `--upload-certs`, serial `join_cp`, etcd quorum checks, external-LB mode |
| **P5 — Wizard UI** | 3 | 4-step wizard, live progress, log drawer, retry/cancel, RBAC + coach marks |
| **P6 — Hardening** | 2 | day-2 add/remove node, audit + failure alerts, full test pass, real-VM validation ladder |

**≈20 working days (4 weeks) for v1.**

P0–P2 are front-loaded so the riskiest unknowns — SSH reachability from the backend, and whether
the vCenter account can be provisioned — are settled before the expensive orchestration work. At
the end of P2 the feature already earns its keep: *point KubeSight at your vCenter, pick 6 VMs,
and it tells you exactly why they are not ready to be a cluster.*

### Deferred to v2

IPAM integration for VIP allocation (fields reserved in `ClusterBuild` now, so the wizard gains
a "reserve a VIP" button without a migration), kube-vip endpoint mode, VM creation/cloning from
template, DRS anti-affinity rule creation, Cilium promotion to supported, change-bundle-style
approval gate before execute, cluster teardown. (The offline bundle builder CLI shipped — see
§4.5.)
