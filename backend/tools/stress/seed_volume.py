"""Fill a KubeSight database with the VOLUME of a busy two-year-old installation.

An empty database makes every list page look fast. This script writes, through
the app's own ORM models, roughly what a fintech estate accumulates in two
years — users, audit trail, alert history, CI services with thousands of
builds, deployment requests, change bundles, inventory templates and version
history, app services and clients, mobile releases — so page and API timings
are measured against realistic row counts.

    cd backend
    python tools/stress/seed_volume.py --database-url sqlite:///C:/path/stress.db

Options:
    --scale 0.5         halve every volume (2.0 doubles it)
    --password ...      shared password of the stress-user-NN accounts
    --force             seed again even though stress users already exist (adds
                        another batch; slugs/names get a -2, -3... suffix)
    --seed N            random seed (row counts and shapes are deterministic for
                        a seed; timestamps are relative to "now")

A default run (scale 1.0) takes about a minute on SQLite and adds ~60k audit
rows, 20k alert history rows, ~12k CI builds / ~53k build stages, 3k
deployment requests, 1.5k change bundles, 300 templates, 5k app versions.

Everything references the custom clusters registered in the target database
(``clusters`` table, public ids ``custom-<id>``), so rows line up with what
the clusters page shows. Nothing is executed: no build runs, no bundle
deploys, no request is pending against a window that opens soon. Builds are
all terminal, bundles are terminal or far in the future, so the CI engine and
the bundle executor of a server started on this database have nothing to do.

The app is constructed with ``TESTING=True`` so it starts none of its
background threads (alert scheduler, CI engine, cache warmer); migrations and
the default seed are then run explicitly, exactly as a normal start does.
"""

from __future__ import annotations

import argparse
import hashlib
import os
import random
import sys
import tempfile
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Sequence, Tuple

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.abspath(os.path.join(_HERE, "..", ".."))


# ---------------------------------------------------------------------------
# Vocabulary
# ---------------------------------------------------------------------------

DOMAINS = [
    "payments", "issuing", "acquiring", "switch", "txm", "wallet", "cards", "kyc",
    "notifications", "gateway", "reporting", "settlement", "fraud", "loyalty",
    "merchant", "onboarding", "billing", "ledger", "auth", "statements",
]
COMPONENTS = [
    "api", "worker", "scheduler", "gateway", "processor", "consumer", "adapter",
    "web", "admin", "sync", "notifier", "exporter", "orchestrator", "router",
]
TEAMS = {
    "payments": "Payments Core", "issuing": "Card Issuing", "acquiring": "Acquiring",
    "switch": "Switch", "txm": "Transaction Monitoring", "wallet": "Digital Wallet",
    "cards": "Card Issuing", "kyc": "Compliance & KYC", "notifications": "Platform",
    "gateway": "Platform", "reporting": "Data & Reporting", "settlement": "Settlement",
    "fraud": "Risk & Fraud", "loyalty": "Loyalty", "merchant": "Merchant Services",
    "onboarding": "Digital Onboarding", "billing": "Billing", "ledger": "Core Ledger",
    "auth": "Identity", "statements": "Data & Reporting",
}
FIRST_NAMES = [
    "Rami", "Nadine", "Karim", "Lara", "Hadi", "Maya", "Omar", "Yara", "Ziad", "Rana",
    "Georges", "Mira", "Tarek", "Joelle", "Fadi", "Carla", "Samir", "Dana", "Elias", "Rita",
    "Walid", "Nour", "Bassel", "Tala", "Jad", "Lea", "Marwan", "Sara", "Anthony", "Hiba",
]
LAST_NAMES = [
    "Haddad", "Khoury", "Saad", "Nassar", "Aoun", "Hajj", "Gemayel", "Karam", "Salameh",
    "Mansour", "Fares", "Chamoun", "Daher", "Rizk", "Sleiman", "Tannous", "Abboud", "Najjar",
]
TIMEZONES = ["Asia/Beirut", "Asia/Beirut", "Asia/Beirut", "Asia/Dubai", "Europe/London", "UTC"]

# (component suffix, application type) — what the CI catalog is made of.
CI_KINDS = [
    ("api", "java_maven"), ("core", "java_maven"), ("worker", "java_gradle"),
    ("processor", "java_gradle"), ("adapter", "java_maven"), ("consumer", "java_gradle"),
    ("scheduler", "java_maven"), ("bff", "node"), ("portal", "node"), ("web", "node"),
    ("notifier", "node"), ("exporter", "python"), ("scoring", "python"), ("etl", "python"),
    ("gateway", "container"), ("proxy", "container"), ("batch", "generic"),
]
MOBILE_KINDS = [("android", "android"), ("ios", "ios"), ("agent-app", "flutter")]
BRANCH_FEATURE_WORDS = [
    "3ds-retry", "settlement-cutoff", "pci-masking", "kafka-upgrade", "card-limits",
    "chargeback-flow", "iso8583-fields", "fx-rates", "otp-resend", "audit-export",
    "merchant-onboarding", "token-refresh", "rate-limit", "spring-boot-3", "jdk17",
    "batch-retries", "pin-change", "wallet-topup", "refund-api", "statement-pdf",
]
COMMIT_MESSAGES = [
    "Fix NPE when the issuer response has no auth code",
    "Bump spring-boot to 3.2.5",
    "Add retry with backoff on the switch connector",
    "Mask PAN in debug logs",
    "Raise Kafka consumer max.poll.records to 200",
    "Handle 3DS v2 frictionless flow",
    "Refactor settlement batch to stream records",
    "Add index on transactions(created_at)",
    "Update ISO 8583 field 55 parsing",
    "Expose /actuator/prometheus behind auth",
    "Fix timezone in daily statement cutoff",
    "Merge branch 'develop' into release",
    "Add contract tests for the merchant API",
    "Upgrade base image to eclipse-temurin 17",
    "Tune Hikari pool for the reporting replica",
    "Remove deprecated v1 endpoints",
    "Add idempotency key to refund requests",
    "Fix flaky CardLimitServiceTest",
    "Translate notification templates (AR/FR)",
    "Lower log level of health checks",
]


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------

def _now() -> datetime:
    return datetime.now(timezone.utc)


def _scaled(value: float, scale: float, minimum: int = 1) -> int:
    return max(minimum, int(round(value * scale)))


def _hex(rng: random.Random, n: int) -> str:
    return "".join(rng.choice("0123456789abcdef") for _ in range(n))


def _sha(rng: random.Random) -> str:
    return hashlib.sha1(str(rng.random()).encode()).hexdigest()


def _sha256(rng: random.Random) -> str:
    return hashlib.sha256(str(rng.random()).encode()).hexdigest()


def _ip(rng: random.Random) -> str:
    return rng.choice(["10.20", "10.30", "172.16", "192.168"]) + f".{rng.randint(0, 254)}.{rng.randint(2, 254)}"


def rand_time(
    rng: random.Random,
    start: datetime,
    end: datetime,
    *,
    growth: float = 1.0,
    business: float = 0.85,
) -> datetime:
    """A moment in [start, end): busier towards the end (the installation grew)
    and mostly during Beirut working hours on weekdays."""
    span = max(1.0, (end - start).total_seconds())
    u = rng.random() ** (1.0 / (1.0 + growth))
    moment = start + timedelta(seconds=span * u)
    if rng.random() < business:
        # 08:00-19:00 Beirut is 05:00-16:00 UTC.
        moment = moment.replace(hour=rng.randint(5, 15), minute=rng.randint(0, 59), second=rng.randint(0, 59))
        if moment.weekday() >= 5 and rng.random() < 0.75:
            moment -= timedelta(days=moment.weekday() - 4)
    if moment >= end:
        moment = end - timedelta(seconds=rng.randint(60, 3600))
    if moment < start:
        moment = start + timedelta(seconds=rng.randint(0, 3600))
    return moment


def weighted(rng: random.Random, pairs: Sequence[Tuple[Any, float]]) -> Any:
    total = sum(w for _, w in pairs)
    pick = rng.random() * total
    for value, w in pairs:
        pick -= w
        if pick <= 0:
            return value
    return pairs[-1][0]


class Uniq:
    """Hands out names/slugs that do not collide with existing ones."""

    def __init__(self, existing):
        self.used = {str(v) for v in existing if v is not None}

    def __call__(self, base: str, limit: int = 120) -> str:
        base = base[:limit]
        candidate = base
        n = 2
        while candidate in self.used:
            suffix = f"-{n}"
            candidate = base[: limit - len(suffix)] + suffix
            n += 1
        self.used.add(candidate)
        return candidate


def _progress(label: str, started: float) -> None:
    print(f"  {label} ({time.perf_counter() - started:.1f}s)", flush=True)


# ---------------------------------------------------------------------------
# Environment and app
# ---------------------------------------------------------------------------

def _prepare_environment(database_url: Optional[str]) -> None:
    if database_url:
        os.environ["DATABASE_URL"] = database_url
    os.environ["APP_ENV"] = "development"
    scratch = tempfile.mkdtemp(prefix="ks-seed-volume-")
    os.environ["KUBESIGHT_KUBECONFIG_DIR"] = os.path.join(scratch, "kubeconfigs")
    os.makedirs(os.environ["KUBESIGHT_KUBECONFIG_DIR"], exist_ok=True)
    empty = os.path.join(scratch, "empty-kubeconfig.yaml")
    with open(empty, "w", encoding="utf-8") as fh:
        fh.write("apiVersion: v1\nkind: Config\nclusters: []\ncontexts: []\nusers: []\n")
    os.environ["KUBECONFIG"] = empty
    os.environ.pop("K8S_KUBECONFIG", None)
    if _BACKEND not in sys.path:
        sys.path.insert(0, _BACKEND)


def _build_app():
    """The real app without its background threads, migrated and seeded."""
    from api import _resolve_database_url, create_app
    from api.runtime_config import INSECURE_DEVELOPMENT_KEY

    database_url = _resolve_database_url()

    class SeedConfig:
        TESTING = True  # keeps create_app from starting schedulers/engines/warmers
        SQLALCHEMY_DATABASE_URI = database_url
        SQLALCHEMY_TRACK_MODIFICATIONS = False
        JWT_SECRET_KEY = os.getenv("JWT_SECRET_KEY", INSECURE_DEVELOPMENT_KEY)

    app = create_app(SeedConfig)
    with app.app_context():
        from api.migrate_rbac import run_migrations
        from api.seed import seed_defaults

        run_migrations()
        seed_defaults()
    return app, database_url


# ---------------------------------------------------------------------------
# Context shared by the sections
# ---------------------------------------------------------------------------

class Ctx:
    def __init__(self, rng: random.Random, scale: float, password: str):
        self.rng = rng
        self.scale = scale
        self.password = password
        self.now = _now()
        self.start = self.now - timedelta(days=730)
        self.clusters: List[Dict[str, Any]] = []
        self.users: List[Any] = []  # stress users (ORM rows)
        self.admins: List[Any] = []
        self.engineers: List[Any] = []
        self.approver_emails: List[str] = []
        self.admin_user = None
        self.ci_services: List[Dict[str, Any]] = []
        self.app_names: List[Tuple[str, str]] = []  # (namespace, workload)

    def cluster(self, prod_bias: float = 1.0) -> Dict[str, Any]:
        pairs = [(c, (prod_bias if c["env"] == "PROD" else 1.0) * c["weight"]) for c in self.clusters]
        return weighted(self.rng, pairs)

    def user(self) -> Any:
        return weighted(self.rng, self.user_weights)

    def engineer(self) -> Any:
        return self.rng.choice(self.engineers or self.users)

    def namespace(self) -> str:
        return self.rng.choice(DOMAINS)


def _load_clusters(ctx: Ctx) -> None:
    from api.cluster_access import custom_cluster_public_id
    from api.models import Cluster

    rows = Cluster.query.filter_by(is_active=True).order_by(Cluster.id.asc()).all()
    for row in rows:
        name = row.name or f"cluster-{row.id}"
        lowered = name.lower()
        if "prod" in lowered:
            env, weight = "PROD", 3.0
        elif "uat" in lowered:
            env, weight = "UAT", 2.0
        elif "sit" in lowered or "stag" in lowered:
            env, weight = "SIT", 2.0
        elif "dev" in lowered:
            env, weight = "DEV", 2.5
        else:
            env, weight = name.upper()[:12], 0.7
        ctx.clusters.append({"id": custom_cluster_public_id(row.id), "name": name, "env": env, "weight": weight})
    if not ctx.clusters:
        print("  ! no custom clusters registered; using the mock cluster ids", flush=True)
        ctx.clusters = [
            {"id": "prod-us-east", "name": "prod-us-east", "env": "PROD", "weight": 3.0},
            {"id": "staging-eu-west", "name": "staging-eu-west", "env": "SIT", "weight": 2.0},
        ]


# ---------------------------------------------------------------------------
# Users
# ---------------------------------------------------------------------------

def seed_users(ctx: Ctx, count: int) -> int:
    from api.db import db
    from api.models import AccessRule, Role, User, UserClusterAccess
    from api.passwords import hash_password

    rng = ctx.rng
    roles = {r.name: r for r in Role.query.all()}
    plan = [("admin", 0.07), ("cluster_admin", 0.18), ("operator", 0.37), ("viewer", 0.38)]
    plan = [(name, w) for name, w in plan if name in roles] or [(next(iter(roles)), 1.0)]

    # Per-role cluster-rule templates, copied from the seeded demo users so the
    # stress users get exactly the permission keys the app grants by default.
    templates: Dict[str, List[Dict[str, Any]]] = {}
    for role_name, username in (("viewer", "viewer"), ("operator", "operator"), ("cluster_admin", "operator")):
        demo = User.query.filter_by(username=username).first()
        if not demo:
            continue
        rules = AccessRule.query.filter_by(user_id=demo.id).all()
        if not rules:
            continue
        first_cluster = rules[0].cluster_id
        templates[role_name] = [
            {"resource_type": r.resource_type, "permission_key": r.permission_key, "effect": r.effect,
             "namespace": r.namespace, "resource_name": r.resource_name}
            for r in rules if r.cluster_id == first_cluster
        ]

    password_hash = hash_password(ctx.password)  # one hash, shared: same password
    created = 0
    existing = {u.username: u for u in User.query.filter(User.username.like("stress-user-%")).all()}
    roles_cycle: List[str] = []
    for name, w in plan:
        roles_cycle += [name] * max(1, int(round(w * count)))
    rng.shuffle(roles_cycle)
    for i in range(1, count + 1):
        username = f"stress-user-{i:02d}"
        if username in existing:
            ctx.users.append(existing[username])
            continue
        role_name = roles_cycle[(i - 1) % len(roles_cycle)]
        first, last = rng.choice(FIRST_NAMES), rng.choice(LAST_NAMES)
        created_at = rand_time(rng, ctx.start, ctx.start + timedelta(days=500), growth=0.0)
        user = User(
            username=username,
            email=f"{username}@kubesight-stress.local",
            password_hash=password_hash,
            full_name=f"{first} {last}",
            role_id=roles[role_name].id,
            is_active=rng.random() > 0.04,
            created_at=created_at,
            updated_at=created_at,
            last_login_at=ctx.now - timedelta(hours=rng.randint(1, 24 * 20)),
            last_login_ip=_ip(rng),
            must_change_password=False,
            temporary_password_used=True,
            mfa_enabled=False,
            first_login_completed=True,
            token_version=0,
            failed_login_attempts=0,
            mfa_failed_attempts=0,
            lock_count_24h=0,
            requires_admin_unlock=False,
            is_service_account=False,
            interactive_login_enabled=True,
        )
        db.session.add(user)
        db.session.flush()
        if role_name != "admin":
            clusters = rng.sample(ctx.clusters, k=rng.randint(min(2, len(ctx.clusters)), len(ctx.clusters)))
            for c in clusters:
                db.session.add(UserClusterAccess(user_id=user.id, cluster_id=c["id"], can_view=True))
                for rule in templates.get(role_name, []):
                    db.session.add(AccessRule(user_id=user.id, cluster_id=c["id"], **rule))
        ctx.users.append(user)
        created += 1
    db.session.commit()

    ctx.admins = [u for u in ctx.users if u.role and u.role.name in ("admin", "cluster_admin")]
    ctx.engineers = [u for u in ctx.users if u.role and u.role.name in ("operator", "cluster_admin", "admin")]
    ctx.approver_emails = [u.email for u in ctx.admins][:8] or ["cab@kubesight-stress.local"]
    ctx.admin_user = User.query.filter_by(username="admin").first()
    # Activity is uneven: a third of the people do most of the work.
    ctx.user_weights = [(u, 6.0 if idx % 3 == 0 else 1.0) for idx, u in enumerate(ctx.users)]
    return created


# ---------------------------------------------------------------------------
# Audit trail
# ---------------------------------------------------------------------------

def seed_audit_logs(ctx: Ctx, count: int) -> int:
    from api.db import db
    from api.models import AuditLog

    rng = ctx.rng
    slugs = [s["slug"] for s in ctx.ci_services] or ["payments-api"]

    def deployment_target():
        c = ctx.cluster()
        ns = ctx.namespace()
        name = f"{ns}-{rng.choice(COMPONENTS)}"
        return c, ns, name

    def make(action: str, user) -> Dict[str, Any]:
        details: Dict[str, Any] = {"ip": _ip(rng)}
        target_type = target_id = None
        if action in ("login_success", "logout"):
            target_type, target_id = "user", str(user.id)
            details["username"] = user.username
        elif action == "login_failed":
            target_type, target_id = "user", str(user.id)
            details.update({"username": user.username, "reason": rng.choice(["invalid_password", "invalid_password", "mfa_failed"])})
        elif action in ("ci_build_triggered", "ci_build_cancelled"):
            target_type, target_id = "ci_build", str(rng.randint(1, 15000))
            details.update({"service": rng.choice(slugs), "buildNumber": rng.randint(1, 400),
                            "branch": rng.choice(["main", "develop", "release/2026.09"]),
                            "trigger": rng.choice(["manual", "webhook", "retry"]), "pipeline": "default"})
        elif action == "ci_pipeline_saved":
            target_type, target_id = "ci_pipeline", str(rng.randint(1, 120))
            details.update({"service": rng.choice(slugs), "stageCount": rng.randint(2, 7)})
        elif action in ("deployment_scaled", "deployment_restarted", "deployment_rolled_back", "deployment_applied",
                        "resource_yaml_viewed", "deployment_rollout_history_viewed"):
            c, ns, name = deployment_target()
            target_type, target_id = "deployment", f"{c['id']}/{ns}/{name}"
            details.update({"clusterId": c["id"], "namespace": ns, "name": name})
            if action == "deployment_scaled":
                details["replicas"] = rng.randint(1, 6)
        elif action == "pod_exec":
            c, ns, name = deployment_target()
            target_type, target_id = "pod", f"{c['id']}/{ns}/{name}-{_hex(rng, 10)}-{_hex(rng, 5)}"
            details.update({"clusterId": c["id"], "namespace": ns, "container": name, "command": "/bin/sh"})
        elif action == "secret_values_revealed":
            c, ns, name = deployment_target()
            target_type, target_id = "resource", f"{c['id']}/{ns}/secret/{name}-credentials"
            details.update({"clusterId": c["id"], "namespace": ns})
        elif action == "application_version_created":
            c, ns, name = deployment_target()
            target_type, target_id = "application_version", str(rng.randint(1, 6000))
            details.update({"version": f"v{rng.randint(1, 9)}.{rng.randint(0, 9)}", "app": name, "namespace": ns, "cluster": c["id"]})
        elif action in ("BUNDLE_SUBMITTED", "BUNDLE_APPROVED", "BUNDLE_REJECTED", "BUNDLE_COMPLETED", "BUNDLE_ITEM_ADDED"):
            target_type, target_id = "change_bundle", str(rng.randint(1, 1600))
            details.update({"bundleId": int(target_id)})
        elif action in ("REQUEST_CREATED", "REQUEST_APPROVED", "REQUEST_DECLINED"):
            c = ctx.cluster(prod_bias=2.0)
            target_type, target_id = "deployment_request", str(rng.randint(1, 3200))
            details.update({"clusterId": c["id"]})
        elif action in ("alert_policy_updated", "alert_policy_created"):
            target_type, target_id = "alert_policy", str(rng.randint(1, 30))
        elif action in ("user_updated", "user_role_changed", "user_created", "user_disabled"):
            other = rng.choice(ctx.users)
            target_type, target_id = "user", str(other.id)
            details.update({"username": other.username})
        elif action in ("forbidden_access_attempt", "unauthorized_deployment_attempt"):
            c, ns, name = deployment_target()
            target_type, target_id = "namespace", f"{c['id']}/{ns}"
            details.update({"clusterId": c["id"], "namespace": ns, "permission": "apps:deploy"})
        elif action in ("helm_upgrade", "helm_install_failed", "helm_rollback_attempted"):
            c, ns, name = deployment_target()
            target_type, target_id = "helm_release", f"{c['id']}/{ns}/{name}"
            details.update({"clusterId": c["id"], "namespace": ns, "release": name, "chart": f"{name}-0.{rng.randint(1, 30)}.0"})
        elif action in ("user_template_created", "user_template_updated"):
            target_type, target_id = "user_template", f"{ctx.namespace()}-{rng.choice(COMPONENTS)}"
        return {"action": action, "target_type": target_type, "target_id": target_id, "details": details}

    actions = [
        ("login_success", 24), ("logout", 6), ("login_failed", 2.5),
        ("ci_build_triggered", 16), ("ci_build_cancelled", 0.8), ("ci_pipeline_saved", 1.5),
        ("deployment_scaled", 3), ("deployment_restarted", 5), ("deployment_rolled_back", 0.8),
        ("deployment_applied", 4), ("resource_yaml_viewed", 7), ("deployment_rollout_history_viewed", 3),
        ("pod_exec", 2.5), ("secret_values_revealed", 1.2), ("application_version_created", 4),
        ("BUNDLE_ITEM_ADDED", 3), ("BUNDLE_SUBMITTED", 2), ("BUNDLE_APPROVED", 1.6), ("BUNDLE_REJECTED", 0.3),
        ("BUNDLE_COMPLETED", 1.4), ("REQUEST_CREATED", 2.5), ("REQUEST_APPROVED", 2), ("REQUEST_DECLINED", 0.5),
        ("alert_policy_updated", 0.4), ("alert_policy_created", 0.1), ("user_updated", 0.3),
        ("user_role_changed", 0.1), ("user_created", 0.1), ("user_disabled", 0.05),
        ("forbidden_access_attempt", 1.0), ("unauthorized_deployment_attempt", 0.4),
        ("helm_upgrade", 1.0), ("helm_install_failed", 0.2), ("helm_rollback_attempted", 0.15),
        ("user_template_created", 0.2), ("user_template_updated", 0.4),
    ]
    admin_only = {"user_updated", "user_role_changed", "user_created", "user_disabled", "alert_policy_created",
                  "alert_policy_updated", "BUNDLE_APPROVED", "BUNDLE_REJECTED", "REQUEST_APPROVED", "REQUEST_DECLINED"}
    batch: List[Dict[str, Any]] = []
    written = 0
    for _ in range(count):
        action = weighted(rng, actions)
        user = rng.choice(ctx.admins) if (action in admin_only and ctx.admins) else ctx.user()
        row = make(action, user)
        system = action in ("BUNDLE_COMPLETED",) and rng.random() < 0.6
        row["actor_user_id"] = None if system else user.id
        row["created_at"] = rand_time(rng, ctx.start, ctx.now, growth=1.2)
        batch.append(row)
        if len(batch) >= 5000:
            db.session.bulk_insert_mappings(AuditLog, batch)
            db.session.commit()
            written += len(batch)
            batch = []
    if batch:
        db.session.bulk_insert_mappings(AuditLog, batch)
        db.session.commit()
        written += len(batch)
    return written


# ---------------------------------------------------------------------------
# Alerts
# ---------------------------------------------------------------------------

def seed_alerts(ctx: Ctx, policy_count: int, history_count: int) -> Tuple[int, int]:
    from api.alert_policy_catalog import (
        METRIC_BY_KEY,
        SERVICE_ALERT_CLUSTER_ID,
        normalize_log_config,
        normalize_scope,
    )
    from api.db import db
    from api.models import AlertHistory, AlertPolicy
    from api.services.alert_policy_service import _dashboard_channels_from_payload, _validate_payload

    rng = ctx.rng
    metric_templates = [
        ("High CPU", [{"metricKey": "cpu_usage_percent", "operator": ">", "threshold": 85}], "warning"),
        ("Memory pressure", [{"metricKey": "memory_usage_percent", "operator": ">=", "threshold": 90}], "critical"),
        ("Restart storm", [{"metricKey": "pod_restart_count", "operator": ">", "threshold": 5}], "warning"),
        ("CrashLoopBackOff", [{"metricKey": "pod_crashloop_backoff", "operator": "=", "threshold": True}], "critical"),
        ("Pods stuck pending", [{"metricKey": "pod_pending", "operator": "=", "threshold": True}], "warning"),
        ("Unavailable replicas", [{"metricKey": "deployment_unavailable_replicas", "operator": ">=", "threshold": 1}], "critical"),
        ("PVC almost full", [{"metricKey": "pvc_usage_percent", "operator": ">", "threshold": 80}], "warning"),
        ("CPU+memory saturation", [{"metricKey": "cpu_usage_percent", "operator": ">", "threshold": 90},
                                   {"metricKey": "memory_usage_percent", "operator": ">", "threshold": 90}], "critical"),
    ]
    log_patterns = [
        ("OutOfMemoryError", "contains", "critical"), ("Connection refused", "contains", "warning"),
        ("HSM timeout", "contains", "critical"), (r"ERROR .*SQLException", "regex", "warning"),
        ("Kafka producer failed", "contains", "warning"), ("ISO8583 decline 96", "contains", "info"),
    ]
    uniq = Uniq(r[0] for r in db.session.query(AlertPolicy.name).all())
    policies: List[AlertPolicy] = []
    creator = ctx.admin_user
    n_metric = max(1, int(policy_count * 0.6))
    n_log = max(1, int(policy_count * 0.3))
    n_service = max(0, policy_count - n_metric - n_log)
    for i in range(policy_count):
        if i < n_metric:
            title, conditions, severity = metric_templates[i % len(metric_templates)]
            c = ctx.cluster(prod_bias=2.0)
            ns = ctx.namespace()
            payload = {
                "name": uniq(f"{title} - {ns} ({c['name']})"), "clusterId": c["id"], "alertType": "metric",
                "severity": severity, "conditionLogic": "all" if len(conditions) > 1 else "any",
                "conditions": conditions, "scope": {"type": rng.choice(["deployment", "pod"]), "namespace": ns, "resourceName": "*"},
            }
        elif i < n_metric + n_log:
            pattern, match_type, severity = log_patterns[(i - n_metric) % len(log_patterns)]
            c = ctx.cluster(prod_bias=2.0)
            ns = ctx.namespace()
            payload = {
                "name": uniq(f"Log: {pattern[:40]} - {ns}"), "clusterId": c["id"], "alertType": "log", "severity": severity,
                "logConfig": {"matchType": match_type, "pattern": pattern, "logWindowSeconds": 300},
                "scope": {"type": "deployment", "namespace": ns, "resourceName": "*"},
            }
        else:
            payload = {"name": uniq(f"Service health {i - n_metric - n_log + 1}"), "alertType": "service", "severity": "critical",
                       "serviceConfig": {"serviceId": "*", "triggerOn": rng.choice(["critical", "degraded"])}}
        error = _validate_payload(payload)
        if error:
            raise RuntimeError(f"alert policy payload rejected: {error} ({payload['name']})")
        alert_type = payload["alertType"]
        created_at = rand_time(rng, ctx.start, ctx.now - timedelta(days=30), growth=0.5)
        policy = AlertPolicy(
            name=payload["name"],
            cluster_id=SERVICE_ALERT_CLUSTER_ID if alert_type == "service" else payload["clusterId"],
            description=f"Stress fixture: {payload['name']}",
            enabled=rng.random() > 0.15,
            alert_type=alert_type,
            severity=payload["severity"],
            condition_logic=payload.get("conditionLogic", "any"),
            conditions=payload.get("conditions") or [],
            scope=normalize_scope(payload.get("scope")),
            notification_channels=_dashboard_channels_from_payload(payload),
            evaluation_interval_seconds=rng.choice([300, 300, 600, 900]),
            created_by_user_id=creator.id if creator else None,
            created_at=created_at,
            updated_at=created_at,
        )
        if alert_type == "log":
            policy.log_config = normalize_log_config(payload["logConfig"])
        if alert_type == "service":
            policy.service_config = payload["serviceConfig"]
        db.session.add(policy)
        policies.append(policy)
    db.session.flush()
    db.session.commit()

    # --- history ---------------------------------------------------------
    used_keys = {r[0] for r in db.session.query(AlertHistory.alert_key).all()}
    active_budget = max(20, int(history_count * 0.008))
    rows: List[Dict[str, Any]] = []
    written = 0
    metric_policies = [p for p in policies if p.alert_type == "metric"]
    log_policies = [p for p in policies if p.alert_type == "log"]
    service_policies = [p for p in policies if p.alert_type == "service"]
    for n in range(history_count):
        kind = weighted(rng, [("metric", 70), ("log", 25), ("service", 5 if service_policies else 0)])
        pool = {"metric": metric_policies, "log": log_policies, "service": service_policies}[kind] or metric_policies
        policy = rng.choice(pool)
        kind = policy.alert_type
        fired = rand_time(rng, ctx.start, ctx.now, growth=1.0, business=0.35)
        recent = (ctx.now - fired) < timedelta(days=2)
        active = recent and active_budget > 0 and kind != "service" and rng.random() < 0.5
        if active:
            active_budget -= 1
        resolved_at = None if active else fired + timedelta(seconds=rng.randint(120, 6 * 3600))
        if resolved_at and resolved_at > ctx.now:
            resolved_at = ctx.now
        if kind == "service":
            c = ctx.cluster()
        else:
            c = next((x for x in ctx.clusters if x["id"] == policy.cluster_id), ctx.clusters[0])
        ns = (policy.scope or {}).get("namespace") or ctx.namespace()
        workload = f"{ns}-{rng.choice(COMPONENTS)}"
        pod = f"{workload}-{_hex(rng, 9)}-{_hex(rng, 5)}"
        row: Dict[str, Any] = {
            "policy_id": policy.id, "policy_name": policy.name, "cluster_id": c["id"], "namespace": ns,
            "alert_type": kind, "severity": policy.severity, "status": "active" if active else "resolved",
            "fired_at": fired, "last_notified_at": fired + timedelta(seconds=rng.randint(1, 30)),
        }
        if resolved_at:
            row["resolved_at"] = resolved_at
        if kind == "metric":
            scope_type = (policy.scope or {}).get("type") or "deployment"
            name = pod if scope_type == "pod" else workload
            details = []
            observations = []
            for cond in policy.conditions or []:
                metric = METRIC_BY_KEY[cond["metricKey"]]
                if metric["type"] == "boolean":
                    observed = True
                elif metric["type"] == "percent":
                    observed = round(float(cond["threshold"]) + rng.uniform(1, 12), 1)
                else:
                    observed = int(cond["threshold"]) + rng.randint(1, 9)
                details.append({"metricKey": cond["metricKey"], "metricLabel": metric["label"], "operator": cond["operator"],
                                "threshold": cond["threshold"], "observedValue": observed, "matched": True})
                observations.append({"metricKey": cond["metricKey"], "value": observed, "resourceType": scope_type,
                                     "namespace": ns, "resourceName": name})
            key = ":".join([f"policy-{policy.id}", c["id"], ns, scope_type, name])
            row.update({
                "resource_type": scope_type, "resource_name": name, "title": f"{policy.name} triggered",
                "description": "; ".join(f"{d['metricLabel']} {d['operator']} {d['threshold']} (observed {d['observedValue']})" for d in details),
                "triggered_conditions": details, "metric_snapshot": {"observations": observations},
            })
        elif kind == "log":
            cfg = policy.log_config or {}
            container = workload
            stamp = fired.strftime("%Y-%m-%dT%H:%M:%S.") + f"{rng.randint(0, 999):03d}Z"
            line = f"{stamp} ERROR [{workload}] {cfg.get('pattern', 'error')} while processing txn {rng.randint(10**8, 10**9)}"
            lines = [f"{stamp} INFO  [{workload}] request id={_hex(rng, 12)}" for _ in range(3)] + [line]
            log_hash = hashlib.sha256(line.encode()).hexdigest()[:32]
            key = ":".join([f"policy-{policy.id}", c["id"], ns, pod, container, log_hash])
            row.update({
                "resource_type": "deployment", "resource_name": workload, "title": "Error detected in logs",
                "description": f"Pattern '{cfg.get('pattern')}' matched in {pod}/{container}",
                "triggered_conditions": [],
                "log_snapshot": {"podName": pod, "containerName": container, "deploymentName": workload,
                                 "matchedPattern": cfg.get("pattern"), "logTimestamp": stamp, "detectedAt": stamp,
                                 "matchingLine": line, "logLines": lines, "logSnippet": "\n".join(lines),
                                 "lastMatchAt": fired.isoformat()},
            })
        else:
            service_id = rng.randint(1, 40)
            key = ":".join([f"policy-{policy.id}", SERVICE_ALERT_CLUSTER_ID, f"svc-{service_id}", "workload", c["id"], ns, workload])
            row.update({
                "resource_type": "deployment", "resource_name": workload,
                "title": f"{workload} is down", "description": f"0/{rng.randint(1, 3)} replicas ready",
                "triggered_conditions": [{"state": "critical", "reason": "no ready replicas"}],
                "metric_snapshot": {"serviceId": service_id, "serviceName": f"{ns.title()} Platform", "affectedClients": []},
            })
        # Historical (resolved) rows keep a unique suffix: the live evaluator
        # matches by exact key, and only the open alerts should be "its" rows.
        if not active or key in used_keys:
            key = f"{key}#h{n}"
        used_keys.add(key)
        row["alert_key"] = key[:512]
        rows.append(row)
        if len(rows) >= 4000:
            db.session.bulk_insert_mappings(AlertHistory, rows)
            db.session.commit()
            written += len(rows)
            rows = []
    if rows:
        db.session.bulk_insert_mappings(AlertHistory, rows)
        db.session.commit()
        written += len(rows)
    return len(policies), written


# ---------------------------------------------------------------------------
# CI
# ---------------------------------------------------------------------------

_STAGE_SECONDS = {
    "checkout": (4, 25), "Build": (70, 420), "Install": (30, 200), "Unit Tests": (40, 900),
    "Package": (3, 20), "Build Image": (45, 360), "Lint": (10, 90), "Dependency Scan": (60, 420),
    "Dependencies": (60, 400), "Assemble APK": (240, 900), "Assemble AAB": (240, 900),
    "Archive": (300, 1400), "Export IPA": (40, 200), "Analyze": (20, 120), "Build APK": (200, 800),
    "Integration Tests": (120, 1200), "Smoke Test": (20, 120),
}


def _stage_seconds(rng: random.Random, definition: Dict[str, Any]) -> int:
    if definition.get("stageType") == "checkout":
        lo, hi = _STAGE_SECONDS["checkout"]
    else:
        lo, hi = _STAGE_SECONDS.get(definition.get("name") or "", (15, 300))
    return rng.randint(lo, hi)


def _log_lines(rng: random.Random, definition: Dict[str, Any], status: str, slug: str) -> List[Tuple[str, str]]:
    name = definition.get("name") or "stage"
    kind = definition.get("stageType")
    out: List[Tuple[str, str]] = []
    if kind == "checkout":
        out += [("stdout", f"Cloning into '/workspace/{slug}'..."), ("stdout", f"HEAD is now at {_hex(rng, 7)} {rng.choice(COMMIT_MESSAGES)}")]
    elif kind == "container_image":
        out += [("stdout", f"#{i} [{i}/9] RUN step {i}") for i in range(1, 8)]
        out += [("stdout", f"#9 exporting to image"), ("stdout", f"#9 pushing nexus.areeba.local:8082/{slug} done")]
    else:
        for command in definition.get("commands") or [name]:
            out.append(("stdout", f"$ {str(command)[:200]}"))
        for i in range(rng.randint(12, 40)):
            out.append(("stdout", f"[INFO] {name}: step {i + 1} ok ({rng.randint(1, 900)} ms)"))
    if status == "failed":
        out.append(("stderr", f"[ERROR] {name} failed: {rng.choice(['Tests run: 412, Failures: 3', 'Could not resolve dependencies', 'exit status 1', 'npm ERR! code ELIFECYCLE'])}"))
    return out


def _test_summary(rng: random.Random, passed_stage: bool) -> Dict[str, Any]:
    tests = rng.randint(120, 2600)
    failed = 0 if passed_stage else rng.randint(1, 12)
    skipped = rng.randint(0, 25)
    lines_pct = round(rng.uniform(48, 88), 1)
    return {
        "reportCount": 2, "testReportCount": 1, "coverageReportCount": 1,
        "totals": {"tests": tests, "passed": tests - failed - skipped, "failed": failed, "errors": 0,
                   "skipped": skipped, "flaky": 0, "durationSeconds": round(rng.uniform(20, 600), 1)},
        "coverage": {"lines": {"pct": lines_pct}, "branches": {"pct": round(lines_pct - rng.uniform(5, 15), 1)}},
        "failures": [], "failureCount": failed, "reports": [], "errors": [], "errorCount": 0,
    }


def seed_ci(ctx: Ctx, service_count: int, builds_per_service: float, shared_count: int) -> Dict[str, int]:
    from api.db import db
    from api.models_application_intelligence import BitbucketCredentialProfile
    from api.models_ci import (
        CiArtifact,
        CiBuild,
        CiBuildStage,
        CiLogChunk,
        CiPipeline,
        CiRunner,
        CiService,
        CiServiceDeployment,
    )
    from api.secret_encryption import encrypt_secret
    from api.services.ci import pipelines as ci_pipelines
    from api.services.ci import templates as ci_templates
    from api.services.ci.serializers import stage_definition

    rng = ctx.rng
    counts = {"ci_services": 0, "ci_pipelines": 0, "ci_builds": 0, "ci_build_stages": 0,
              "ci_log_chunks": 0, "ci_artifacts": 0, "ci_service_deployments": 0}

    # One read-only Bitbucket credential, so services look "source configured".
    credential = BitbucketCredentialProfile.query.filter_by(name="bitbucket-ci-readonly").first()
    if credential is None:
        credential = BitbucketCredentialProfile(
            name="bitbucket-ci-readonly", provider="bitbucket", credential_type="repository_access_token",
            principal="x-token-auth", secret_cipher=encrypt_secret("stress-fixture-not-a-real-token"),
            read_only=True, enabled=True, created_by_user_id=ctx.admin_user.id if ctx.admin_user else None,
        )
        db.session.add(credential)
        db.session.flush()
    runner = CiRunner.query.filter_by(runner_type="kubernetes").first() or CiRunner.query.first()
    runner_id = runner.id if runner else None

    slug_uniq = Uniq(r[0] for r in db.session.query(CiService.slug).all())
    link_uniq = Uniq(f"{a}|{b}|{c}" for a, b, c in db.session.query(
        CiServiceDeployment.cluster_id, CiServiceDeployment.namespace, CiServiceDeployment.workload_name).all())

    # --- shared pipelines (Pipelines page) -------------------------------
    shared_defs = [
        ("Java service (Maven)", "java_maven"), ("Node service", "node"), ("Container only", "container"),
        ("Release from tag", "java_gradle"), ("Python job", "python"), ("Nightly security scan", "generic"),
    ]
    homes: List[Dict[str, Any]] = []
    for name, app_type in shared_defs[:shared_count]:
        created_at = rand_time(rng, ctx.start, ctx.start + timedelta(days=300), growth=0.0)
        home = CiService(
            kind="pipeline", name=name, slug=slug_uniq(name.lower().replace(" ", "-").replace("(", "").replace(")", "")),
            description=f"Shared pipeline: {name}", owner_team="Platform", application_type=app_type,
            status="active", default_branch="main", created_by_user_id=ctx.admin_user.id if ctx.admin_user else None,
            created_at=created_at, updated_at=created_at,
        )
        db.session.add(home)
        db.session.flush()
        payload = ci_templates.default_pipeline_payload(app_type)
        pipe = CiPipeline(service_id=home.id, name="default", description=payload["description"], purpose="build",
                          is_default=True, enabled=True, version=rng.randint(2, 9),
                          parameters=ci_pipelines._parameters(payload["parameters"]),
                          created_at=created_at, updated_at=created_at)
        db.session.add(pipe)
        db.session.flush()
        ci_pipelines._apply_stages(pipe, payload["stages"], actor=None)
        db.session.flush()
        homes.append({"service": home, "pipeline": pipe, "app_type": app_type, "created_at": created_at})
        counts["ci_services"] += 1
        counts["ci_pipelines"] += 1

    # --- services --------------------------------------------------------
    specs: List[Tuple[str, str, str]] = []  # (domain, name, app_type)
    seen = set()
    while len(specs) < service_count:
        domain = rng.choice(DOMAINS)
        if rng.random() < 0.06:
            suffix, app_type = rng.choice(MOBILE_KINDS)
        else:
            suffix, app_type = rng.choice(CI_KINDS)
        name = f"{domain}-{suffix}"
        if name in seen:
            name = f"{domain}-{rng.choice(['card', 'txn', 'merchant', 'core', 'ext'])}-{suffix}"
            if name in seen:
                continue
        seen.add(name)
        specs.append((domain, name, app_type))

    extras = {
        "java_maven": [{"name": "Dependency Scan", "stageType": "command", "image": "", "runnerLabels": ["linux"],
                        "commands": ["./mvnw -B org.owasp:dependency-check-maven:check"], "timeoutSeconds": 1800,
                        "continueOnFailure": True}],
        "node": [{"name": "Lint", "stageType": "command", "image": "", "runnerLabels": ["linux", "node"],
                  "commands": ["npm run lint --if-present"], "timeoutSeconds": 600}],
        "container": [{"name": "Smoke Test", "stageType": "command", "image": "", "runnerLabels": ["linux"],
                       "commands": ["./scripts/smoke.sh"], "timeoutSeconds": 600}],
        "generic": [{"name": "Integration Tests", "stageType": "command", "image": "", "runnerLabels": ["linux"],
                     "commands": ["./run-it.sh"], "timeoutSeconds": 2400}],
    }

    build_total_target = int(service_count * builds_per_service)
    weights = [rng.lognormvariate(0, 0.8) for _ in specs]
    wsum = sum(weights) or 1.0
    per_service_builds = [max(5, int(build_total_target * w / wsum)) for w in weights]

    for index, (domain, name, app_type) in enumerate(specs):
        svc_started = time.perf_counter()
        created_at = rand_time(rng, ctx.start, ctx.now - timedelta(days=30), growth=0.3, business=1.0)
        default_branch = rng.choice(["main", "main", "master", "develop"])
        repo = name
        creator = ctx.engineer()
        svc = CiService(
            kind="service", name=name, slug=slug_uniq(name, 180), description=f"{TEAMS.get(domain, 'Platform')} — {name}",
            owner_team=TEAMS.get(domain, "Platform"), criticality=rng.choice(["low", "medium", "high", "high", "critical"]),
            application_type=app_type, status=weighted(rng, [("active", 92), ("paused", 5), ("archived", 3)]),
            repository_provider="bitbucket", repository_url=f"https://bitbucket.org/areeba/{repo}.git",
            repository_workspace="areeba", repository_name=repo, default_branch=default_branch,
            credential_profile_id=credential.id, max_concurrent_builds=rng.choice([1, 1, 2]),
            created_by_user_id=creator.id, created_at=created_at, updated_at=created_at,
        )
        db.session.add(svc)
        db.session.flush()
        payload = ci_templates.default_pipeline_payload(app_type)
        stages_payload = payload["stages"]
        if app_type in extras and rng.random() < 0.6:
            # Before the image stage, as people actually add them.
            insert_at = max(1, len(stages_payload) - 1)
            stages_payload = stages_payload[:insert_at] + extras[app_type] + stages_payload[insert_at:]
        pipe = CiPipeline(service_id=svc.id, name="default", description=payload["description"], purpose="build",
                          is_default=True, enabled=True, version=rng.randint(1, 14),
                          parameters=ci_pipelines._parameters(payload["parameters"]),
                          created_by_user_id=creator.id, created_at=created_at,
                          updated_at=rand_time(rng, created_at, ctx.now, growth=0.0))
        db.session.add(pipe)
        db.session.flush()
        ci_pipelines._apply_stages(pipe, stages_payload, actor=None)
        db.session.flush()
        counts["ci_services"] += 1
        counts["ci_pipelines"] += 1

        # A few services build with a shared pipeline.
        home = None
        if homes and rng.random() < 0.12:
            matching = [h for h in homes if h["app_type"] == app_type] or homes
            home = rng.choice(matching)
            pipe.linked_pipeline_id = home["pipeline"].id
        run_pipeline = home["pipeline"] if home else pipe
        definitions = [stage_definition(s) for s in run_pipeline.stages]
        snapshot_base = {
            "pipelineId": pipe.id, "pipelineName": pipe.name, "pipelineVersion": run_pipeline.version,
            "pipelineSource": "shared" if home else "configured",
            "parameters": ci_pipelines.parameter_definitions(run_pipeline), "refType": "branch",
            "stages": definitions, "postActions": [],
        }
        if home:
            snapshot_base["sharedPipeline"] = {"id": home["service"].id, "slug": home["service"].slug,
                                               "name": home["service"].name, "pipelineId": home["pipeline"].id,
                                               "version": home["pipeline"].version, "homeServiceId": home["service"].id}
        param_names = {p.get("name") for p in snapshot_base["parameters"]}

        # Inventory links (Built by).
        if rng.random() < 0.7:
            for c in rng.sample(ctx.clusters, k=rng.randint(1, min(3, len(ctx.clusters)))):
                key = link_uniq(f"{c['id']}|{domain}|{svc.slug}")
                if key != f"{c['id']}|{domain}|{svc.slug}":
                    continue
                db.session.add(CiServiceDeployment(
                    service_id=svc.id, cluster_id=c["id"], namespace=domain, workload_kind="Deployment",
                    workload_name=svc.slug, environment=c["env"], source=rng.choice(["manual", "inventory", "deploy_stage"]),
                    created_by_user_id=creator.id, created_at=created_at, updated_at=created_at,
                ))
                counts["ci_service_deployments"] += 1
        ctx.ci_services.append({"slug": svc.slug, "id": svc.id, "domain": domain})
        ctx.app_names.append((domain, svc.slug))

        n_builds = per_service_builds[index]
        times = sorted(rand_time(rng, created_at, ctx.now - timedelta(minutes=30), growth=0.6) for _ in range(n_builds))
        builds: List[Dict[str, Any]] = []
        plans: List[Dict[str, Any]] = []
        for number, queued_at in enumerate(times, start=1):
            trigger = weighted(rng, [("manual", 42), ("webhook", 32), ("automation", 10), ("schedule", 8),
                                     ("retry", 6), ("api", 2)])
            if trigger == "retry" and not (plans and plans[-1]["status"] in ("failed", "timeout", "cancelled")):
                trigger = "manual"
            status = weighted(rng, [("success", 82), ("failed", 12), ("cancelled", 4), ("timeout", 1.5)])
            variables: Dict[str, str] = {}
            if "SKIP_TESTS" in param_names:
                variables["SKIP_TESTS"] = "true" if rng.random() < 0.05 else "false"
            for flag in ("BUILD_APK", "BUILD_AAB"):
                if flag in param_names:
                    variables[flag] = "true" if rng.random() < 0.85 else "false"
            is_tag = rng.random() < 0.07
            if is_tag:
                branch = f"v{rng.randint(1, 4)}.{rng.randint(0, 30)}.{rng.randint(0, 9)}"
            else:
                branch = weighted(rng, [(default_branch, 60), ("develop", 15), ("release/2026.%02d" % rng.randint(1, 12), 5),
                                        (f"feature/{domain.upper()[:4]}-{rng.randint(100, 4999)}-{rng.choice(BRANCH_FEATURE_WORDS)}", 20)])
            snapshot = dict(snapshot_base, variables=variables, refType="tag" if is_tag else "branch")
            # Stage outcomes.
            stage_rows = []
            started_at = queued_at + timedelta(seconds=rng.randint(1, 40))
            moment = started_at
            failing = None
            if status in ("failed", "cancelled", "timeout"):
                candidates = [i for i, d in enumerate(definitions) if d.get("stageType") != "checkout"] or [0]
                pref = [i for i in candidates if definitions[i].get("name") in ("Unit Tests", "Build", "Build Image")]
                failing = rng.choice(pref if pref and rng.random() < 0.8 else candidates)
            for pos, definition in enumerate(definitions):
                cond = definition.get("runCondition") or {}
                skipped_by_condition = bool(cond) and variables.get(cond.get("variable"), "") != str(cond.get("value"))
                if failing is not None and pos > failing:
                    stage_rows.append({"position": pos, "status": "skipped", "def": definition, "error": "An earlier stage failed." if status == "failed" else None})
                    continue
                if skipped_by_condition and pos != failing:
                    stage_rows.append({"position": pos, "status": "skipped", "def": definition, "start": moment, "end": moment, "seconds": 0})
                    continue
                seconds = _stage_seconds(rng, definition)
                stage_status = "success"
                exit_code = 0
                error = None
                if pos == failing:
                    if status == "failed":
                        stage_status, exit_code = "failed", rng.choice([1, 1, 1, 2, 127, 137])
                        error = f"Command exited with code {exit_code}."
                    elif status == "cancelled":
                        stage_status, exit_code = "cancelled", None
                        seconds = max(1, seconds // 3)
                        error = "Cancelled by request."
                    else:
                        stage_status, exit_code = "timeout", None
                        seconds = int(definition.get("timeoutSeconds") or 1800)
                        error = "A stage exceeded its timeout."
                end = moment + timedelta(seconds=seconds)
                stage_rows.append({"position": pos, "status": stage_status, "def": definition, "start": moment,
                                   "end": end, "seconds": seconds, "exit": exit_code, "error": error})
                moment = end + timedelta(seconds=rng.randint(1, 6))
            finished_at = moment
            if finished_at > ctx.now:
                finished_at = ctx.now - timedelta(seconds=5)
            requester = None
            if trigger in ("manual", "retry", "api"):
                requester = ctx.engineer().id
            build_error = None
            if status == "failed":
                build_error = f"Stage '{definitions[failing]['name']}' failed."
            elif status == "cancelled":
                build_error = "Cancelled by request."
            elif status == "timeout":
                build_error = "A stage exceeded its timeout."
            build = {
                "service_id": svc.id, "pipeline_id": pipe.id, "number": number, "status": status, "trigger_type": trigger,
                "branch": branch, "commit_sha": _sha(rng), "commit_message": rng.choice(COMMIT_MESSAGES),
                "pipeline_snapshot": snapshot, "runner_id": runner_id, "workspace_ref": f"ks-build-{svc.id}-{number}",
                "cancel_requested": status == "cancelled", "queued_at": queued_at, "started_at": started_at,
                "finished_at": finished_at, "duration_seconds": int((finished_at - started_at).total_seconds()),
                "created_at": queued_at, "worker_callback_token_hash": _sha256(rng),
            }
            if requester is not None:
                build["requested_by_user_id"] = requester
            if status == "cancelled" and requester is not None:
                build["cancel_requested_by_user_id"] = requester
            if build_error:
                build["error"] = build_error
            tests_def = next((i for i, d in enumerate(definitions) if d.get("name") == "Unit Tests" and d.get("artifacts")), None)
            if tests_def is not None:
                tstatus = next((s["status"] for s in stage_rows if s["position"] == tests_def), None)
                if tstatus in ("success", "failed"):
                    build["test_summary"] = _test_summary(rng, tstatus == "success")
            builds.append(build)
            plans.append({"status": status, "stages": stage_rows, "trigger": trigger, "branch": branch, "number": number,
                          "finished_at": finished_at})

        db.session.bulk_insert_mappings(CiBuild, builds, return_defaults=True)
        retry_updates = []
        for i, build in enumerate(builds):
            if build["trigger_type"] == "retry" and i > 0:
                retry_updates.append({"id": build["id"], "retry_of_build_id": builds[i - 1]["id"]})
        if retry_updates:
            db.session.bulk_update_mappings(CiBuild, retry_updates)

        recent_cut = max(0, len(builds) - 2)  # logs for the two newest builds only
        artifact_cut = max(0, len(builds) - 5)
        old_stages: List[Dict[str, Any]] = []
        new_stages: List[Dict[str, Any]] = []
        for i, (build, plan) in enumerate(zip(builds, plans)):
            for s in plan["stages"]:
                definition = s["def"]
                row = {
                    "build_id": build["id"], "pipeline_stage_id": definition.get("pipelineStageId"),
                    "position": s["position"], "name": definition.get("name") or f"Stage {s['position'] + 1}",
                    "stage_type": definition.get("stageType") or "command", "status": s["status"], "attempt": 1,
                    "log_line_count": 0, "log_truncated": False, "created_at": build["queued_at"],
                }
                if s.get("start") is not None and s["status"] != "skipped" or s.get("seconds") == 0:
                    row["started_at"] = s.get("start")
                    row["finished_at"] = s.get("end")
                    row["duration_seconds"] = s.get("seconds")
                if s["status"] not in ("skipped",):
                    row["runner_id"] = runner_id
                    row["external_ref"] = f"stage-{s['position']}"
                if s.get("exit") is not None:
                    row["exit_code"] = s["exit"]
                if s.get("error"):
                    row["error"] = s["error"]
                if i >= recent_cut or i >= artifact_cut:
                    row["_plan"] = (i, s)
                    new_stages.append(row)
                else:
                    old_stages.append(row)
        if old_stages:
            db.session.bulk_insert_mappings(CiBuildStage, old_stages)
        meta = [r.pop("_plan") for r in new_stages]
        if new_stages:
            db.session.bulk_insert_mappings(CiBuildStage, new_stages, return_defaults=True)
        counts["ci_build_stages"] += len(old_stages) + len(new_stages)

        chunks: List[Dict[str, Any]] = []
        artifacts: List[Dict[str, Any]] = []
        line_counts: List[Dict[str, Any]] = []
        for row, (i, s) in zip(new_stages, meta):
            build = builds[i]
            definition = s["def"]
            if i >= recent_cut and s["status"] != "skipped":
                lines = _log_lines(rng, definition, s["status"], svc.slug)
                base = s.get("start") or build["queued_at"]
                for seq, (stream, content) in enumerate(lines, start=1):
                    chunks.append({"build_stage_id": row["id"], "seq": seq, "stream": stream, "content": content,
                                   "created_at": base + timedelta(milliseconds=seq * 50)})
                line_counts.append({"id": row["id"], "log_line_count": len(lines)})
            if i >= artifact_cut and build["status"] == "success" and s["status"] == "success":
                common = {"service_id": svc.id, "build_id": build["id"], "build_stage_id": row["id"],
                          "commit_sha": build["commit_sha"], "branch": build["branch"], "created_at": s.get("end") or build["finished_at"]}
                tag = build["branch"] if build["pipeline_snapshot"].get("refType") == "tag" else f"{build['branch'].replace('/', '-')}-{build['number']}"
                if definition.get("stageType") == "container_image":
                    artifacts.append(dict(common, artifact_type="container-image", name=svc.slug, version=tag,
                                          uri=f"nexus.areeba.local:8082/{domain}/{svc.slug}:{tag}",
                                          digest=f"sha256:{_sha256(rng)}", size_bytes=rng.randint(80, 600) * 1024 * 1024,
                                          storage_backend="registry", artifact_metadata={"pushed": True}))
                for spec in definition.get("artifacts") or []:
                    kind = spec.get("type") or "binary"
                    artifacts.append(dict(common, artifact_type=kind, name=os.path.basename(str(spec.get("path") or "artifact")).replace("*", svc.slug),
                                          version=tag, checksum_sha256=_sha256(rng), size_bytes=rng.randint(20, 90000) * 1024,
                                          storage_backend="local", artifact_metadata={"sourcePath": spec.get("path")}))
        if line_counts:
            db.session.bulk_update_mappings(CiBuildStage, line_counts)
        if chunks:
            db.session.bulk_insert_mappings(CiLogChunk, chunks)
        if artifacts:
            db.session.bulk_insert_mappings(CiArtifact, artifacts)
        svc.next_build_number = len(builds) + 1
        last = plans[-1] if plans else None
        svc.updated_at = last["finished_at"] if last else created_at
        db.session.commit()
        counts["ci_builds"] += len(builds)
        counts["ci_log_chunks"] += len(chunks)
        counts["ci_artifacts"] += len(artifacts)
        if (index + 1) % 10 == 0 or index + 1 == len(specs):
            print(f"    CI {index + 1}/{len(specs)} services, {counts['ci_builds']} builds "
                  f"({time.perf_counter() - svc_started:.2f}s last)", flush=True)

    # Standalone runs of the shared pipelines themselves.
    for home in homes:
        svc, pipe = home["service"], home["pipeline"]
        definitions = [stage_definition(s) for s in pipe.stages]
        rows = []
        number = 0
        for queued_at in sorted(rand_time(rng, home["created_at"], ctx.now - timedelta(hours=2)) for _ in range(rng.randint(10, 40))):
            number += 1
            status = weighted(rng, [("success", 85), ("failed", 15)])
            seconds = sum(_stage_seconds(rng, d) for d in definitions)
            started = queued_at + timedelta(seconds=5)
            finished = started + timedelta(seconds=seconds)
            rows.append({
                "service_id": svc.id, "pipeline_id": pipe.id, "number": number, "status": status, "trigger_type": "manual",
                "branch": "main", "commit_sha": _sha(rng), "commit_message": rng.choice(COMMIT_MESSAGES),
                "pipeline_snapshot": {"pipelineId": pipe.id, "pipelineName": pipe.name, "pipelineVersion": pipe.version,
                                      "pipelineSource": "configured", "variables": {}, "refType": "branch",
                                      "parameters": ci_pipelines.parameter_definitions(pipe), "stages": definitions,
                                      "postActions": []},
                "runner_id": runner_id, "queued_at": queued_at, "started_at": started, "finished_at": finished,
                "duration_seconds": seconds, "created_at": queued_at, "requested_by_user_id": ctx.engineer().id,
                **({"error": f"Stage '{definitions[-1]['name']}' failed."} if status == "failed" else {}),
            })
        db.session.bulk_insert_mappings(CiBuild, rows, return_defaults=True)
        stage_rows = []
        for b in rows:
            moment = b["started_at"]
            for pos, d in enumerate(definitions):
                failed_here = b["status"] == "failed" and pos == len(definitions) - 1
                secs = _stage_seconds(rng, d)
                stage_rows.append({"build_id": b["id"], "pipeline_stage_id": d.get("pipelineStageId"), "position": pos,
                                   "name": d.get("name"), "stage_type": d.get("stageType") or "command",
                                   "status": "failed" if failed_here else "success", "attempt": 1,
                                   "exit_code": 1 if failed_here else 0, "started_at": moment,
                                   "finished_at": moment + timedelta(seconds=secs), "duration_seconds": secs,
                                   "log_line_count": 0, "log_truncated": False, "runner_id": runner_id,
                                   "created_at": b["queued_at"]})
                moment += timedelta(seconds=secs + 2)
        db.session.bulk_insert_mappings(CiBuildStage, stage_rows)
        svc.next_build_number = number + 1
        counts["ci_builds"] += len(rows)
        counts["ci_build_stages"] += len(stage_rows)
    db.session.commit()
    return counts


# ---------------------------------------------------------------------------
# Deployment requests and change bundles
# ---------------------------------------------------------------------------

REQUEST_MESSAGES = [
    "Deploy {app} {ver} to {ns} — fixes the settlement cutoff issue (ticket #{t}).",
    "Please approve the rollout of {app} {ver}; includes the PCI log masking change.",
    "Hotfix {ver} for {app}: issuer timeout handling. Needed before the evening batch.",
    "Scale {app} to 4 replicas for the end-of-month peak.",
    "Update {app} config (new HSM endpoint) in {ns}.",
    "Release {app} {ver} as part of release train {train}.",
    "Restart {app} after the certificate rotation.",
    "Roll back {app} to the previous version — elevated 5xx since {ver}.",
]


def seed_requests_and_bundles(ctx: Ctx, request_count: int, bundle_count: int) -> Dict[str, int]:
    from api.db import db
    from api.models import (
        ChangeBundle,
        ChangeBundleItem,
        ChangeBundleVote,
        DeploymentRequest,
        DeploymentRequestVote,
    )
    from api.services.change_bundle_service import build_item_preview

    rng = ctx.rng
    counts = {"deployment_requests": 0, "deployment_request_votes": 0, "change_bundles": 0,
              "change_bundle_items": 0, "change_bundle_votes": 0}
    approvers = ctx.approver_emails
    admins = ctx.admins or ctx.users

    def app_and_version():
        ns, app = rng.choice(ctx.app_names) if ctx.app_names and rng.random() < 0.7 else (ctx.namespace(), f"{ctx.namespace()}-{rng.choice(COMPONENTS)}")
        return ns, app, f"{rng.randint(1, 6)}.{rng.randint(0, 40)}.{rng.randint(0, 12)}"

    def window(created: datetime, tz: str):
        start = created + timedelta(hours=rng.randint(4, 72))
        start = start.replace(minute=rng.choice([0, 30]), second=0, microsecond=0)
        return start, start + timedelta(hours=rng.choice([1, 2, 2, 3, 4])), tz

    # --- deployment requests ---------------------------------------------
    rows: List[Dict[str, Any]] = []
    plans: List[Tuple[str, int, datetime, Optional[datetime]]] = []
    for _ in range(request_count):
        requester = ctx.user()
        c = ctx.cluster(prod_bias=2.5)
        ns, app, ver = app_and_version()
        status = weighted(rng, [("approved", 72), ("declined", 18), ("pending", 3)])
        if status == "pending":
            created = ctx.now - timedelta(hours=rng.randint(1, 30))
        else:
            created = rand_time(rng, ctx.start, ctx.now - timedelta(days=3), growth=1.0)
        tz = rng.choice(TIMEZONES)
        start, end, tz = window(created, tz) if rng.random() < 0.85 else (None, None, None)
        if status == "pending" and start is not None and start <= ctx.now:
            start, end = ctx.now + timedelta(days=rng.randint(2, 10)), None
            end = start + timedelta(hours=2)
        required = 2 if c["env"] == "PROD" else 1
        decided_at = None
        if status != "pending":
            decided_at = created + timedelta(minutes=rng.randint(5, 600))
        row = {
            "requester_id": requester.id, "cluster_id": c["id"], "cluster_name": c["name"],
            "message": rng.choice(REQUEST_MESSAGES).format(app=app, ver=ver, ns=ns, t=rng.randint(10000, 99999), train=f"R{rng.randint(20, 60)}"),
            "status": status, "required_approvals": required, "total_recipients": len(approvers),
            "created_at": created,
        }
        if start is not None:
            row.update({"requested_window_start": start, "requested_window_end": end, "requested_window_timezone": tz})
        if decided_at:
            row["decided_at"] = decided_at
            row["decided_by_user_id"] = rng.choice(admins).id
        rows.append(row)
        plans.append((status, required, created, decided_at))
    db.session.bulk_insert_mappings(DeploymentRequest, rows, return_defaults=True)
    votes = []
    for row, (status, required, created, decided_at) in zip(rows, plans):
        voters = rng.sample(approvers, k=min(len(approvers), required + (1 if rng.random() < 0.2 else 0)))
        if status == "approved":
            decisions = ["approve"] * min(required, len(voters))
        elif status == "declined":
            decisions = ["decline"]
        else:
            decisions = ["approve"] if required > 1 and rng.random() < 0.5 else []
        for email, decision in zip(voters, decisions):
            votes.append({"request_id": row["id"], "voter_email": email, "decision": decision,
                          "created_at": (decided_at or created) - timedelta(minutes=rng.randint(0, 4))})
    if votes:
        db.session.bulk_insert_mappings(DeploymentRequestVote, votes)
    db.session.commit()
    counts["deployment_requests"] = len(rows)
    counts["deployment_request_votes"] = len(votes)

    # --- change bundles --------------------------------------------------
    preview_cache: Dict[Tuple[str, str, str, int], Dict[str, Any]] = {}
    drafts_left = {u.id for u in ctx.users}
    bundle_rows: List[Dict[str, Any]] = []
    bundle_plans: List[Dict[str, Any]] = []
    for _ in range(bundle_count):
        requester = ctx.user()
        status = weighted(rng, [("completed", 58), ("rejected", 9), ("failed", 5), ("partially_failed", 4),
                                ("expired", 10), ("pending_approval", 4), ("scheduled", 1.5), ("draft", 3)])
        if status == "draft":
            if requester.id not in drafts_left:
                status = "completed"
            else:
                drafts_left.discard(requester.id)
        if status in ("pending_approval", "draft"):
            created = ctx.now - timedelta(hours=rng.randint(1, 72))
        elif status == "scheduled":
            created = ctx.now - timedelta(days=rng.randint(1, 5))
        else:
            created = rand_time(rng, ctx.start, ctx.now - timedelta(days=2), growth=1.0)
        tz = rng.choice(TIMEZONES)
        start, end, tz = window(created, tz)
        if status in ("pending_approval", "scheduled"):
            # Far enough ahead that a server started on this database leaves them alone.
            start = ctx.now + timedelta(days=rng.randint(20, 60))
            end = start + timedelta(hours=3)
        row: Dict[str, Any] = {
            "requester_user_id": None if rng.random() < 0.12 else requester.id, "status": status,
            "note": rng.choice(["Release train", "Hotfix", "Config change", "Monthly patching", "Scale for peak", "Rollback"]) + f" R{rng.randint(20, 60)}",
            "required_approvals": rng.choice([1, 1, 2]), "total_recipients": len(approvers),
            "stop_on_failure": rng.random() < 0.85, "created_at": created, "updated_at": created,
        }
        if status != "draft":
            row.update({"requested_start_time": start, "requested_end_time": end, "requested_window_timezone": tz})
        if status in ("completed", "failed", "partially_failed", "scheduled", "expired"):
            row["approved_by_user_id"] = rng.choice(admins).id
            row["approved_at"] = created + timedelta(minutes=rng.randint(10, 900))
        if status == "rejected":
            row["rejection_reason"] = rng.choice(["Outside the change window", "Missing rollback plan", "Freeze period", "Wrong image tag"])
        if status in ("completed", "failed", "partially_failed"):
            row["execution_started_at"] = start
            row["execution_finished_at"] = start + timedelta(seconds=rng.randint(20, 900))
            row["updated_at"] = row["execution_finished_at"]
        if status == "expired":
            row["execution_finished_at"] = end
        bundle_rows.append(row)
        bundle_plans.append({"status": status, "created": created})
    db.session.bulk_insert_mappings(ChangeBundle, bundle_rows, return_defaults=True)

    items: List[Dict[str, Any]] = []
    bundle_votes: List[Dict[str, Any]] = []
    for row, plan in zip(bundle_rows, bundle_plans):
        status = plan["status"]
        c = ctx.cluster(prod_bias=2.0)
        n_items = weighted(rng, [(1, 40), (2, 25), (3, 15), (4, 10), (5, 6), (6, 4)])
        fail_at = rng.randrange(n_items) if status in ("failed", "partially_failed") else None
        for pos in range(n_items):
            ns, app, ver = app_and_version()
            action = weighted(rng, [("change_image", 45), ("scale_replicas", 12), ("restart_workload", 12),
                                    ("update_env", 10), ("update_resources", 8), ("rollback_deployment", 5),
                                    ("apply_yaml", 8)])
            if action == "change_image":
                image = f"nexus.areeba.local:8082/{ns}/{app}:{ver}"
                yaml_preview = (
                    f"apiVersion: apps/v1\nkind: Deployment\nmetadata:\n  name: {app}\n  namespace: {ns}\nspec:\n"
                    f"  template:\n    spec:\n      containers:\n        - name: {app}\n          image: {image}\n"
                )
                built = {"yamlPreview": yaml_preview, "execution": {"mode": "apply"}, "resourceKind": "Deployment",
                         "resourceName": app, "namespace": ns}
                payload = {"clusterId": c["id"], "namespace": ns, "resourceKind": "Deployment", "resourceName": app,
                           "containerName": app, "image": image}
                old_payload = {"image": f"nexus.areeba.local:8082/{ns}/{app}:{rng.randint(1, 6)}.{rng.randint(0, 40)}.0"}
            else:
                if action == "scale_replicas":
                    payload = {"namespace": ns, "resourceKind": "Deployment", "resourceName": app, "replicas": rng.randint(1, 6)}
                elif action in ("restart_workload", "rollback_deployment"):
                    payload = {"namespace": ns, "resourceKind": "Deployment", "resourceName": app}
                else:
                    payload = {"namespace": ns, "yaml": (
                        f"apiVersion: apps/v1\nkind: Deployment\nmetadata:\n  name: {app}\n  namespace: {ns}\nspec:\n"
                        f"  replicas: {rng.randint(1, 4)}\n  template:\n    spec:\n      containers:\n        - name: {app}\n"
                        f"          image: nexus.areeba.local:8082/{ns}/{app}:{ver}\n          env:\n"
                        f"            - name: SPRING_PROFILES_ACTIVE\n              value: {c['env'].lower()}\n"
                        f"          resources:\n            limits:\n              memory: {rng.choice(['512Mi', '1Gi', '2Gi'])}\n")}
                cache_key = (action, ns, app, int(payload.get("replicas") or 0))
                built = preview_cache.get(cache_key)
                if built is None:
                    built = build_item_preview(action, payload)
                    preview_cache[cache_key] = built
                payload = dict(payload, clusterId=c["id"])
                old_payload = None
            if status in ("completed",):
                item_status = "succeeded"
            elif status in ("failed", "partially_failed"):
                item_status = "succeeded" if pos < fail_at else ("failed" if pos == fail_at else ("skipped" if status == "failed" else "succeeded"))
            elif status in ("expired",):
                item_status = "skipped"
            else:
                item_status = "pending"
            item = {
                "bundle_id": row["id"], "position": pos, "action_type": action, "cluster_id": c["id"],
                "cluster_name": c["name"], "namespace": built.get("namespace") or ns,
                "resource_kind": built.get("resourceKind") or "Deployment", "resource_name": built.get("resourceName") or app,
                "new_payload_json": {"input": payload, "execution": built.get("execution") or {"mode": "apply"}},
                "yaml_preview": built.get("yamlPreview"), "validation_status": "valid", "status": item_status,
                "created_at": plan["created"], "updated_at": row.get("execution_finished_at") or plan["created"],
            }
            if old_payload:
                item["old_payload_json"] = old_payload
            mode = (built.get("execution") or {}).get("mode", "apply")
            if item_status == "succeeded":
                item["execution_result"] = {"ok": True, "output": f"deployment.apps/{app} configured", "mode": mode}
            elif item_status == "failed":
                item["execution_result"] = {"ok": False, "error": rng.choice([
                    f"deployments.apps \"{app}\" not found", "error: timed out waiting for the condition",
                    f"image nexus.areeba.local:8082/{ns}/{app}:{ver} not found in registry"]), "mode": mode}
            elif item_status == "skipped":
                item["execution_result"] = {"skipped": True, "reason": "window expired" if status == "expired" else "stopped after earlier failure"}
            items.append(item)
        if status not in ("draft",):
            voters = rng.sample(approvers, k=min(len(approvers), row["required_approvals"]))
            decision = "decline" if status == "rejected" else "approve"
            if status == "pending_approval":
                voters = voters[: row["required_approvals"] - 1]
            for email in voters:
                bundle_votes.append({"bundle_id": row["id"], "voter_email": email, "decision": decision,
                                     "created_at": row.get("approved_at") or plan["created"] + timedelta(minutes=rng.randint(5, 300))})
    for start in range(0, len(items), 3000):
        db.session.bulk_insert_mappings(ChangeBundleItem, items[start:start + 3000])
    if bundle_votes:
        db.session.bulk_insert_mappings(ChangeBundleVote, bundle_votes)
    db.session.commit()
    counts["change_bundles"] = len(bundle_rows)
    counts["change_bundle_items"] = len(items)
    counts["change_bundle_votes"] = len(bundle_votes)
    return counts


# ---------------------------------------------------------------------------
# Inventory
# ---------------------------------------------------------------------------

TEMPLATE_CATEGORIES = ["Payments", "Issuing", "Acquiring", "Platform", "Data", "Observability", "Batch",
                       "Gateways", "Mobile Backends", "Internal Tools"]


def seed_inventory(ctx: Ctx, template_count: int, version_count: int, catalog_count: int) -> Dict[str, int]:
    from api.db import db
    from api.models import AppCatalogEntry, ApplicationDeploymentVersion, UserTemplate

    rng = ctx.rng
    counts = {"user_templates": 0, "app_catalog_entries": 0, "application_deployment_versions": 0}
    uniq = Uniq(r[0] for r in db.session.query(UserTemplate.slug).all())
    rows = []
    for i in range(template_count):
        domain = rng.choice(DOMAINS)
        component = rng.choice(COMPONENTS)
        flavour = rng.choice(["", " (Java 17)", " (Node 22)", " v2", " HA", " SIT", " UAT", " PROD"])
        name = f"{domain.title()} {component}{flavour}"
        slug = uniq(name.lower().replace(" ", "-").replace("(", "").replace(")", ""))
        image = f"nexus.areeba.local:8082/{domain}/{domain}-{component}"
        port = rng.choice([8080, 8080, 8081, 3000, 9090])
        spec = {
            "containers": [{"name": f"{domain}-{component}", "image": image, "tag": f"{rng.randint(1, 5)}.{rng.randint(0, 30)}.0", "ports": [port]}],
            "resources": {"cpuRequest": rng.choice(["100m", "250m", "500m"]), "memoryRequest": rng.choice(["256Mi", "512Mi", "1Gi"]),
                          "cpuLimit": rng.choice(["500m", "1", "2"]), "memoryLimit": rng.choice(["512Mi", "1Gi", "2Gi"])},
            "networking": {"service": {"enabled": True, "type": "ClusterIP", "port": 80, "targetPort": port}},
            "scaling": {"replicas": rng.randint(1, 4)},
            "schema": {"env": [
                {"key": "SPRING_PROFILES_ACTIVE", "required": False, "default": "sit", "allowedSources": ["value"]},
                {"key": "LOG_LEVEL", "required": False, "default": "INFO", "allowedSources": ["value"]},
            ]},
        }
        created = rand_time(rng, ctx.start, ctx.now - timedelta(days=1), growth=0.8)
        rows.append({"slug": slug, "name": name[:120], "description": f"{TEAMS.get(domain, 'Platform')} deployment template",
                     "category": rng.choice(TEMPLATE_CATEGORIES), "workload_type": "StatefulSet" if rng.random() < 0.08 else "Deployment",
                     "spec": spec, "created_by": rng.choice(ctx.admins or ctx.users).id, "created_at": created, "updated_at": created})
    db.session.bulk_insert_mappings(UserTemplate, rows)
    db.session.commit()
    counts["user_templates"] = len(rows)

    # Apps: what versions and catalog entries are about.
    apps: List[Tuple[Dict[str, Any], str, str]] = []
    seen = set()
    while len(apps) < max(1, version_count // 8):
        c = ctx.cluster()
        ns = ctx.namespace()
        app = f"{ns}-{rng.choice(COMPONENTS)}"
        if (c["id"], ns, app) in seen:
            if len(seen) > 5000:
                break
            continue
        seen.add((c["id"], ns, app))
        apps.append((c, ns, app))

    catalog_rows = []
    for c, ns, app in apps[:catalog_count]:
        created = rand_time(rng, ctx.start, ctx.now - timedelta(days=10), growth=0.5)
        catalog_rows.append({
            "cluster_id": c["id"], "namespace": ns, "workload_type": "Deployment", "workload_name": app,
            "display_name": app.replace("-", " ").title(), "owner_team": TEAMS.get(ns, "Platform"),
            "environment": c["env"], "criticality": rng.choice(["low", "medium", "high", "critical"]),
            "description": f"{app} in {ns}", "tags": [ns, c["env"].lower()], "source": "Registered",
            "contact_email": f"{ns}-team@kubesight-stress.local", "created_by_user_id": rng.choice(ctx.users).id,
            "created_at": created, "updated_at": created, "is_active": True,
        })
    if catalog_rows:
        db.session.bulk_insert_mappings(AppCatalogEntry, catalog_rows, return_defaults=True)
    catalog_by_app = {(r["cluster_id"], r["namespace"], r["workload_name"]): r["id"] for r in catalog_rows}
    counts["app_catalog_entries"] = len(catalog_rows)

    versions = []
    # Busy apps have dozens of versions, quiet ones one or two.
    app_weights = [rng.lognormvariate(0, 0.8) for _ in apps]
    total_weight = sum(app_weights) or 1.0
    per_app = [max(1, int(version_count * w / total_weight)) for w in app_weights]
    per_app[0] += max(0, version_count - sum(per_app))
    for idx, (c, ns, app) in enumerate(apps):
        n = per_app[idx]
        times = sorted(rand_time(rng, ctx.start, ctx.now, growth=1.0) for _ in range(n))
        major, minor = 1, 0
        for k, moment in enumerate(times):
            if k:
                minor += 1
                if minor >= 10:
                    major, minor = major + 1, 0
            tag = f"{rng.randint(1, 6)}.{rng.randint(0, 40)}.{rng.randint(0, 12)}"
            versions.append({
                "catalog_entry_id": catalog_by_app.get((c["id"], ns, app)), "cluster_id": c["id"], "namespace": ns,
                "app_name": app, "version_label": f"v{major}.{minor}", "version_major": major, "version_minor": minor,
                "workload_type": "Deployment",
                "change_summary": rng.choice([f"Image {tag}", f"Deployed Deployment {app}", "Env update", "Resources update", f"Rollback to v{major}.{max(0, minor - 1)}"]),
                "yaml_snapshot": (
                    f"apiVersion: apps/v1\nkind: Deployment\nmetadata:\n  name: {app}\n  namespace: {ns}\n  labels:\n    app: {app}\n"
                    f"spec:\n  replicas: {rng.randint(1, 4)}\n  selector:\n    matchLabels:\n      app: {app}\n  template:\n"
                    f"    metadata:\n      labels:\n        app: {app}\n    spec:\n      containers:\n        - name: {app}\n"
                    f"          image: nexus.areeba.local:8082/{ns}/{app}:{tag}\n          ports:\n            - containerPort: 8080\n"
                ),
                "created_by_user_id": ctx.user().id, "created_at": moment,
            })
    for start in range(0, len(versions), 3000):
        batch = versions[start:start + 3000]
        with_catalog = [v for v in batch if v["catalog_entry_id"] is not None]
        without = [{k: v for k, v in row.items() if k != "catalog_entry_id"} for row in batch if row["catalog_entry_id"] is None]
        if with_catalog:
            db.session.bulk_insert_mappings(ApplicationDeploymentVersion, with_catalog)
        if without:
            db.session.bulk_insert_mappings(ApplicationDeploymentVersion, without)
    db.session.commit()
    counts["application_deployment_versions"] = len(versions)
    return counts


# ---------------------------------------------------------------------------
# App services, clients, topology components, mobile apps
# ---------------------------------------------------------------------------

def seed_catalog_extras(ctx: Ctx, client_count: int, service_count: int, component_count: int, mobile_count: int) -> Dict[str, int]:
    from api.db import db
    from api.models import (
        ApplicationService,
        ApplicationServiceDeployment,
        ApplicationServiceTopologyEdge,
        ApplicationServiceTopologyNode,
        Client,
        ClientApplicationService,
        ClientServiceConnection,
        MobileAppBuild,
        MobileApplication,
        TopologyComponent,
    )

    rng = ctx.rng
    counts = {"topology_components": 0, "application_services": 0, "application_service_deployments": 0,
              "application_service_topology_nodes": 0, "application_service_topology_edges": 0,
              "clients": 0, "client_application_services": 0, "client_service_connections": 0,
              "mobile_applications": 0, "mobile_app_builds": 0}

    comp_uniq = Uniq(r[0] for r in db.session.query(TopologyComponent.name).all())
    components = []
    for name, category in [("WAF", "Security"), ("API Gateway", "Gateway"), ("HSM", "Security"), ("Kafka", "Messaging"),
                           ("Redis", "Cache"), ("Oracle DB", "Database"), ("PostgreSQL", "Database"), ("F5 Load Balancer", "Network"),
                           ("Visa Gateway", "External"), ("Mastercard MIP", "External"), ("SMTP Relay", "Messaging"),
                           ("Keycloak", "Identity"), ("Elastic", "Observability"), ("SFTP", "Integration"), ("RabbitMQ", "Messaging")][:component_count]:
        row = TopologyComponent(name=comp_uniq(name), category=category, description=f"{name} ({category})", check_type="none",
                                last_status="unknown", created_by=ctx.admin_user.id if ctx.admin_user else None)
        db.session.add(row)
        components.append(row)
    db.session.flush()
    counts["topology_components"] = len(components)

    svc_uniq = Uniq(r[0] for r in db.session.query(ApplicationService.name).all())
    services = []
    for i in range(service_count):
        domain = DOMAINS[i % len(DOMAINS)]
        name = svc_uniq(f"{domain.title()} Platform" if i < len(DOMAINS) else f"{domain.title()} Platform {i // len(DOMAINS) + 1}")
        svc = ApplicationService(name=name, description=f"{TEAMS.get(domain, 'Platform')} application service")
        db.session.add(svc)
        db.session.flush()
        deployments = []
        used = set()
        for _ in range(rng.randint(2, 6)):
            c = ctx.cluster(prod_bias=1.5)
            dep = f"{domain}-{rng.choice(COMPONENTS)}"
            if (c["id"], dep) in used:
                continue
            used.add((c["id"], dep))
            db.session.add(ApplicationServiceDeployment(service_id=svc.id, cluster_id=c["id"], namespace=domain,
                                                        deployment_name=dep, resource_kind="deployment"))
            deployments.append((c, dep))
        db.session.flush()
        nodes = []
        for k, (c, dep) in enumerate(deployments):
            node = ApplicationServiceTopologyNode(service_id=svc.id, name=dep, type="Deployment", linked_cluster_id=c["id"],
                                                  linked_namespace=domain, linked_deployment=dep,
                                                  position_x=float(120 + 220 * k), position_y=float(200 + rng.randint(-60, 60)))
            db.session.add(node)
            nodes.append(node)
        if components:
            for comp in rng.sample(components, k=min(len(components), rng.randint(1, 2))):
                node = ApplicationServiceTopologyNode(service_id=svc.id, name=comp.name, type=comp.category, component_id=comp.id,
                                                      position_x=float(rng.randint(0, 900)), position_y=float(rng.randint(0, 400)))
                db.session.add(node)
                nodes.insert(0, node)
        db.session.flush()
        for a, b in zip(nodes, nodes[1:]):
            db.session.add(ApplicationServiceTopologyEdge(service_id=svc.id, source_node_id=a.id, target_node_id=b.id,
                                                          protocol=rng.choice(["HTTP", "HTTPS", "gRPC", "TCP"]),
                                                          scope=rng.choice(["internal", "internal", "external"])))
            counts["application_service_topology_edges"] += 1
        services.append(svc)
        counts["application_service_deployments"] += len(deployments)
        counts["application_service_topology_nodes"] += len(nodes)
    db.session.flush()
    counts["application_services"] = len(services)

    client_uniq = Uniq(r[0] for r in db.session.query(Client.name).all())
    banks = ["Bank Audi", "BLOM Bank", "Byblos Bank", "Fransabank", "SGBL", "Bankmed", "Credit Libanais", "BBAC",
             "IBL Bank", "Lebanon & Gulf Bank", "First National Bank", "Al Mawarid", "Cedrus Bank", "Banque BEMO",
             "Saradar Bank", "Arab Bank", "Bank of Beirut", "Creditbank", "Fenicia Bank", "Emirates NBD", "QNB",
             "Bank Muscat", "Al Rajhi", "Mashreq", "ADCB"]
    transports = ["VPN", "Leased Line", "MPLS", "Internet", "Private Link", "Internal Network"]
    for i in range(client_count):
        name = client_uniq(banks[i % len(banks)] + ("" if i < len(banks) else f" {i // len(banks) + 1}"))
        client = Client(name=name, contact_person=f"{rng.choice(FIRST_NAMES)} {rng.choice(LAST_NAMES)}",
                        email=f"ops@{name.lower().replace(' ', '').replace('&', '')}.example", phone=f"+961 1 {rng.randint(100000, 999999)}")
        db.session.add(client)
        db.session.flush()
        for svc in rng.sample(services, k=min(len(services), rng.randint(1, 5))) if services else []:
            db.session.add(ClientApplicationService(client_id=client.id, service_id=svc.id))
            counts["client_application_services"] += 1
            if rng.random() < 0.6:
                c = ctx.cluster(prod_bias=2.0)
                db.session.add(ClientServiceConnection(
                    client_id=client.id, service_id=svc.id, source_ip=f"10.{rng.randint(1, 250)}.{rng.randint(0, 255)}.{rng.randint(1, 254)}",
                    destination_ip=f"172.16.{rng.randint(0, 255)}.{rng.randint(1, 254)}", transport_type=rng.choice(transports),
                    cluster_id=c["id"], namespace=rng.choice(DOMAINS), environment=c["env"], direction=rng.choice(["inbound", "inbound", "both"]),
                    status=rng.choice(["active", "active", "active", "planned", "degraded"]), is_active=True,
                ))
                counts["client_service_connections"] += 1
        counts["clients"] += 1
    db.session.flush()

    mobile_names = ["areeba Wallet", "POS Mobile", "Merchant App", "Agent App", "Cards Companion", "Pay by Link",
                    "Bank Audi Pay", "SoftPOS", "Loyalty Rewards", "Field Onboarding"]
    for i in range(mobile_count):
        name = mobile_names[i % len(mobile_names)] + ("" if i < len(mobile_names) else f" {i + 1}")
        pkg = "com.areeba." + name.lower().replace(" ", "")
        app = MobileApplication(name=name, description=f"{name} mobile app", enabled=True,
                                zoho_environment=name, jenkins_job_path=f"mobile/{name.lower().replace(' ', '-')}",
                                android_package_name=pkg, ios_bundle_id=pkg if rng.random() < 0.6 else "")
        db.session.add(app)
        db.session.flush()
        builds = []
        for k, moment in enumerate(sorted(rand_time(rng, ctx.start, ctx.now - timedelta(hours=3)) for _ in range(rng.randint(15, 60)))):
            platform = "ios" if app.ios_bundle_id and rng.random() < 0.35 else "android"
            artifact = "ipa" if platform == "ios" else rng.choice(["apk", "aab"])
            ok = rng.random() > 0.07
            version = f"{rng.randint(1, 5)}.{rng.randint(0, 20)}.{k}"
            builds.append({
                "app_id": app.id, "platform": platform, "artifact_type": artifact, "version": version,
                "file_name": f"{pkg}-{version}.{artifact}", "file_size": rng.randint(20, 180) * 1024 * 1024,
                "sha256": _sha256(rng), "signature_state": rng.choice(["signed", "signed", "unsigned", "unknown"]),
                "jenkins_build_number": 100 + k, "jenkins_build_url": f"https://jenkins.areeba.local/job/mobile/job/{pkg}/{100 + k}/",
                "ticket_number": f"#{rng.randint(10000, 99999)}", "source": rng.choice(["ticket", "ticket", "manual", "upload"]),
                "status": "available" if ok else "failed", "retry_count": 0 if ok else rng.randint(1, 3),
                "created_at": moment, "updated_at": moment,
                **({"downloaded_at": moment + timedelta(minutes=2), "storage_path": f"{app.id}/stress/{pkg}-{version}.{artifact}"} if ok
                   else {"error": "Artifact not found in the Jenkins build"}),
            })
        db.session.bulk_insert_mappings(MobileAppBuild, builds)
        counts["mobile_applications"] += 1
        counts["mobile_app_builds"] += len(builds)
    db.session.commit()
    return counts


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def _table_counts(tables: Sequence[str]) -> Dict[str, int]:
    from api.db import db

    out = {}
    for table in tables:
        try:
            out[table] = int(db.session.execute(db.text(f'SELECT COUNT(*) FROM "{table}"')).scalar() or 0)
        except Exception:
            db.session.rollback()
            out[table] = -1
    return out


SUMMARY_TABLES = [
    "users", "user_cluster_access", "access_rules", "audit_logs", "alert_policies", "alert_history",
    "ci_services", "ci_pipelines", "ci_pipeline_stages", "ci_builds", "ci_build_stages", "ci_log_chunks",
    "ci_artifacts", "ci_service_deployments", "deployment_requests", "deployment_request_votes",
    "change_bundles", "change_bundle_items", "change_bundle_votes", "user_templates", "app_catalog_entries",
    "application_deployment_versions", "topology_components", "application_services",
    "application_service_deployments", "application_service_topology_nodes", "application_service_topology_edges",
    "clients", "client_application_services", "client_service_connections", "mobile_applications", "mobile_app_builds",
]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--database-url", help="Target database (sets DATABASE_URL). Defaults to the environment's.")
    parser.add_argument("--scale", type=float, default=1.0, help="Multiply every volume (default 1.0).")
    parser.add_argument("--password", default="Stress-Test-1!", help="Password of the stress-user-NN accounts.")
    parser.add_argument("--seed", type=int, default=20261006, help="Random seed (deterministic output).")
    parser.add_argument("--force", action="store_true", help="Seed again even if stress users already exist.")
    args = parser.parse_args()

    _prepare_environment(args.database_url)
    started = time.perf_counter()
    app, database_url = _build_app()
    print(f"Database: {database_url}", flush=True)
    _progress("app built, migrations + default seed run", started)

    exit_code = 0
    with app.app_context():
        from api.models import User

        if User.query.filter_by(username="stress-user-01").first() and not args.force:
            print("Stress data is already present (stress-user-01 exists). Use --force to add another batch.", flush=True)
            os._exit(0)

        before = _table_counts(SUMMARY_TABLES)
        ctx = Ctx(random.Random(args.seed), args.scale, args.password)
        s = args.scale
        _load_clusters(ctx)
        print(f"Clusters: {', '.join(c['id'] + '=' + c['name'] for c in ctx.clusters)}", flush=True)
        try:
            seed_users(ctx, _scaled(60, s))
            _progress(f"users ({len(ctx.users)})", started)
            seed_ci(ctx, _scaled(80, s), 150.0, min(6, _scaled(4, s)))
            _progress("CI", started)
            seed_audit_logs(ctx, _scaled(60000, s))
            _progress("audit logs", started)
            seed_alerts(ctx, _scaled(25, s, minimum=3), _scaled(20000, s))
            _progress("alerts", started)
            seed_requests_and_bundles(ctx, _scaled(3000, s), _scaled(1500, s))
            _progress("deployment requests + change bundles", started)
            seed_inventory(ctx, _scaled(300, s), _scaled(5000, s), _scaled(300, s))
            _progress("inventory", started)
            seed_catalog_extras(ctx, _scaled(25, s), _scaled(30, s), 15, _scaled(8, s))
            _progress("app services, clients, components, mobile", started)
        except Exception:
            import traceback

            traceback.print_exc()
            exit_code = 1
        finally:
            from api.db import db

            db.session.rollback()
            after = _table_counts(SUMMARY_TABLES)
            print("\nRow counts (before -> after, +added):")
            width = max(len(t) for t in SUMMARY_TABLES)
            for table in SUMMARY_TABLES:
                b, a = before.get(table, 0), after.get(table, 0)
                print(f"  {table:<{width}}  {b:>8} -> {a:>8}  (+{a - b})")
            print(f"\nDone in {time.perf_counter() - started:.1f}s. Stress users: stress-user-01..{_scaled(60, s):02d} / {args.password}")
            sys.stdout.flush()
            db.session.remove()
            db.engine.dispose()
    # Nothing was started in the background, but leave no doubt.
    os._exit(exit_code)


if __name__ == "__main__":
    main()
