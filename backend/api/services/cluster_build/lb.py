"""Managed haproxy + keepalived config for the API load-balancer tier.

Two details here decide whether HA works or is merely flaky (plan §5.3):

1. Backend health checks are mandatory. At ``kubeadm init`` time only CP1
   exists; init itself talks through the VIP, so without checks its API calls
   round-robin onto dead backends and init hangs intermittently. ``check-ssl``
   against /healthz drops not-yet-joined control planes from rotation.

2. keepalived must track haproxy, not just the node. Without ``chk_haproxy``
   the VIP stays on a host whose haproxy died while both LB VMs look healthy.
"""

from __future__ import annotations

import secrets
from typing import List, Tuple


def generate_vrrp_auth_pass() -> str:
    # keepalived truncates auth_pass to 8 characters — generate exactly 8.
    return secrets.token_urlsafe(6)[:8]


def render_haproxy_cfg(control_planes: List[Tuple[str, str]]) -> str:
    """control_planes: [(name, address)]."""
    servers = "\n".join(
        f"    server {name} {address}:6443 check check-ssl verify none"
        for name, address in control_planes
    )
    return f"""# Managed by KubeSight Cluster Builder — do not edit by hand.
global
    log /dev/log local0
    maxconn 4096

defaults
    mode tcp
    log global
    option tcplog
    timeout connect 5s
    timeout client  300s
    timeout server  300s

frontend kube_api
    bind *:6443
    default_backend kube_apiservers

backend kube_apiservers
    mode tcp
    option httpchk GET /healthz
    http-check expect status 200
    default-server inter 3s fall 3 rise 2
{servers}
"""


def render_keepalived_conf(
    *,
    is_master: bool,
    interface: str,
    router_id: int,
    auth_pass: str,
    vip: str,
    peer_addresses: List[str],
) -> str:
    state = "MASTER" if is_master else "BACKUP"
    priority = 120 if is_master else 100
    unicast_peers = "\n".join(f"        {addr}" for addr in peer_addresses)
    unicast_block = (
        f"    unicast_peer {{\n{unicast_peers}\n    }}\n" if peer_addresses else ""
    )
    return f"""# Managed by KubeSight Cluster Builder — do not edit by hand.
vrrp_script chk_haproxy {{
    script "/usr/bin/killall -0 haproxy"
    interval 2
    weight -30
    fall 2
    rise 2
}}

vrrp_instance KUBESIGHT_API {{
    state {state}
    interface {interface}
    virtual_router_id {router_id}
    priority {priority}
    advert_int 1
    authentication {{
        auth_type PASS
        auth_pass {auth_pass}
    }}
{unicast_block}    virtual_ipaddress {{
        {vip}
    }}
    track_script {{
        chk_haproxy
    }}
}}
"""


def lb_apply_script(haproxy_cfg_b64: str, keepalived_conf_b64: str) -> str:
    """Install both configs and (re)start services, validating haproxy first."""
    return f"""set -e
echo {haproxy_cfg_b64} | base64 -d > /etc/haproxy/haproxy.cfg
echo {keepalived_conf_b64} | base64 -d > /etc/keepalived/keepalived.conf
haproxy -c -f /etc/haproxy/haproxy.cfg
systemctl enable haproxy keepalived
systemctl restart haproxy
systemctl restart keepalived
sleep 2
systemctl is-active haproxy
systemctl is-active keepalived
"""


def detect_interface_script(node_address: str) -> str:
    """Print the interface carrying the node's management address (used when
    the build doesn't pin vip_interface explicitly)."""
    return f"""set -e
ip -o -4 addr show | awk '$4 ~ "^{node_address}/" {{print $2; exit}}'
"""


def vip_verify_script(vip: str) -> str:
    """Run on the LB master after config: the VIP must be locally bound."""
    return f"""set -e
ip -o -4 addr show | grep -q " {vip}/" || {{ echo "VIP {vip} is not bound locally"; exit 1; }}
echo "VIP {vip} bound"
"""


def vip_ping_script(vip: str) -> str:
    """Run from another node: the VIP must now answer."""
    return f"""set -e
ping -c 2 -W 2 {vip} > /dev/null || {{ echo "VIP {vip} does not answer from peer"; exit 1; }}
echo "VIP {vip} reachable"
"""
