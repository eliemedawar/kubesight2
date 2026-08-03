from pathlib import Path

import yaml


CHART_ROOT = Path(__file__).resolve().parents[1]


def test_default_chart_can_observe_its_own_cluster():
    values = yaml.safe_load((CHART_ROOT / "values.yaml").read_text(encoding="utf-8"))

    assert values["rbac"]["create"] is True
    assert values["podSecurityContext"]["fsGroup"] == 10001
    assert values["networkPolicy"]["kubernetesApiEgress"]["enabled"] is True
    assert 443 in values["networkPolicy"]["kubernetesApiEgress"]["ports"]
    assert 6443 in values["networkPolicy"]["kubernetesApiEgress"]["ports"]


def test_network_policy_renders_configured_api_server_egress():
    template = (CHART_ROOT / "templates" / "networkpolicy.yaml").read_text(
        encoding="utf-8"
    )

    assert ".Values.networkPolicy.kubernetesApiEgress" in template
    assert "range .cidrs" in template
    assert "range .ports" in template
