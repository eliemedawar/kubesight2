"""Populate an isolated KubeSight demo and capture a screenshot-led product tour.

Safety: run this only against the disposable in-memory backend started for the
presentation.  Every business name, address and credential below is synthetic.
"""

from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

from playwright.sync_api import sync_playwright


for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass


BASE = os.environ.get("KUBESIGHT_DEMO_URL", "http://127.0.0.1:5000").rstrip("/")
ROOT = Path(__file__).resolve().parent
OUT = ROOT / "screenshots"
TOKEN = ""


def api(path: str, method: str = "GET", payload=None, *, auth: bool = True):
    url = BASE + path
    headers = {"Accept": "application/json"}
    if auth and TOKEN:
        headers["Authorization"] = f"Bearer {TOKEN}"
    data = None
    if payload is not None:
        headers["Content-Type"] = "application/json"
        data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=15) as response:
            raw = response.read().decode("utf-8")
            body = json.loads(raw) if raw else {}
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"{method} {path} -> {exc.code}: {raw[:600]}") from exc
    except Exception as exc:
        raise RuntimeError(f"{method} {path} failed: {exc}") from exc
    if isinstance(body, dict) and body.get("success") is False:
        raise RuntimeError(f"{method} {path}: {body.get('error') or body}")
    return body.get("data", body) if isinstance(body, dict) else body


def safe_api(label: str, path: str, method: str = "GET", payload=None, *, auth=True):
    try:
        result = api(path, method, payload, auth=auth)
        print(f"[seed] {label}: ok", flush=True)
        return result
    except Exception as exc:
        print(f"[seed] {label}: skipped ({exc})", flush=True)
        return None


def items(path: str):
    data = safe_api(f"list {path}", path) or {}
    if isinstance(data, list):
        return data
    return data.get("items", []) if isinstance(data, dict) else []


def ensure_named(path: str, name: str, payload: dict, *, update=False):
    for row in items(path):
        if str(row.get("name", "")).strip().lower() == name.lower():
            if update and row.get("id"):
                updated = safe_api(f"update {name}", f"{path}/{row['id']}", "PUT", payload)
                return updated or row
            return row
    return safe_api(f"create {name}", path, "POST", payload)


def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def seed_demo() -> dict:
    global TOKEN
    health = api("/health", auth=False)
    if str(health.get("kubernetesMode", "")).lower() != "mock":
        raise RuntimeError("Refusing to seed: backend is not in Kubernetes mock mode")
    login = api("/api/auth/login", "POST", {"username": "admin", "password": "admin123"}, auth=False)
    TOKEN = login.get("token", "")
    if not TOKEN:
        raise RuntimeError(f"Admin login did not return an access token: {login}")
    print("[seed] Connected to isolated mock backend", flush=True)

    receivers = []
    for payload in [
        {"name": "Platform On-Call", "type": "email", "emailAddress": "oncall@demo.example", "enabled": True},
        {"name": "Payments Owner", "type": "email", "emailAddress": "payments.owner@demo.example", "enabled": True},
        {"name": "SRE Slack", "type": "slack", "url": "https://hooks.slack.com/services/DEMO/ONLY/NOT-A-SECRET", "enabled": True},
    ]:
        row = ensure_named("/api/alert-routing/receivers", payload["name"], payload)
        if row:
            receivers.append(row)
    group_payload = {
        "name": "Production Operations",
        "description": "Primary routing group for production platform signals.",
        "enabled": True,
        "receiverIds": [r["id"] for r in receivers if r.get("id")],
        "emailList": "noc@demo.example",
    }
    group = ensure_named("/api/alert-routing/receiver-groups", group_payload["name"], group_payload)

    policy_specs = [
        ("Payments API CPU saturation", "critical", "cpu_usage_percent", 80, "payments-api"),
        ("Ledger worker memory pressure", "warning", "memory_usage_percent", 72, "ledger-worker"),
        ("Checkout API availability", "critical", "cpu_usage_percent", 90, "checkout-api"),
    ]
    for name, severity, metric, threshold, resource in policy_specs:
        payload = {
            "name": name,
            "clusterId": "prod-us-east",
            "description": f"Synthetic demonstration policy for {resource}.",
            "enabled": True,
            "severity": severity,
            "conditionLogic": "any",
            "conditions": [{"metricKey": metric, "operator": ">", "threshold": threshold}],
            "scope": {"type": "deployment", "namespace": "payments", "resourceName": resource},
            "showOnDashboard": True,
            "evaluationIntervalSeconds": 60,
            "receiverGroupIds": [group["id"]] if group and group.get("id") else [],
        }
        ensure_named("/api/alert-policies", name, payload)
    safe_api("evaluate demo policies", "/api/alert-policies/evaluate", "POST", {"clusterId": "prod-us-east"})

    component_specs = [
        ("Edge WAF", "security", "Public edge policy enforcement"),
        ("API Gateway", "gateway", "Authenticated API routing"),
        ("Payments API", "application", "Merchant payments workload"),
        ("Ledger Worker", "worker", "Asynchronous ledger processing"),
        ("PostgreSQL Ledger", "database", "Highly available transaction store"),
        ("Redis Cache", "cache", "Low-latency session and idempotency cache"),
        ("Checkout API", "application", "Checkout orchestration"),
        ("Prometheus", "monitoring", "Metrics and alert evaluation"),
    ]
    components = {}
    for name, category, description in component_specs:
        payload = {
            "name": name,
            "category": category,
            "description": description,
            "checkType": "webhook",
            "heartbeatIntervalSeconds": 300,
        }
        row = ensure_named("/api/topology-components", name, payload)
        if row:
            components[name] = row
            token = row.get("webhookToken")
            if token and row.get("id"):
                safe_api(
                    f"heartbeat {name}",
                    f"/api/topology-components/{row['id']}/heartbeat?token={token}",
                    "POST",
                    {"status": "healthy"},
                    auth=False,
                )

    def node(temp_id, name, kind, x, y, component_name=None, description=""):
        result = {
            "tempId": temp_id,
            "name": name,
            "type": kind,
            "description": description,
            "positionX": x,
            "positionY": y,
        }
        comp = components.get(component_name or name)
        if comp and comp.get("id"):
            result["componentId"] = comp["id"]
        return result

    payments_topology = {
        "nodes": [
            node("waf", "Edge WAF", "Security", 80, 160, description="TLS termination and WAF rules"),
            node("gateway", "API Gateway", "Gateway", 290, 160, description="OAuth and request routing"),
            node("api", "Payments API", "Application", 510, 95, description="Kubernetes deployment: payments-api"),
            node("worker", "Ledger Worker", "Worker", 510, 255, description="Kubernetes deployment: ledger-worker"),
            node("db", "PostgreSQL Ledger", "Database", 755, 95, description="Primary transaction store"),
            node("cache", "Redis Cache", "Cache", 755, 255, description="Idempotency and session cache"),
        ],
        "edges": [
            {"sourceTempId": "waf", "targetTempId": "gateway", "protocol": "HTTPS", "scope": "external"},
            {"sourceTempId": "gateway", "targetTempId": "api", "protocol": "HTTPS", "scope": "internal"},
            {"sourceTempId": "gateway", "targetTempId": "worker", "protocol": "AMQP", "scope": "internal"},
            {"sourceTempId": "api", "targetTempId": "db", "protocol": "PostgreSQL", "scope": "internal"},
            {"sourceTempId": "api", "targetTempId": "cache", "protocol": "RESP", "scope": "internal"},
            {"sourceTempId": "worker", "targetTempId": "db", "protocol": "PostgreSQL", "scope": "internal"},
        ],
    }
    service_payloads = [
        {
            "name": "Merchant Payments API",
            "description": "Production merchant payments, ledger processing and settlement dependencies.",
            "deployments": [
                {"clusterId": "prod-us-east", "namespace": "payments", "deploymentName": "payments-api", "kind": "deployment"},
                {"clusterId": "prod-us-east", "namespace": "payments", "deploymentName": "ledger-worker", "kind": "deployment"},
            ],
            "topology": payments_topology,
        },
        {
            "name": "Checkout Experience",
            "description": "Customer checkout orchestration and session services.",
            "deployments": [{"clusterId": "prod-us-east", "namespace": "checkout", "deploymentName": "checkout-api", "kind": "deployment"}],
            "topology": {
                "nodes": [
                    node("checkout", "Checkout API", "Application", 220, 150),
                    node("cache", "Redis Cache", "Cache", 560, 150),
                ],
                "edges": [{"sourceTempId": "checkout", "targetTempId": "cache", "protocol": "RESP"}],
            },
        },
        {
            "name": "Observability Stack",
            "description": "Shared metrics, alerting and operational telemetry.",
            "deployments": [{"clusterId": "prod-us-east", "namespace": "monitoring", "deploymentName": "prometheus", "kind": "deployment"}],
            "topology": {"nodes": [node("prom", "Prometheus", "Monitoring", 300, 160)], "edges": []},
        },
    ]
    services = {}
    for payload in service_payloads:
        existing = next((r for r in items("/api/application-services") if r.get("name") == payload["name"]), None)
        row = existing or safe_api(f"create {payload['name']}", "/api/application-services", "POST", payload)
        if row and row.get("id"):
            full = safe_api(f"load {payload['name']}", f"/api/application-services/{row['id']}") or row
            services[payload["name"]] = full

    payments = services.get("Merchant Payments API")
    checkout = services.get("Checkout Experience")
    client_specs = [
        {
            "name": "Northstar Retail",
            "contactPerson": "Maya Reed",
            "email": "maya.reed@northstar.demo.example",
            "phone": "+1 555 010 2200",
            "notes": "Enterprise client · 99.95% availability objective · synthetic demo record",
            "serviceIds": [s["id"] for s in (payments, checkout) if s and s.get("id")],
        },
        {
            "name": "Meridian Logistics",
            "contactPerson": "Noah Blake",
            "email": "noah.blake@meridian.demo.example",
            "phone": "+1 555 010 3300",
            "notes": "Regional logistics integration · synthetic demo record",
            "serviceIds": [checkout["id"]] if checkout and checkout.get("id") else [],
        },
        {
            "name": "Horizon Labs",
            "contactPerson": "Ava Chen",
            "email": "ava.chen@horizon.demo.example",
            "phone": "+1 555 010 4400",
            "notes": "Sandbox integration partner · synthetic demo record",
            "serviceIds": [payments["id"]] if payments and payments.get("id") else [],
        },
    ]
    clients = {}
    for payload in client_specs:
        existing = next((r for r in items("/api/clients") if r.get("name") == payload["name"]), None)
        row = safe_api(f"update {payload['name']}", f"/api/clients/{existing['id']}", "PUT", payload) if existing else safe_api(f"create {payload['name']}", "/api/clients", "POST", payload)
        if row:
            clients[payload["name"]] = row

    northstar = clients.get("Northstar Retail")
    if northstar and payments:
        saved_nodes = (payments.get("topology") or {}).get("nodes") or []
        refs = []
        for wanted, src, dst in [
            ("Edge WAF", "10.42.12.10", "172.20.10.10"),
        ]:
            match = next((n for n in saved_nodes if n.get("name") == wanted), None)
            if match and match.get("id") is not None:
                refs.append({"ref": str(match["id"]), "sourceIp": src, "destinationIp": dst})
        safe_api(
            "configure Northstar connectivity",
            f"/api/clients/{northstar['id']}/services/{payments['id']}/connection",
            "POST",
            {
                "sourceIp": "10.42.12.10",
                "destinationIp": "172.20.10.25",
                "transportType": "Private Link",
                "transportName": "Northstar Private Link",
                "transportNotes": "Encrypted, redundant private endpoints in two availability zones.",
                "clusterId": "prod-us-east",
                "namespace": "payments",
                "environment": "Production",
                "componentRefs": refs,
                # A one-way client-to-service path also keeps the layered
                # topology acyclic and therefore maximally readable.
                "direction": "inbound",
                "status": "active",
                "isActive": True,
            },
        )

    blueprints = [
        {
            "name": "Merchant API Stack",
            "description": "Production-ready edge, API, worker, data and cache blueprint.",
            "category": "Payments", "ownerTeam": "Platform Engineering", "criticality": "critical", "status": "ready", "version": "2.1.0",
            "components": [
                {"tempId": "edge", "name": "Ingress", "role": "edge", "componentType": "ingress", "required": True, "defaultPort": 443, "position": 0},
                {"tempId": "api", "name": "Payments API", "role": "backend", "componentType": "deployment", "required": True, "defaultPort": 8080, "position": 1},
                {"tempId": "worker", "name": "Ledger Worker", "role": "worker", "componentType": "worker", "required": True, "position": 2},
                {"tempId": "db", "name": "PostgreSQL", "role": "database", "componentType": "database", "required": True, "supportsExternal": True, "defaultPort": 5432, "position": 3},
                {"tempId": "cache", "name": "Redis", "role": "cache", "componentType": "redis", "required": False, "defaultPort": 6379, "position": 4},
            ],
            "connections": [
                {"sourceTempId": "edge", "targetTempId": "api", "protocol": "HTTPS", "port": 8080},
                {"sourceTempId": "api", "targetTempId": "db", "protocol": "TCP", "port": 5432},
                {"sourceTempId": "api", "targetTempId": "cache", "protocol": "TCP", "port": 6379},
                {"sourceTempId": "api", "targetTempId": "worker", "protocol": "AMQP", "port": 5672},
            ],
            "requirements": [
                {"componentTempId": "edge", "key": "INGRESS_HOST", "requirementType": "ingress_host", "required": True, "valueSource": "manual"},
                {"componentTempId": "api", "key": "IMAGE_TAG", "requirementType": "env_var", "required": True, "valueSource": "manual"},
                {"componentTempId": "db", "key": "DB_PASSWORD", "requirementType": "secret", "required": True, "valueSource": "generated", "secret": True, "autoGenerate": True},
            ],
        },
        {
            "name": "Digital Checkout", "description": "Composable web checkout and backend API.", "category": "Commerce", "ownerTeam": "Experience", "criticality": "high", "status": "ready", "version": "1.4.0",
            "components": [
                {"tempId": "web", "name": "Checkout Web", "role": "frontend", "componentType": "deployment", "required": True, "position": 0},
                {"tempId": "api", "name": "Checkout API", "role": "backend", "componentType": "deployment", "required": True, "position": 1},
            ],
            "connections": [{"sourceTempId": "web", "targetTempId": "api", "protocol": "HTTPS", "port": 8080}],
            "requirements": [],
        },
        {
            "name": "Observability Baseline", "description": "Metrics, dashboards and alert routing for every namespace.", "category": "Platform", "ownerTeam": "SRE", "criticality": "medium", "status": "ready", "version": "3.0.0",
            "components": [
                {"tempId": "metrics", "name": "Metrics Collector", "role": "monitoring", "componentType": "deployment", "required": True, "position": 0},
                {"tempId": "alerts", "name": "Alert Router", "role": "alerting", "componentType": "deployment", "required": True, "position": 1},
            ],
            "connections": [{"sourceTempId": "metrics", "targetTempId": "alerts", "protocol": "HTTP", "port": 9093}],
            "requirements": [],
        },
    ]
    for payload in blueprints:
        ensure_named("/api/service-blueprints", payload["name"], payload)

    templates = [
        ("Payments API", "Business Services", "registry.demo.example/payments-api", "2.9.0", 8080),
        ("Ledger Worker", "Background Workers", "registry.demo.example/ledger-worker", "1.8.2", 9090),
        ("Checkout API", "Business Services", "registry.demo.example/checkout-api", "4.3.1", 8080),
    ]
    for name, category, image, tag, port in templates:
        payload = {
            "name": name, "description": f"Governed template for {name}.", "category": category, "workloadType": "Deployment",
            "containers": [{"name": "app", "image": image, "tag": tag, "ports": [port]}],
            "resources": {"cpuRequest": "250m", "cpuLimit": "1", "memoryRequest": "256Mi", "memoryLimit": "1Gi"},
            "networking": {"service": {"enabled": True, "type": "ClusterIP", "port": port, "targetPort": port}, "ingress": {"enabled": True, "host": f"{name.lower().replace(' ', '-')}.demo.example", "path": "/"}},
            "scaling": {"replicas": 3, "hpa": {"enabled": True, "minReplicas": 3, "maxReplicas": 10}},
            "schema": {"overrides": {"tag": True, "replicas": True}, "env": [{"key": "LOG_LEVEL", "default": "INFO"}], "dependencies": []},
        }
        ensure_named("/api/inventory/deploy/wizard/templates", name, payload)

    registry_payloads = [
        {"name": "Corporate Nexus", "registryType": "generic", "baseUrl": "https://registry.demo.example", "authMode": "basic", "username": "svc_kubesight", "password": "demo-only-not-a-secret", "imageHosts": ["registry.demo.example"], "verifyTls": True, "enforcement": "block", "enabled": True},
        {"name": "Partner Mirror", "registryType": "generic", "baseUrl": "https://mirror.demo.example", "authMode": "none", "imageHosts": ["mirror.demo.example"], "verifyTls": True, "enforcement": "warn", "enabled": True},
    ]
    registries = {}
    for payload in registry_payloads:
        row = ensure_named("/api/registries", payload["name"], payload)
        if row:
            registries[payload["name"]] = row

    safe_api("configure approval policy", "/api/deployment-requests/recipients", "PUT", {"recipients": ["approver@demo.example"], "requiredApprovals": 1, "clusterApprovals": {"prod-us-east": 1}})
    existing_requests = items("/api/deployment-requests")
    if not existing_requests:
        start = datetime.now(timezone.utc) + timedelta(days=1)
        request_specs = [
            ("Scale payments-api for the quarterly retail campaign.", 0),
            ("Roll out checkout-api 4.3.1 after smoke validation.", 3),
            ("Refresh observability collectors across production.", 6),
        ]
        created = []
        for message, hours in request_specs:
            row = safe_api("create deployment request", "/api/deployment-requests", "POST", {"clusterId": "prod-us-east", "clusterName": "Production US-East", "message": message, "windowStart": iso(start + timedelta(hours=hours)), "windowEnd": iso(start + timedelta(hours=hours + 2)), "windowTimezone": "Asia/Beirut"})
            if row:
                created.append(row)
        if created and created[0].get("id"):
            safe_api("approve demo request", f"/api/deployment-requests/{created[0]['id']}/approve", "POST", {})
        if len(created) > 1 and created[1].get("id"):
            safe_api("decline demo request", f"/api/deployment-requests/{created[1]['id']}/decline", "POST", {"reason": "Move to the next maintenance window."})

    draft = safe_api("load change bundle draft", "/api/change-bundles/draft")
    if draft and draft.get("id") and not draft.get("items"):
        safe_api("add scale change", f"/api/change-bundles/{draft['id']}/items", "POST", {"actionType": "scale_replicas", "clusterId": "prod-us-east", "clusterName": "Production US-East", "namespace": "payments", "resourceKind": "Deployment", "resourceName": "payments-api", "replicas": 12})
        safe_api("add cleanup change", f"/api/change-bundles/{draft['id']}/items", "POST", {"actionType": "delete_deployment", "clusterId": "prod-us-east", "clusterName": "Production US-East", "namespace": "payments", "resourceKind": "Deployment", "resourceName": "legacy-payments-canary"})
        start = datetime.now(timezone.utc) + timedelta(days=2)
        safe_api("submit change bundle", f"/api/change-bundles/{draft['id']}/submit", "POST", {"note": "Q3 merchant payments release", "windowStart": iso(start), "windowEnd": iso(start + timedelta(hours=2)), "windowTimezone": "Asia/Beirut", "stopOnFailure": True})

    zoho_config = {
        "enabled": True,
        "apiBase": "https://desk.zoho.com/api/v1", "accountsBase": "https://accounts.zoho.com", "tokenEndpoint": "https://accounts.zoho.com/oauth/v2/token",
        "orgId": "DEMO-ORG-1001", "departmentId": "Payments Operations", "layoutId": "DEVOPS-DEMO-LAYOUT",
        "appFieldId": "FIELD-APP-DEMO", "appFieldApiName": "cf_application", "environmentFieldId": "FIELD-ENV-DEMO", "environmentFieldApiName": "cf_environment",
        "tagFieldApiName": "cf_tag", "variableFieldId": "FIELD-VAR-DEMO", "variableFieldApiName": "cf_variable", "valueFieldId": "FIELD-VALUE-DEMO", "valueFieldApiName": "cf_value",
        "clientId": "demo-client-id", "clientSecret": "demo-client-secret", "refreshToken": "demo-refresh-token", "inboundSecret": "demo-inbound-secret",
        "syncApplication": True, "syncEnvironment": True, "syncVariables": True, "cascadeEnabled": True, "syncIntervalMinutes": 30, "ticketWritebackEnabled": True,
    }
    safe_api("configure Zoho demo", "/api/zoho/config", "PUT", zoho_config)
    safe_api("configure Zoho field source", "/api/zoho/source", "PUT", {"clusterId": "prod-us-east", "selectedNamespaces": ["payments", "checkout"], "selectedDeployments": {"payments": {"all": False, "names": ["payments-api", "ledger-worker"]}, "checkout": {"all": True, "names": []}}, "customEnvironments": [{"name": "Sandbox", "applications": ["demo-payments"], "jenkinsJobPath": "demo/sandbox-router", "jenkinsParams": {"APP": "{application}", "TAG": "{tag}", "NAMESPACE": "{environment}"}}]})
    registry_id = (registries.get("Corporate Nexus") or {}).get("id")
    jenkins_payload = {"enabled": True, "baseUrl": "https://jenkins.demo.example", "username": "kubesight-demo", "apiToken": "demo-token", "routerJobPath": "platform/deploy-router", "verifyTls": True, "sendParamApp": True, "sendParamNamespace": True, "sendParamTag": True, "autoRunTickets": False, "imageTagTemplate": "v{tag}"}
    if registry_id:
        jenkins_payload["registryConnectionId"] = registry_id
    safe_api("configure Jenkins demo", "/api/zoho/jenkins", "PUT", jenkins_payload)

    return {"northstar": northstar, "payments": payments}


def capture_demo() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    chrome = r"C:\Program Files\Google\Chrome\Application\chrome.exe"
    if not Path(chrome).exists():
        raise RuntimeError(f"Chrome not found at {chrome}")

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, executable_path=chrome, args=["--no-sandbox", "--disable-gpu"])
        context = browser.new_context(viewport={"width": 1600, "height": 900}, device_scale_factor=1, color_scheme="light", locale="en-US")
        context.add_init_script("localStorage.setItem('kubesight-theme','light');")

        synthetic_layout = {
            "success": True,
            "data": {
                "layoutId": "DEVOPS-DEMO-LAYOUT", "layoutName": "DevOps Request",
                "sections": [
                    {"id": "deployment", "name": "Deployment", "fields": [
                        {"id": "FIELD-APP-DEMO", "apiName": "cf_application", "displayLabel": "Application", "type": "Pick List", "optionCount": 4},
                        {"id": "FIELD-ENV-DEMO", "apiName": "cf_environment", "displayLabel": "Environment", "type": "Pick List", "optionCount": 3},
                        {"id": "FIELD-TAG-DEMO", "apiName": "cf_tag", "displayLabel": "Release tag", "type": "Single Line", "optionCount": 0},
                    ]},
                    {"id": "variables", "name": "Runtime variables", "fields": [
                        {"id": "FIELD-VAR-DEMO", "apiName": "cf_variable", "displayLabel": "Variable", "type": "Pick List", "optionCount": 5},
                        {"id": "FIELD-VALUE-DEMO", "apiName": "cf_value", "displayLabel": "Value", "type": "Single Line", "optionCount": 0},
                    ]},
                ],
            },
            "error": None,
        }
        context.route("**/api/zoho/layout*", lambda route: route.fulfill(status=200, content_type="application/json", body=json.dumps(synthetic_layout)))
        page = context.new_page()
        page.add_style_tag(content="*{transition:none!important;animation:none!important;caret-color:transparent!important} .fab{display:none!important}")

        def shot(name: str, wait_ms: int = 900):
            page.wait_for_timeout(wait_ms)
            path = OUT / name
            page.screenshot(path=str(path), full_page=False)
            print(f"[capture] {name}", flush=True)

        def optional(label: str, fn):
            try:
                fn()
                return True
            except Exception as exc:
                print(f"[capture] optional {label} skipped: {exc}", flush=True)
                return False

        def nav(label: str, wait_text: str | None = None):
            button = page.locator("aside[aria-label='Primary navigation']").get_by_role("button", name=label, exact=True)
            button.scroll_into_view_if_needed()
            button.click()
            if wait_text:
                page.get_by_text(wait_text, exact=False).first.wait_for(timeout=12000)
            page.wait_for_timeout(900)

        page.goto(BASE, wait_until="domcontentloaded", timeout=30000)
        page.get_by_label("Username or email").wait_for(timeout=15000)
        shot("01_login.png", 300)
        page.get_by_label("Username or email").fill("admin")
        page.get_by_label("Password").fill("admin123")
        page.get_by_role("button", name="Sign In", exact=True).click()
        page.get_by_text("Operations Dashboard", exact=False).first.wait_for(timeout=20000)
        shot("02_dashboard.png", 1800)

        nav("Clusters")
        shot("03_clusters.png")
        nav("Cluster Management")
        optional("Add Cluster modal", lambda: page.get_by_role("button", name="Add Cluster", exact=True).click())
        optional("Add Cluster dialog wait", lambda: page.get_by_role("dialog").wait_for(timeout=8000))
        shot("04_cluster_management_modal.png")
        optional("close Add Cluster", lambda: page.get_by_role("dialog").get_by_role("button", name="Close").click())

        nav("Namespaces")
        shot("05_namespaces.png")
        nav("Resources")
        optional("Pods tab", lambda: page.locator("nav[aria-label='resource-tabs']").get_by_role("button", name="Pods", exact=True).click())
        shot("06_resources_pods.png")
        optional("Deployments tab", lambda: page.locator("nav[aria-label='resource-tabs']").get_by_role("button", name="Deployments", exact=True).click())
        shot("07_resources_deployments.png")

        nav("Logs")
        def choose_log_value(label_text: str, option_text: str):
            wrapper = page.locator("label").filter(has_text=label_text).first
            wrapper.get_by_role("button").click()
            page.get_by_role("option", name=option_text, exact=True).click()
            page.wait_for_timeout(700)
        optional("logs namespace payments", lambda: choose_log_value("Namespace", "payments"))
        optional("logs pod", lambda: choose_log_value("Pod", "payments-api-84b5d5"))
        def choose_first_log_container():
            wrapper = page.locator("label").filter(has_text="Container").first
            wrapper.get_by_role("button").click()
            page.get_by_role("option").first.click()
            page.wait_for_timeout(1000)
        optional("logs first container", choose_first_log_container)
        shot("08_logs.png", 1400)
        nav("Alerts")
        # Open is the default tab. Keep the tour progressing even if a lazy tab
        # chunk takes longer than expected to become interactive.
        shot("09_alerts_open.png", 1600)
        optional("alerts history tab", lambda: page.get_by_role("tab", name="History", exact=True).click(timeout=10000))
        shot("10_alerts_history.png")
        optional("alerts policies tab", lambda: page.get_by_role("tab", name="Policies", exact=True).click(timeout=10000))
        shot("11_alerts_policies.png")
        optional("alerts routing tab", lambda: page.get_by_role("tab", name="Routing", exact=True).click(timeout=10000))
        optional("routing receivers", lambda: page.get_by_role("button", name="Receivers", exact=True).click())
        optional("receiver groups", lambda: page.get_by_role("button", name="Receiver Groups", exact=True).click())
        shot("12_alerts_routing.png")

        nav("Inventory")
        shot("13_inventory_templates.png", 1200)
        optional("application builder", lambda: page.get_by_role("button", name="Start From Scratch", exact=True).first.click())
        optional("application builder wait", lambda: page.get_by_role("dialog").filter(has_text="Application Builder").wait_for(timeout=10000))
        shot("14_application_builder.png")
        optional("close application builder", lambda: page.get_by_role("dialog").filter(has_text="Application Builder").get_by_role("button", name="Close").click())

        nav("My Requests")
        shot("15_my_requests.png")
        nav("Change Bundles")
        optional("all bundles", lambda: page.get_by_role("button", name="All Bundles", exact=True).click())
        shot("16_change_bundles.png")
        nav("Deployment Requests")
        shot("17_deployment_requests.png")

        nav("Service Catalog")
        shot("18_service_catalog.png")
        optional("blueprint detail", lambda: page.get_by_role("button", name="View blueprint Merchant API Stack", exact=True).click())
        shot("19_service_blueprint_detail.png")
        nav("App Services")
        shot("20_app_services.png", 1200)
        optional("open app service", lambda: page.get_by_role("row").filter(has_text="Merchant Payments API").click())
        optional("app service dialog", lambda: page.get_by_role("dialog").filter(has_text="Merchant Payments API").wait_for(timeout=10000))
        shot("21_app_service_topology.png")
        optional("close app service", lambda: page.keyboard.press("Escape"))
        nav("Components")
        shot("22_components.png")

        nav("Clients")
        shot("23_clients.png")
        optional("open Northstar", lambda: page.get_by_role("row").filter(has_text="Northstar Retail").click())
        optional("Northstar dialog", lambda: page.get_by_role("dialog").filter(has_text="Northstar Retail").wait_for(timeout=10000))
        optional("client services tab", lambda: page.get_by_role("dialog").filter(has_text="Northstar Retail").get_by_role("tab", name="Services", exact=True).click())
        optional("view payments topology", lambda: page.get_by_role("dialog").filter(has_text="Northstar Retail").get_by_role("button", name="View Topology", exact=True).first.click())
        page.wait_for_timeout(1200)
        optional("fullscreen client topology", lambda: page.get_by_role("button", name="View topology fullscreen", exact=True).click(timeout=10000))
        shot("24_client_topology.png")
        optional("exit client topology fullscreen", lambda: page.keyboard.press("Escape"))
        optional("client access tab", lambda: page.get_by_role("dialog").filter(has_text="Northstar Retail").get_by_role("tab", name="Access Details", exact=True).click())
        shot("25_client_access.png")
        optional("close Northstar", lambda: page.keyboard.press("Escape"))

        nav("User Management")
        shot("26_user_management.png")
        optional("roles tab", lambda: page.locator("nav[aria-label='user-management-tabs']").get_by_role("button", name="Roles", exact=True).click())
        shot("27_roles.png")
        nav("Audit Logs")
        shot("28_audit_logs.png", 1200)
        nav("Image Registries")
        shot("29_registries.png")
        nav("Zoho Integration")
        optional("Zoho overview tab", lambda: page.get_by_role("tab", name="Overview", exact=True).click(timeout=10000))
        shot("30_zoho_overview.png")
        optional("Zoho field sync tab", lambda: page.get_by_role("tab", name="Field sync", exact=True).click(timeout=10000))
        shot("31_zoho_fieldsync.png", 1500)
        nav("Settings")
        shot("32_settings.png")
        nav("Upgrade Center")
        optional("run precheck", lambda: page.get_by_role("button", name="Run Precheck", exact=True).first.click())
        shot("33_upgrade_precheck.png", 1700)

        browser.close()


def main() -> int:
    print(f"KubeSight demo target: {BASE}", flush=True)
    seed_demo()
    capture_demo()
    pngs = sorted(OUT.glob("*.png"))
    print(f"Complete: {len(pngs)} screenshots in {OUT}", flush=True)
    if len(pngs) < 28:
        print("ERROR: too few screenshots were captured", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
