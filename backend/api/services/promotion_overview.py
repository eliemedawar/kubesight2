"""What the Promotions page shows: every application across the ladder.

One scan per cluster, one pass over the ledger, and the whole picture comes
back in one payload — sized for a ladder with hundreds of applications, where
the page does its filtering and grouping in the browser:

* ``environments`` — the rungs, with how many applications and workloads each
  holds;
* ``gates`` — each hop (Dev → SIT, …) with how many applications are ready,
  soaking, waiting, blocked, awaiting approval or in sync;
* ``apps`` — one per image repository (images are built once, so the
  repository IS the application), with its version in each environment, the
  state of each hop, how many hops it lags, and the system / team it belongs
  to for grouping.
"""

from __future__ import annotations

import logging
import re
from collections import Counter
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from ..db import db
from ..models_promotion import PromotionEnvironment, PromotionRecord
from . import promotion_service as core

logger = logging.getLogger(__name__)

# Hop states that mean "the higher environment is behind".
BEHIND_STATES = ("ready", "soaking", "waiting", "blocked", "pending_approval")

_ENV_WORDS = {
    "dev", "develop", "development", "sit", "uat", "qa", "test", "tst", "stg", "stage", "staging",
    "pre", "preprod", "pp", "prod", "prd", "production", "int", "integration", "perf",
}


def _env_tokens(rungs: List[PromotionEnvironment]) -> set:
    words = set(_ENV_WORDS)
    for env in rungs:
        words.add(env.key.lower())
        words.add(re.sub(r"[^a-z0-9]", "", env.name.lower()))
    return words


def namespace_base(namespace: str, env_words: set) -> str:
    """``payments-sit`` → ``payments``: the part of a namespace that names the
    system rather than the environment."""
    parts = [p for p in re.split(r"[-_.]", namespace or "") if p]
    kept = [p for p in parts if p.lower() not in env_words]
    return "-".join(kept) or namespace


def _ci_links() -> Dict[Tuple[str, str, str], Any]:
    try:
        from .ci.deployment_links import index_all

        return index_all()
    except Exception:  # noqa: BLE001
        return {}


def pending_changes() -> Dict[Tuple[str, str, str], List[Tuple[str, int]]]:
    """Workload → [(image key, bundle id)] for changes waiting on approval, so a
    hop can say "awaiting approval" instead of offering the same promotion twice."""
    from ..models import ChangeBundle, ChangeBundleItem

    out: Dict[Tuple[str, str, str], List[Tuple[str, int]]] = {}
    try:
        items = (
            ChangeBundleItem.query.join(ChangeBundle, ChangeBundleItem.bundle_id == ChangeBundle.id)
            .filter(ChangeBundle.status.in_(("pending_approval", "approved", "scheduled", "deploying")))
            .all()
        )
    except Exception:  # noqa: BLE001 — a nicety; never break the page
        logger.exception("Could not read pending change bundles")
        return out
    for item in items:
        for image in core.images_in_yaml(item.yaml_preview or ""):
            parsed = core.parse_image(image)
            if parsed:
                key = (str(item.cluster_id), item.namespace or "", item.resource_name or "")
                out.setdefault(key, []).append((parsed["image"], item.bundle_id))
    return out


def _cluster_rules(cluster_ids: List[str]) -> Dict[str, Dict[str, Any]]:
    from .deployment_request_service import cluster_required_approvals

    out = {}
    for cluster_id in cluster_ids:
        try:
            required = int(cluster_required_approvals(cluster_id) or 0)
        except Exception:  # noqa: BLE001
            required = 0
        out[cluster_id] = {"name": core.cluster_name(cluster_id), "requiredApprovals": required}
    return out


def _latest(dts: List[Optional[datetime]]) -> Optional[datetime]:
    values = [core._aware(dt) for dt in dts if dt is not None]
    return max(values) if values else None


def build_overview() -> Dict[str, Any]:
    rungs = core.ladder()
    pol = core.policy()
    resolver = core.Resolver()
    by_env, errors = core.scan_ladder(resolver)
    ledgers = core.ledger_by_env([env.id for env in rungs])

    # What the page sees running healthy has passed that environment: record it
    # now rather than wait for the observer's next tick.
    for env in rungs:
        core.record_healthy(env, by_env.get(env.id, []), existing=ledgers[env.id])
    if rungs:
        db.session.commit()

    links = _ci_links()
    pending = pending_changes()
    env_words = _env_tokens(rungs)

    apps: Dict[str, Dict[str, Any]] = {}
    exempt_repos = set()
    clusters_seen = set()
    for env in rungs:
        for workload in by_env.get(env.id, []):
            clusters_seen.add(workload["clusterId"])
            for container in workload["containers"]:
                parsed = core.parse_image(container["image"])
                if not parsed:
                    continue
                if core.exempt(parsed["repository"], pol.exempt_images):
                    exempt_repos.add(parsed["repository"])
                    continue
                app = apps.setdefault(
                    parsed["repository"],
                    {"cells": {}, "partOf": Counter(), "bases": Counter(), "ciService": None, "namespaces": set()},
                )
                if workload["partOf"]:
                    app["partOf"][workload["partOf"]] += 1
                app["bases"][namespace_base(workload["namespace"], env_words)] += 1
                app["namespaces"].add(workload["namespace"])
                link = links.get((str(workload["clusterId"]), workload["namespace"], workload["name"]))
                service = getattr(link, "service", None) if link is not None else None
                if service is not None and app["ciService"] is None:
                    app["ciService"] = {
                        "id": service.id,
                        "name": service.name,
                        "team": getattr(service, "owner_team", None) or None,
                    }
                app["cells"].setdefault(env.id, []).append(
                    {
                        "clusterId": workload["clusterId"],
                        "namespace": workload["namespace"],
                        "kind": workload["kind"],
                        "name": workload["name"],
                        "container": container["name"],
                        "image": parsed["image"],
                        "tag": parsed["tag"],
                        "state": workload["state"],
                        "ready": workload["ready"],
                        "desired": workload["desired"],
                    }
                )

    gate_counts: List[Counter] = [Counter() for _ in range(max(0, len(rungs) - 1))]
    env_apps: Counter = Counter()
    out_apps = []
    for repository, app in apps.items():
        cells = []
        for env in rungs:
            entries = app["cells"].get(env.id) or []
            if entries:
                env_apps[env.id] += 1
            images = sorted({e["image"] for e in entries})
            states = {e["state"] for e in entries}
            record_times = [
                getattr(ledgers[env.id].get(image), "first_healthy_at", None) for image in images
            ]
            cells.append(
                {
                    "environmentId": env.id,
                    "images": images,
                    "tags": sorted({e["tag"] for e in entries}),
                    "state": (
                        "empty" if not entries
                        else "progressing" if "progressing" in states
                        else "scaled_down" if states == {"scaled_down"}
                        else "healthy"
                    ),
                    "healthySince": core._iso(_latest(record_times)),
                    "workloads": entries,
                    "drift": [],
                }
            )
        steps = []
        for index in range(1, len(rungs)):
            source_env, target_env = rungs[index - 1], rungs[index]
            source, target = cells[index - 1], cells[index]
            step = _hop(source_env, target_env, source, target, ledgers, pending, pol)
            steps.append(step)
            gate_counts[index - 1][step["state"]] += 1
            # Drift: the target runs an image that never passed the source —
            # not counting what was there when the namespace joined the ladder.
            if target_env.mode != "off":
                for image in target["images"]:
                    here = ledgers[target_env.id].get(image)
                    if here is not None and here.source == "baseline":
                        continue
                    if image not in ledgers[source_env.id] and image not in source["images"]:
                        target["drift"].append(
                            {"image": image, "tag": (core.parse_image(image) or {}).get("tag"), "skipped": source_env.name}
                        )
                if target["drift"]:
                    gate_counts[index - 1]["drift"] += 1
        system = (
            app["partOf"].most_common(1)[0][0]
            if app["partOf"]
            else app["bases"].most_common(1)[0][0]
            if app["bases"]
            else "Ungrouped"
        )
        name = (app["ciService"] or {}).get("name") or core.repo_short_name(repository)
        out_apps.append(
            {
                "key": repository,
                "repository": repository,
                "name": name,
                "system": system,
                "team": (app["ciService"] or {}).get("team"),
                "ciService": app["ciService"],
                "namespaces": sorted(app["namespaces"]),
                "cells": cells,
                "steps": steps,
                "lag": sum(1 for s in steps if s["state"] in BEHIND_STATES),
                "drift": any(c["drift"] for c in cells),
            }
        )
    out_apps.sort(key=lambda a: (a["system"].lower(), a["name"].lower()))

    gates = []
    for index in range(1, len(rungs)):
        counts = gate_counts[index - 1]
        gates.append(
            {
                "fromEnvironmentId": rungs[index - 1].id,
                "toEnvironmentId": rungs[index].id,
                "counts": {
                    state: counts.get(state, 0)
                    for state in ("ready", "soaking", "waiting", "blocked", "pending_approval", "in_sync", "not_deployed", "drift")
                },
            }
        )

    environments = []
    for env in rungs:
        workloads = by_env.get(env.id, [])
        environments.append(
            core.serialize_environment(
                env,
                appCount=env_apps.get(env.id, 0),
                workloadCount=len(workloads),
                progressingCount=sum(1 for w in workloads if w["state"] == "progressing"),
            )
        )

    return {
        "environments": environments,
        "gates": gates,
        "apps": out_apps,
        "clusters": _cluster_rules(sorted(clusters_seen | set(resolver.clusters))),
        "summary": {
            "applications": len(out_apps),
            "readyToPromote": sum(g["counts"]["ready"] for g in gates),
            "awaitingApproval": sum(g["counts"]["pending_approval"] for g in gates),
            "blocked": sum(g["counts"]["blocked"] for g in gates),
            "drift": sum(1 for a in out_apps if a["drift"]),
            "behind": sum(1 for a in out_apps if a["lag"]),
            "exempt": len(exempt_repos),
            "systems": len({a["system"] for a in out_apps}),
        },
        "policy": core.serialize_policy(pol),
        "errors": errors,
        "scannedAt": core._iso(datetime.now(timezone.utc)),
    }


def _hop(
    source_env: PromotionEnvironment,
    target_env: PromotionEnvironment,
    source: Dict[str, Any],
    target: Dict[str, Any],
    ledgers: Dict[int, Dict[str, PromotionRecord]],
    pending: Dict[Tuple[str, str, str], List[Tuple[str, int]]],
    pol,
) -> Dict[str, Any]:
    """What the hop from one environment to the next can do for one app."""
    step: Dict[str, Any] = {
        "fromEnvironmentId": source_env.id,
        "toEnvironmentId": target_env.id,
        "state": "idle",
        "image": None,
        "tag": None,
        "targetTags": target["tags"],
        "passedAt": None,
        "detail": "",
        "targets": [],
    }
    if not source["workloads"]:
        step["detail"] = f"Not in {source_env.name}."
        return step
    source_ledger = ledgers[source_env.id]
    candidate = sorted(
        source["images"],
        key=lambda image: core._aware(getattr(source_ledger.get(image), "first_healthy_at", None))
        or datetime.min.replace(tzinfo=timezone.utc),
        reverse=True,
    )[0]
    parsed = core.parse_image(candidate) or {}
    record = source_ledger.get(candidate)
    step["image"] = candidate
    step["tag"] = parsed.get("tag")
    step["passedAt"] = core._iso(getattr(record, "first_healthy_at", None))
    if not target["workloads"]:
        step["state"] = "not_deployed"
        step["detail"] = f"Not deployed in {target_env.name} — deploy it there once; after that it is promoted."
        return step
    step["targets"] = [
        {
            "clusterId": w["clusterId"],
            "namespace": w["namespace"],
            "kind": w["kind"],
            "name": w["name"],
            "container": w["container"],
            "image": w["image"],
            "tag": w["tag"],
        }
        for w in target["workloads"]
    ]
    behind = [w for w in target["workloads"] if w["image"] != candidate]
    if not behind:
        step["state"] = "in_sync"
        step["detail"] = f"{target_env.name} runs {parsed.get('tag')}, the same as {source_env.name}."
        return step
    waiting_bundles = []
    for w in behind:
        hits = [
            bundle_id
            for image, bundle_id in pending.get((str(w["clusterId"]), w["namespace"], w["name"]), [])
            if image == candidate
        ]
        if not hits:
            waiting_bundles = []
            break
        waiting_bundles.append(hits[0])
    if waiting_bundles:
        ids = sorted(set(waiting_bundles))
        step["state"] = "pending_approval"
        step["bundleIds"] = ids
        step["detail"] = (
            f"{parsed.get('tag')} is waiting for approval — change bundle "
            + ", ".join(f"#{i}" for i in ids)
            + f". It is deployed to {target_env.name} once approved."
        )
        return step
    if record is None:
        step["state"] = "waiting"
        step["detail"] = f"{parsed.get('tag')} is not healthy in {source_env.name} yet."
        return step
    if parsed.get("mutable") and pol.require_versioned_tags and target_env.mode != "off":
        step["state"] = "blocked"
        step["detail"] = f"'{parsed.get('tag')}' is a mutable tag — it cannot be promoted. Build a versioned tag."
        return step
    left = core.soak_left(record, source_env)
    if left > 0 and target_env.mode != "off":
        step["state"] = "soaking"
        step["soakMinutesLeft"] = left
        step["detail"] = f"Soaking in {source_env.name} — {core.format_minutes(left)} left."
        return step
    step["state"] = "ready"
    step["detail"] = (
        f"{parsed.get('tag')} passed {source_env.name}; {target_env.name} runs "
        + ", ".join(sorted({w["tag"] for w in behind}))
        + "."
    )
    return step


def image_history(repository: str) -> List[Dict[str, Any]]:
    """Every tag of one repository the ledger knows, and where it passed."""
    rungs = {env.id: env for env in core.ladder()}
    rows = PromotionRecord.query.filter_by(repository=repository).all()
    by_image: Dict[str, Dict[str, Any]] = {}
    for row in rows:
        env = rungs.get(row.environment_id)
        if env is None:
            continue
        entry = by_image.setdefault(row.image, {"image": row.image, "tag": row.tag, "environments": []})
        entry["environments"].append(
            {
                "environmentId": env.id,
                "name": env.name,
                "position": env.position,
                "firstHealthyAt": core._iso(row.first_healthy_at),
                "lastSeenAt": core._iso(row.last_seen_at),
                "where": f"{row.namespace}/{row.workload_name}" if row.workload_name else None,
                "source": row.source,
                "deployedBy": row.deployed_by,
            }
        )
    out = list(by_image.values())
    for entry in out:
        entry["environments"].sort(key=lambda e: e["position"])
        entry["firstSeenAt"] = min((e["firstHealthyAt"] or "") for e in entry["environments"])
    out.sort(key=lambda e: e["firstSeenAt"] or "", reverse=True)
    return out[:60]
