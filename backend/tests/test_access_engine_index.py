"""The indexed, memoised rule evaluation answers exactly as the plain scan did.

``evaluate_access`` used to match every rule of the user with ``_rule_matches``
and sort the matches by ``rule_specificity`` on every call. It now buckets the
rules by cluster once per request and memoises answers. The reference below is
the old algorithm, verbatim, and both are asked the same randomised questions.
"""

from __future__ import annotations

import itertools
import random
from types import SimpleNamespace as NS

import pytest
from flask import Flask, g

from api import access_engine as ae


def _reference(rules, *, cluster_id, permission_key, namespace=None, resource_type=None,
               resource_name=None, container_name=None, port=None):
    scoped = ae._rules_for_evaluation(
        rules, cluster_id=cluster_id, namespace=namespace, resource_type=resource_type
    )
    matching = [
        r
        for r in scoped
        if ae._rule_matches(
            r,
            cluster_id=cluster_id,
            namespace=namespace,
            resource_type=resource_type,
            resource_name=resource_name,
            container_name=container_name,
            port=port,
            permission_key=permission_key,
        )
    ]
    if not matching:
        return False
    matching.sort(key=ae.rule_specificity, reverse=True)
    top_score = ae.rule_specificity(matching[0])
    top = [r for r in matching if ae.rule_specificity(r) == top_score]
    if any(r.effect == "deny" for r in top):
        return False
    return any(r.effect == "allow" for r in top)


CLUSTERS = ["c1", "c2"]
NAMESPACES = [None, "", "ns1", " ns1 ", "ns2"]
TYPES = [None, "cluster", "namespace", "pod", "deployment", "service", "container", "service_port"]
NAMES = [None, "", "web", "web-7d9-abc", "api"]
CONTAINERS = [None, "app", "sidecar"]
PORTS = [None, 80, 443]
PERMS = ["clusters:view", "namespaces:view", "resources:view", "pods:view", "logs:view",
         "deployments:view", "services:view", "services:ports:view", "alerts:view"]


def _random_rule(rng):
    return NS(
        cluster_id=rng.choice(CLUSTERS),
        namespace=rng.choice(NAMESPACES),
        resource_type=rng.choice(TYPES),
        resource_name=rng.choice(NAMES),
        container_name=rng.choice(CONTAINERS),
        port=rng.choice(PORTS),
        permission_key=rng.choice(PERMS),
        effect=rng.choice(["allow", "allow", "deny"]),
    )


@pytest.mark.parametrize("seed", range(40))
def test_indexed_evaluation_matches_the_plain_scan(seed):
    rng = random.Random(seed)
    rules = [_random_rule(rng) for _ in range(rng.randint(1, 25))]
    index = ae._RuleIndex(rules)
    questions = [
        dict(
            cluster_id=rng.choice(CLUSTERS + ["c3"]),
            permission_key=rng.choice(PERMS),
            namespace=rng.choice(NAMESPACES),
            resource_type=rng.choice(TYPES),
            resource_name=rng.choice(NAMES),
            container_name=rng.choice(CONTAINERS),
            port=rng.choice(PORTS),
        )
        for _ in range(150)
    ]
    # Twice: the second pass is answered from the memo.
    for question in itertools.chain(questions, questions):
        assert ae._evaluate_rules(index, **question) == _reference(rules, **question), question


@pytest.mark.parametrize("seed", range(10))
def test_scoped_namespaces_match_the_plain_scan(seed):
    rng = random.Random(1000 + seed)
    rules = [_random_rule(rng) for _ in range(rng.randint(1, 30))]
    index = ae._RuleIndex(rules)
    for cluster_id in CLUSTERS + ["c3"]:
        expected = {
            (rule.namespace or "").strip()
            for rule in rules
            if rule.cluster_id == cluster_id
            and rule.effect == "allow"
            and rule.permission_key in ("namespaces:view", "resources:view")
            and (rule.resource_type or "cluster") in ("namespace", "pod", "deployment", "service")
            and (rule.namespace or "").strip()
        }
        assert ae._scoped_namespaces(index, cluster_id) == expected


def _viewer(rules):
    role = NS(name="viewer", permissions=[NS(key=k) for k in PERMS])
    return NS(id=7, role=role, cluster_access_entries=[], namespace_access_entries=[]), rules


def test_answers_are_memoised_per_request_and_dropped_on_invalidation():
    app = Flask("memo")
    user, rules = _viewer(
        [NS(cluster_id="c1", namespace="ns1", resource_type="namespace", resource_name=None,
            container_name=None, port=None, permission_key="logs:view", effect="allow")]
    )
    with app.test_request_context("/"):
        g._access_rules_by_user_id = {user.id: rules}
        assert ae.can_view_logs(user, "c1", "ns1", "pod-a")
        index = g._access_rule_index_by_user_id[user.id]
        assert index.answers
        calls = []
        original = ae._prepared_matches
        try:
            ae._prepared_matches = lambda *a, **k: calls.append(1) or original(*a, **k)
            assert ae.can_view_logs(user, "c1", "ns1", "pod-a")
        finally:
            ae._prepared_matches = original
        assert calls == []

        ae.invalidate_access_rules_cache(user.id)
        assert user.id not in g._access_rule_index_by_user_id
        # A rule change after invalidation is seen.
        rules[0].effect = "deny"
        g._access_rules_by_user_id = {user.id: list(rules)}
        assert not ae.can_view_logs(user, "c1", "ns1", "pod-a")
