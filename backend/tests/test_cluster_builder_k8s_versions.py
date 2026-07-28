"""Kubernetes version policy, upstream patch discovery, and the version-sensitive
parts of the creation stack.

No test here touches the network: ``k8s_versions._http_get_text`` is the single
outbound call in the provider and every test that exercises discovery replaces
it with a fake. ``_network_disabled`` is patched off in the same place, because
it suppresses outbound calls under Flask TESTING by default.
"""

from __future__ import annotations

import hashlib
import urllib.error

import pytest
import yaml

from api.db import db
from api.models import BuildProfile, ClusterBuild, ClusterBuildNode
from api.services import ssh_profile_service
from api.services.cluster_build import k8s_versions
from api.services.cluster_build import kubeadm as kubeadm_mod
from api.services.cluster_build import lb as lb_mod
from api.services.cluster_build import preflight as preflight_mod
from api.services.cluster_build import service as build_service
from api.services.cluster_build import os_adapters
from api.services.cluster_build import cni as cni_registry
from api.services.cluster_build.addons.metrics_server import METRICS_SERVER
from api.services.cluster_build.cni.calico import CALICO
from api.services.cluster_build.profiles import default_profile, resolve


def auth_headers(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


ENABLED_MINORS = k8s_versions.enabled_minors()


@pytest.fixture(autouse=True)
def clean_discovery_cache():
    """Discovery state is process-global; no test may inherit another's."""
    k8s_versions.reset_cache()
    yield
    k8s_versions.reset_cache()


@pytest.fixture()
def discovery(monkeypatch):
    """Enable discovery with a scripted, offline transport.

    Returns a recorder whose ``responses`` maps a minor to either a body string
    or an exception instance to raise, and whose ``calls`` lists the minors
    actually fetched — which is how cache hits are observed.
    """

    class _Discovery:
        def __init__(self):
            self.responses: dict = {}
            self.calls: list = []
            self.default = None  # None ⇒ raise 404 for unscripted minors

        def _get(self, url: str, timeout: float) -> str:
            assert url.startswith("https://dl.k8s.io/release/stable-")
            assert url.endswith(".txt")
            assert timeout == k8s_versions.HTTP_TIMEOUT_S
            minor = url.rsplit("stable-", 1)[1][: -len(".txt")]
            self.calls.append(minor)
            outcome = self.responses.get(minor, self.default)
            if outcome is None:
                raise urllib.error.HTTPError(url, 404, "Not Found", {}, None)
            if isinstance(outcome, BaseException):
                raise outcome
            return outcome

    recorder = _Discovery()
    monkeypatch.setattr(k8s_versions, "_network_disabled", lambda: False)
    monkeypatch.setattr(k8s_versions, "_http_get_text", recorder._get)
    return recorder


def _all_minors_respond(discovery, patch: int = 99) -> None:
    for minor in ENABLED_MINORS:
        discovery.responses[minor] = f"v{minor}.{patch}\n"


# ---------------------------------------------------------------------------
# Version grammar and policy
# ---------------------------------------------------------------------------

class TestVersionPolicy:
    def test_leading_v_is_normalized_away(self):
        assert k8s_versions.normalize_version("v1.32.4") == "1.32.4"
        assert k8s_versions.normalize_version(" 1.32.4 ") == "1.32.4"
        assert k8s_versions.normalize_version("V1.32.4") == ""

    @pytest.mark.parametrize(
        "value",
        [
            "1.32.4-rc.1", "v1.33.0-alpha.2", "1.32.0-beta.0",
            "1.32", "1.32.4.1", "", "latest", "v1.32.x", "1.32.4+build",
        ],
    )
    def test_malformed_and_prerelease_versions_are_rejected(self, value):
        assert k8s_versions.parse_version(value) is None
        with pytest.raises(ValueError):
            k8s_versions.validate_version(value)

    def test_semantic_not_lexical_ordering(self):
        # "1.9.0" beats "1.32.4" as a string; it must not here.
        assert k8s_versions.sort_versions(
            ["1.9.0", "v1.32.4", "1.31.8", "1.32.10", "1.30.12"]
        ) == ["1.32.10", "1.32.4", "1.31.8", "1.30.12", "1.9.0"]

    def test_unsupported_minor_is_rejected_even_when_well_formed(self):
        with pytest.raises(ValueError) as excinfo:
            k8s_versions.validate_version("1.12.0")
        assert "not supported" in str(excinfo.value)

    def test_discovered_upstream_does_not_imply_supported(self):
        """A minor upstream ships is not automatically selectable here."""
        for record in k8s_versions.SUPPORTED_MINORS:
            if record.enabled:
                continue
            assert record.blockers, (
                f"{record.minor} is disabled but records no blocker; a reader "
                "cannot tell what would unblock it."
            )
            with pytest.raises(ValueError):
                k8s_versions.validate_version(record.fallback_patch)

    def test_any_patch_of_an_enabled_minor_is_accepted(self):
        """Pinning is patch-exact but policy is minor-scoped, so a draft does
        not become invalid the day a newer patch is published."""
        for minor in ENABLED_MINORS:
            assert k8s_versions.validate_version(f"{minor}.0") == f"{minor}.0"
            assert k8s_versions.validate_version(f"v{minor}.999") == f"{minor}.999"

    def test_static_fallback_covers_every_enabled_minor(self):
        assert k8s_versions.STATIC_FALLBACK_VERSIONS
        assert build_service.SUPPORTED_K8S_VERSIONS == \
            k8s_versions.STATIC_FALLBACK_VERSIONS
        covered = {k8s_versions.minor_of(v)
                   for v in k8s_versions.STATIC_FALLBACK_VERSIONS}
        assert covered == set(ENABLED_MINORS)
        for version in k8s_versions.STATIC_FALLBACK_VERSIONS:
            assert k8s_versions.validate_version(version) == version


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------

class TestDiscovery:
    def test_discovers_the_latest_patch_for_every_supported_minor(self, discovery):
        _all_minors_respond(discovery, patch=77)
        versions = k8s_versions.supported_versions()

        assert versions == k8s_versions.sort_versions(
            f"{minor}.77" for minor in ENABLED_MINORS
        )
        assert sorted(discovery.calls) == sorted(ENABLED_MINORS)

    def test_results_are_sorted_newest_first_semantically(self, discovery):
        for index, minor in enumerate(ENABLED_MINORS):
            # Deliberately give the oldest minor the largest patch number, so a
            # lexical sort would put it first.
            discovery.responses[minor] = f"v{minor}.{9 - index}"
        versions = k8s_versions.supported_versions()
        parsed = [k8s_versions.parse_version(v) for v in versions]
        assert parsed == sorted(parsed, reverse=True)

    def test_leading_v_is_stripped_from_upstream_payloads(self, discovery):
        _all_minors_respond(discovery, patch=12)
        for version in k8s_versions.supported_versions():
            assert not version.startswith("v")

    def test_a_new_patch_appears_after_the_cache_refreshes(self, discovery):
        _all_minors_respond(discovery, patch=4)
        assert k8s_versions.supported_versions()[0].endswith(".4")

        # Upstream publishes a newer patch; nothing in the source tree changes.
        _all_minors_respond(discovery, patch=5)
        assert k8s_versions.supported_versions()[0].endswith(".4"), \
            "a fresh cache entry must be served without refetching"

        k8s_versions.reset_cache()
        assert k8s_versions.supported_versions()[0].endswith(".5")

    def test_cache_hit_avoids_a_second_fetch(self, discovery):
        _all_minors_respond(discovery)
        k8s_versions.supported_versions()
        first_round = len(discovery.calls)
        k8s_versions.supported_versions()
        assert len(discovery.calls) == first_round

    @pytest.mark.parametrize(
        "outcome",
        [
            TimeoutError("timed out"),
            urllib.error.URLError("connection refused"),
            urllib.error.HTTPError(
                "https://dl.k8s.io", 404, "Not Found", {}, None
            ),
            urllib.error.HTTPError(
                "https://dl.k8s.io", 503, "Service Unavailable", {}, None
            ),
            OSError("network unreachable"),
        ],
        ids=["timeout", "urlerror", "http-404", "http-503", "oserror"],
    )
    def test_every_upstream_failure_falls_back_to_the_static_set(
        self, discovery, outcome
    ):
        for minor in ENABLED_MINORS:
            discovery.responses[minor] = outcome
        assert k8s_versions.supported_versions() == \
            k8s_versions.sort_versions(k8s_versions.STATIC_FALLBACK_VERSIONS)

    @pytest.mark.parametrize(
        "body",
        ["", "   ", "not-a-version", "v1.32", "<html>404</html>", "v1.32.4-rc.0"],
    )
    def test_unusable_response_bodies_fall_back(self, discovery, body):
        for minor in ENABLED_MINORS:
            discovery.responses[minor] = body
        assert k8s_versions.supported_versions() == \
            k8s_versions.sort_versions(k8s_versions.STATIC_FALLBACK_VERSIONS)

    def test_a_payload_naming_a_different_branch_is_ignored(self, discovery):
        """A mis-served file must never smuggle in an unsupported minor."""
        for minor in ENABLED_MINORS:
            discovery.responses[minor] = "v1.99.0"
        versions = k8s_versions.supported_versions()
        assert versions == k8s_versions.sort_versions(
            k8s_versions.STATIC_FALLBACK_VERSIONS
        )
        assert "1.99.0" not in versions

    def test_last_known_good_survives_a_later_outage(self, discovery):
        _all_minors_respond(discovery, patch=41)
        assert k8s_versions.supported_versions()[0].endswith(".41")

        # The cached entry expires, and upstream is now down.
        with k8s_versions._lock:
            k8s_versions._cache.clear()
        for minor in ENABLED_MINORS:
            discovery.responses[minor] = urllib.error.URLError("down")

        served = k8s_versions.supported_versions()
        assert all(version.endswith(".41") for version in served), served
        assert served != list(k8s_versions.STATIC_FALLBACK_VERSIONS)

    def test_partial_outage_mixes_live_and_fallback(self, discovery):
        newest, *rest = ENABLED_MINORS
        discovery.responses[newest] = f"v{newest}.55"
        for minor in rest:
            discovery.responses[minor] = urllib.error.URLError("down")

        entries = {entry["minor"]: entry for entry in k8s_versions.discover_versions()}
        assert entries[newest]["version"] == f"{newest}.55"
        assert entries[newest]["source"] == "upstream"
        for minor in rest:
            assert entries[minor]["source"] == "fallback"

    def test_discovery_never_raises_even_when_the_fetcher_explodes(
        self, monkeypatch
    ):
        monkeypatch.setattr(k8s_versions, "_network_disabled", lambda: False)

        def _boom(url, timeout):
            raise RuntimeError("unexpected transport failure")

        monkeypatch.setattr(k8s_versions, "_http_get_text", _boom)
        with pytest.raises(RuntimeError):
            k8s_versions._http_get_text("x", 1)
        # _fetch_stable_patch only absorbs network-shaped errors, so this
        # escapes to supported_versions(), which must still answer.
        assert k8s_versions.supported_versions() == \
            list(k8s_versions.STATIC_FALLBACK_VERSIONS)

    def test_no_outbound_call_is_attempted_under_testing(self, app, monkeypatch):
        """The default posture inside the app: discovery stays off the network,
        so no unrelated test can depend on upstream being reachable.

        Asserting on *attempts*, not on the result: minors are fetched from a
        thread pool where ``current_app`` is unavailable, and a suppression
        check that only consulted the app context there would read as "network
        allowed" while the returned value still happened to look like the
        fallback.
        """
        attempts = []

        def _record(url, timeout):
            attempts.append(url)
            return "v1.32.4"

        monkeypatch.setattr(k8s_versions, "_http_get_text", _record)
        with app.app_context():
            assert k8s_versions.supported_versions() == \
                list(k8s_versions.STATIC_FALLBACK_VERSIONS)
        assert attempts == [], f"discovery reached the network: {attempts}"

    def test_no_outbound_call_is_attempted_outside_an_app_context(
        self, monkeypatch
    ):
        """Discovery runs on worker threads; the suppression decision is taken
        once on the calling thread and must reach every worker."""
        attempts = []
        monkeypatch.setattr(k8s_versions, "_network_disabled", lambda: True)
        monkeypatch.setattr(
            k8s_versions, "_http_get_text",
            lambda url, timeout: attempts.append(url) or "v1.32.4",
        )
        assert k8s_versions.supported_versions() == \
            list(k8s_versions.STATIC_FALLBACK_VERSIONS)
        assert attempts == []


# ---------------------------------------------------------------------------
# Options endpoint contract
# ---------------------------------------------------------------------------

class TestOptionsContract:
    def test_k8s_versions_is_a_plain_string_list(self, client, admin_token):
        response = client.get(
            "/api/cluster-builds/options", headers=auth_headers(admin_token)
        )
        assert response.status_code == 200
        data = response.get_json()["data"]

        versions = data["k8sVersions"]
        assert isinstance(versions, list)
        assert versions, "the wizard cannot open without at least one version"
        assert all(isinstance(version, str) for version in versions)
        assert all(k8s_versions.parse_version(v) is not None for v in versions)
        assert versions == k8s_versions.sort_versions(versions)

    def test_additive_metadata_does_not_displace_existing_fields(
        self, client, admin_token
    ):
        data = client.get(
            "/api/cluster-builds/options", headers=auth_headers(admin_token)
        ).get_json()["data"]
        for field in ("k8sVersions", "cniPlugins", "addons", "osMatrix",
                      "endpointModes", "topologies", "defaults"):
            assert field in data, field
        info = data["k8sVersionInfo"]
        assert info["supportedMinors"] == ENABLED_MINORS
        assert info["staticFallback"] == list(k8s_versions.STATIC_FALLBACK_VERSIONS)
        assert "stable-{minor}.txt" in info["source"]

    def test_one_options_call_fetches_each_minor_at_most_once(
        self, client, admin_token, discovery
    ):
        """The version list and its provenance share a single discovery pass."""
        _all_minors_respond(discovery)
        client.get("/api/cluster-builds/options", headers=auth_headers(admin_token))
        assert sorted(discovery.calls) == sorted(ENABLED_MINORS)

    def test_options_survive_an_upstream_outage(
        self, client, admin_token, discovery
    ):
        for minor in ENABLED_MINORS:
            discovery.responses[minor] = urllib.error.URLError("down")
        response = client.get(
            "/api/cluster-builds/options", headers=auth_headers(admin_token)
        )
        assert response.status_code == 200
        assert response.get_json()["data"]["k8sVersions"] == \
            k8s_versions.sort_versions(k8s_versions.STATIC_FALLBACK_VERSIONS)

    def test_the_error_message_never_leaks_upstream_exception_detail(
        self, client, admin_token, discovery
    ):
        secret = "https://internal-proxy.corp:8080 refused"
        for minor in ENABLED_MINORS:
            discovery.responses[minor] = urllib.error.URLError(secret)
        body = client.get(
            "/api/cluster-builds/options", headers=auth_headers(admin_token)
        ).get_data(as_text=True)
        assert "internal-proxy.corp" not in body


# ---------------------------------------------------------------------------
# Validation agrees with what the wizard offers, and pins stay pinned
# ---------------------------------------------------------------------------

def _payload(name: str, version: str, profile_id: int) -> dict:
    return {
        "name": name,
        "k8sVersion": version,
        "topologyType": "single_cp",
        "endpointMode": "manual_endpoint",
        "controlPlaneEndpoint": "10.0.0.100:6443",
        "cniPlugin": "calico",
        "podCidr": "10.244.0.0/16",
        "serviceCidr": "10.96.0.0/12",
        "connectionProfileId": profile_id,
        "nodes": [],
    }


@pytest.fixture()
def ssh_profile(app):
    cred = ssh_profile_service.create_credential(
        {"name": "root-key", "username": "kubesight", "authMethod": "key",
         "secret": "-----BEGIN KEY-----\nfake\n-----END KEY-----",
         "sudoMode": "nopasswd"},
    )
    return ssh_profile_service.create_profile(
        {"name": "default", "credentialId": cred["id"], "routeMode": "direct",
         "hostKeyPolicy": "tofu"},
    )


class TestValidationMatchesOptions:
    def test_every_offered_version_is_accepted_by_create(
        self, client, admin_token, ssh_profile
    ):
        offered = client.get(
            "/api/cluster-builds/options", headers=auth_headers(admin_token)
        ).get_json()["data"]["k8sVersions"]
        assert offered

        for index, version in enumerate(offered):
            response = client.post(
                "/api/cluster-builds",
                json=_payload(f"offered-{index}", version, ssh_profile["id"]),
                headers=auth_headers(admin_token),
            )
            assert response.status_code == 201, response.get_json()
            assert response.get_json()["data"]["k8sVersion"] == version

    def test_backend_rejects_what_the_wizard_would_never_offer(
        self, client, admin_token, ssh_profile
    ):
        disabled = next(
            record.fallback_patch for record in k8s_versions.SUPPORTED_MINORS
            if not record.enabled
        )
        for version in ("1.12.0", disabled, "1.32.4-rc.1", "latest", "1.32"):
            response = client.post(
                "/api/cluster-builds",
                json=_payload("bad", version, ssh_profile["id"]),
                headers=auth_headers(admin_token),
            )
            assert response.status_code == 400, version

    def test_a_leading_v_is_normalized_on_the_way_in(
        self, client, admin_token, ssh_profile
    ):
        version = k8s_versions.STATIC_FALLBACK_VERSIONS[0]
        response = client.post(
            "/api/cluster-builds",
            json=_payload("with-v", f"v{version}", ssh_profile["id"]),
            headers=auth_headers(admin_token),
        )
        assert response.status_code == 201
        assert response.get_json()["data"]["k8sVersion"] == version


class TestPinning:
    def test_the_exact_patch_survives_a_newer_upstream_release(
        self, client, admin_token, ssh_profile, discovery
    ):
        minor = ENABLED_MINORS[0]
        _all_minors_respond(discovery, patch=4)
        pinned = k8s_versions.supported_versions()[0]
        assert pinned == f"{minor}.4"

        build_id = client.post(
            "/api/cluster-builds",
            json=_payload("pinned", pinned, ssh_profile["id"]),
            headers=auth_headers(admin_token),
        ).get_json()["data"]["id"]

        # Upstream publishes a newer patch and the cache picks it up.
        k8s_versions.reset_cache()
        _all_minors_respond(discovery, patch=9)
        assert k8s_versions.supported_versions()[0] == f"{minor}.9"

        fetched = client.get(
            f"/api/cluster-builds/{build_id}", headers=auth_headers(admin_token)
        ).get_json()["data"]
        assert fetched["k8sVersion"] == pinned

    def test_an_existing_draft_stays_editable_after_a_newer_patch_appears(
        self, client, admin_token, ssh_profile, discovery
    ):
        minor = ENABLED_MINORS[0]
        _all_minors_respond(discovery, patch=4)
        pinned = f"{minor}.4"
        build_id = client.post(
            "/api/cluster-builds",
            json=_payload("draft", pinned, ssh_profile["id"]),
            headers=auth_headers(admin_token),
        ).get_json()["data"]["id"]

        k8s_versions.reset_cache()
        _all_minors_respond(discovery, patch=9)

        payload = _payload("draft-renamed", pinned, ssh_profile["id"])
        response = client.put(
            f"/api/cluster-builds/{build_id}", json=payload,
            headers=auth_headers(admin_token),
        )
        assert response.status_code == 200, response.get_json()
        assert response.get_json()["data"]["k8sVersion"] == pinned

    def test_a_historical_pin_predating_discovery_remains_readable(
        self, client, admin_token, app
    ):
        """Rows written before this change carry a bare patch string; reading
        and serializing them must not consult discovery at all."""
        with app.app_context():
            build = ClusterBuild(
                name="historical", status="draft",
                k8s_version=k8s_versions.STATIC_FALLBACK_VERSIONS[-1],
                cri="containerd", topology_type="single_cp",
                endpoint_mode="manual_endpoint",
                control_plane_endpoint="10.0.0.9:6443",
                cni_plugin="calico", pod_cidr="10.244.0.0/16",
                service_cidr="10.96.0.0/12",
            )
            db.session.add(build)
            db.session.commit()
            build_id = build.id

        response = client.get(
            f"/api/cluster-builds/{build_id}", headers=auth_headers(admin_token)
        )
        assert response.status_code == 200
        assert response.get_json()["data"]["k8sVersion"] == \
            k8s_versions.STATIC_FALLBACK_VERSIONS[-1]


# ---------------------------------------------------------------------------
# Version-sensitive provisioning logic
# ---------------------------------------------------------------------------

class TestPauseImage:
    @pytest.mark.parametrize("minor", ENABLED_MINORS)
    def test_every_enabled_minor_has_an_explicit_tag(self, minor):
        record = k8s_versions._BY_MINOR[minor]
        assert record.pause_image_tag
        assert kubeadm_mod.pause_image_tag(f"{minor}.4") == record.pause_image_tag
        assert kubeadm_mod.pause_image_tag(f"v{minor}.4") == record.pause_image_tag

    def test_tags_match_kubeadms_pinned_pause_versions(self):
        """kubeadm's PauseVersion per release branch. Mirrors and offline
        registries only carry what kubeadm's image list names."""
        expected = {
            "1.29": "3.9", "1.30": "3.9", "1.31": "3.10", "1.32": "3.10",
            "1.33": "3.10", "1.34": "3.10.1", "1.35": "3.10.1", "1.36": "3.10.2",
        }
        for minor, tag in expected.items():
            assert k8s_versions._BY_MINOR[minor].pause_image_tag == tag, minor

    def test_an_unrecorded_minor_raises_instead_of_guessing(self):
        """The old generic fallback would have silently picked a wrong tag."""
        with pytest.raises(ValueError) as excinfo:
            kubeadm_mod.pause_image_tag("1.99.0")
        assert "pause image tag" in str(excinfo.value)

    @pytest.mark.parametrize("minor", ENABLED_MINORS)
    def test_containerd_sandbox_image_uses_the_recorded_tag(self, minor):
        script = os_adapters.base.containerd_config_script(
            os_adapters.ScriptContext(
                profile=default_profile(), k8s_version=f"{minor}.4"
            )
        )
        tag = k8s_versions._BY_MINOR[minor].pause_image_tag
        assert f"registry.k8s.io/pause:{tag}" in script


class TestKubeadmConfigApi:
    def test_the_v1beta4_boundary_is_kubeadm_1_31(self):
        # v1beta4 exists from kubeadm 1.31; older minors must stay on v1beta3.
        assert kubeadm_mod.config_api_version("1.30.12") == \
            k8s_versions.KUBEADM_API_V1BETA3
        assert kubeadm_mod.config_api_version("1.29.15") == \
            k8s_versions.KUBEADM_API_V1BETA3
        assert kubeadm_mod.config_api_version("1.31.8") == \
            k8s_versions.KUBEADM_API_V1BETA4
        assert kubeadm_mod.config_api_version("1.32.4") == \
            k8s_versions.KUBEADM_API_V1BETA4

    @pytest.mark.parametrize("minor", ENABLED_MINORS)
    def test_the_whole_document_is_schema_valid_not_just_the_apiversion(
        self, minor
    ):
        rendered = kubeadm_mod.render_init_config(
            k8s_version=f"{minor}.4",
            control_plane_endpoint="10.0.0.100:6443",
            pod_cidr="10.244.0.0/16",
            service_cidr="10.96.0.0/12",
            profile=default_profile(),
            node_name="cp-1",
            server_tls_bootstrap=True,
        )
        documents = {
            doc["kind"]: doc for doc in yaml.safe_load_all(rendered) if doc
        }
        expected_api = k8s_versions._BY_MINOR[minor].kubeadm_config_api

        assert set(documents) == {
            "InitConfiguration", "ClusterConfiguration", "KubeletConfiguration"
        }
        assert documents["InitConfiguration"]["apiVersion"] == expected_api
        assert documents["ClusterConfiguration"]["apiVersion"] == expected_api
        # KubeletConfiguration is a different API group and is unaffected.
        assert documents["KubeletConfiguration"]["apiVersion"] == \
            "kubelet.config.k8s.io/v1beta1"
        assert documents["KubeletConfiguration"]["cgroupDriver"] == "systemd"
        assert documents["KubeletConfiguration"]["serverTLSBootstrap"] is True

        registration = documents["InitConfiguration"]["nodeRegistration"]
        assert registration["name"] == "cp-1"
        assert registration["criSocket"] == kubeadm_mod.CRI_SOCKET

        cluster = documents["ClusterConfiguration"]
        assert cluster["kubernetesVersion"] == f"v{minor}.4"
        assert cluster["controlPlaneEndpoint"] == "10.0.0.100:6443"
        assert cluster["networking"]["podSubnet"] == "10.244.0.0/16"
        assert cluster["networking"]["serviceSubnet"] == "10.96.0.0/12"
        assert cluster["apiServer"]["certSANs"] == ["10.0.0.100"]
        # Removed in v1beta4; never emitted for either API version.
        assert "timeoutForControlPlane" not in cluster["apiServer"]

    def test_a_mirror_registry_reaches_the_config_for_every_minor(self):
        profile = default_profile().__class__(
            **{**default_profile().__dict__,
               "k8s_image_registry": "nexus.corp:5000/kubernetes"}
        )
        for minor in ENABLED_MINORS:
            cluster = next(
                doc for doc in yaml.safe_load_all(
                    kubeadm_mod.render_init_config(
                        k8s_version=f"{minor}.4",
                        control_plane_endpoint="10.0.0.100:6443",
                        pod_cidr="10.244.0.0/16",
                        service_cidr="10.96.0.0/12",
                        profile=profile,
                        node_name="cp-1",
                    )
                )
                if doc and doc.get("kind") == "ClusterConfiguration"
            )
            assert cluster["imageRepository"] == "nexus.corp:5000/kubernetes"


class TestCreationScripts:
    """One single-control-plane and one HA rendering per enabled minor."""

    @staticmethod
    def _prep_scripts(minor: str) -> str:
        ctx = os_adapters.ScriptContext(
            profile=default_profile(), k8s_version=f"{minor}.4"
        )
        return "\n".join(
            "\n".join([
                adapter.script_kernel_prep(ctx),
                adapter.script_install_containerd(ctx),
                adapter.script_install_kube_packages(ctx),
            ])
            for adapter in os_adapters.ADAPTERS
        )

    @pytest.mark.parametrize("minor", ENABLED_MINORS)
    def test_single_control_plane_creation(self, minor):
        version = f"{minor}.4"
        scripts = self._prep_scripts(minor)
        # Exact patch pin per OS family, and the minor-scoped repository URL.
        assert f'K8S_VER="{version}"' in scripts
        assert f"core:/stable:/v{minor}/deb/" in scripts
        assert f"core:/stable:/v{minor}/rpm/" in scripts
        assert 'kubelet="${K8S_VER}-*"' in scripts
        assert '"kubelet-${K8S_VER}"' in scripts
        tag = k8s_versions._BY_MINOR[minor].pause_image_tag
        assert f"pause:{tag}" in scripts

        config = kubeadm_mod.render_init_config(
            k8s_version=version, control_plane_endpoint="10.0.0.100:6443",
            pod_cidr="10.244.0.0/16", service_cidr="10.96.0.0/12",
            profile=default_profile(), node_name="cp-1",
        )
        cluster = next(
            doc for doc in yaml.safe_load_all(config)
            if doc and doc["kind"] == "ClusterConfiguration"
        )
        assert cluster["kubernetesVersion"] == f"v{version}"
        # A single control plane needs no cert key: nothing joins the CP tier.
        artifacts = kubeadm_mod.InitArtifacts(
            token="abcdef.0123456789abcdef",
            ca_cert_hash="sha256:" + "a" * 64,
        )
        assert kubeadm_mod.validate_init_artifacts(
            artifacts, need_certificate_key=False
        ) is None
        assert artifacts.worker_join_command("10.0.0.100:6443").startswith(
            "kubeadm join 10.0.0.100:6443 --token "
        )

    @pytest.mark.parametrize("minor", ENABLED_MINORS)
    def test_stacked_ha_creation(self, minor):
        version = f"{minor}.4"
        assert f'K8S_VER="{version}"' in self._prep_scripts(minor)

        config = kubeadm_mod.render_init_config(
            k8s_version=version, control_plane_endpoint="10.0.0.50:6443",
            pod_cidr="10.244.0.0/16", service_cidr="10.96.0.0/12",
            profile=default_profile(), node_name="cp-1",
        )
        cluster = next(
            doc for doc in yaml.safe_load_all(config)
            if doc and doc["kind"] == "ClusterConfiguration"
        )
        # The VIP must be baked into the certificate SANs before init runs.
        assert cluster["controlPlaneEndpoint"] == "10.0.0.50:6443"
        assert cluster["apiServer"]["certSANs"] == ["10.0.0.50"]

        artifacts = kubeadm_mod.InitArtifacts(
            token="abcdef.0123456789abcdef",
            ca_cert_hash="sha256:" + "a" * 64,
            certificate_key="b" * 64,
        )
        assert kubeadm_mod.validate_init_artifacts(
            artifacts, need_certificate_key=True
        ) is None
        join = artifacts.control_plane_join_command("10.0.0.50:6443")
        assert "--control-plane --certificate-key" in join
        # Missing the key is a hard error for HA, never a silent worker join.
        assert kubeadm_mod.validate_init_artifacts(
            kubeadm_mod.InitArtifacts(
                token=artifacts.token, ca_cert_hash=artifacts.ca_cert_hash
            ),
            need_certificate_key=True,
        ) is not None

        haproxy = lb_mod.render_haproxy_cfg(
            [("cp-1", "10.0.0.11"), ("cp-2", "10.0.0.12"), ("cp-3", "10.0.0.13")]
        )
        for address in ("10.0.0.11", "10.0.0.12", "10.0.0.13"):
            assert f"{address}:6443" in haproxy


# ---------------------------------------------------------------------------
# Per-version CNI / add-on compatibility
# ---------------------------------------------------------------------------

class TestCompatibilityMatrix:
    def test_every_enabled_minor_has_a_cni_and_is_fully_pinned(self):
        """The gate that makes enabling a minor safe: nothing may be exposed
        without a digest-pinned CNI release validated on it."""
        for minor in ENABLED_MINORS:
            usable = [
                descriptor for descriptor in cni_registry.available()
                if descriptor.versions_for_k8s_minor(minor)
            ]
            assert usable, f"Kubernetes {minor} is enabled with no usable CNI"
            for descriptor in usable:
                for version in descriptor.versions_for_k8s_minor(minor):
                    digests = descriptor.manifest_sha256.get(version, ())
                    if descriptor.manifest_urls:
                        assert len(digests) == len(descriptor.manifest_files), (
                            f"{descriptor.id} {version} is not digest-pinned"
                        )

    def test_disabled_minors_are_never_offered_a_version(self):
        for record in k8s_versions.SUPPORTED_MINORS:
            if record.enabled:
                continue
            for descriptor in cni_registry.available():
                assert descriptor.versions_for_k8s_minor(record.minor) == [] or \
                    record.blockers, record.minor

    def test_calico_releases_cover_disjoint_kubernetes_windows(self):
        assert CALICO.versions_for_k8s_minor("1.36") == ["3.32.1"]
        assert CALICO.versions_for_k8s_minor("1.34") == ["3.32.1"]
        assert CALICO.versions_for_k8s_minor("1.32") == ["3.28.2", "3.27.4"]
        # 3.32 dropped the old minors and 3.28 never had the new ones.
        assert not CALICO.supports_k8s_minor("3.32.1", "1.29")
        assert not CALICO.supports_k8s_minor("3.28.2", "1.36")

    def test_metrics_server_releases_split_at_1_34(self):
        assert METRICS_SERVER.versions_for_k8s_minor("1.36") == ["0.9.0"]
        assert METRICS_SERVER.versions_for_k8s_minor("1.32") == ["0.7.2"]
        assert METRICS_SERVER.digests_for("0.9.0") != \
            METRICS_SERVER.digests_for("0.7.2")

    @pytest.mark.parametrize("minor", ENABLED_MINORS)
    def test_the_vendored_manifests_exist_for_every_enabled_minor(self, minor):
        """Offline builds need the bytes on disk, not just a pinned URL."""
        version = CALICO.default_version_for_k8s_minor(minor)
        assert version
        for filename in CALICO.manifest_files:
            assert CALICO.bundled_path(version, filename).is_file(), (
                f"calico {version} is not vendored for Kubernetes {minor}"
            )

    def test_calico_3_32_1_matches_its_pinned_digest(self):
        """The digest is computed from the vendored bytes, so a corrupted or
        swapped manifest fails here rather than on a live cluster."""
        path = CALICO.bundled_path("3.32.1", "calico.yaml")
        actual = hashlib.sha256(path.read_bytes()).hexdigest()
        assert actual == CALICO.manifest_sha256["3.32.1"][0]

    def test_calico_3_32_1_renders_with_the_build_pod_cidr(self):
        rendered = CALICO.render("3.32.1", "10.99.0.0/16", default_profile())
        blob = "\n".join(rendered)
        assert 'value: "10.99.0.0/16"' in blob
        assert "192.168.0.0/16" not in blob
        documents = [doc for doc in yaml.safe_load_all(blob) if doc]
        assert len(documents) > 1
        # The readiness gate the executor waits on must exist in the manifest.
        namespace, daemonset = CALICO.readiness_daemonset
        assert any(
            doc.get("kind") == "DaemonSet"
            and doc.get("metadata", {}).get("name") == daemonset
            for doc in documents
        )
        assert all("v3.32.1" in image for image in CALICO.required_images(
            "3.32.1", default_profile()
        ))

    def test_the_catalog_publishes_the_matrix_for_the_wizard(
        self, client, admin_token
    ):
        data = client.get(
            "/api/cluster-builds/options", headers=auth_headers(admin_token)
        ).get_json()["data"]

        calico = next(p for p in data["cniPlugins"] if p["id"] == "calico")
        assert calico["k8sMinorsByVersion"]["3.32.1"] == ["1.34", "1.35", "1.36"]
        assert "1.36" not in calico["k8sMinorsByVersion"]["3.28.2"]

        metrics = next(a for a in data["addons"] if a["id"] == "metrics-server")
        assert metrics["k8sMinorsByVersion"]["0.9.0"] == ["1.34", "1.35", "1.36"]
        # Every offered Kubernetes version must have a usable CNI in the payload.
        for version in data["k8sVersions"]:
            minor = ".".join(version.split(".")[:2])
            assert any(
                any(minor in minors
                    for minors in plugin["k8sMinorsByVersion"].values())
                for plugin in data["cniPlugins"]
            ), version


class TestVersionAwareSelection:
    def test_the_default_cni_version_follows_the_kubernetes_version(
        self, client, admin_token, ssh_profile, app
    ):
        for minor, expected in (("1.36", "3.32.1"), ("1.32", "3.28.2")):
            created = client.post(
                "/api/cluster-builds",
                json=_payload(f"cni-{minor}", f"{minor}.4", ssh_profile["id"]),
                headers=auth_headers(admin_token),
            )
            assert created.status_code == 201, created.get_json()
            with app.app_context():
                row = db.session.get(ClusterBuild, created.get_json()["data"]["id"])
                assert row.cni_version == expected, minor

    def test_an_incompatible_cni_version_is_rejected_at_create(
        self, client, admin_token, ssh_profile
    ):
        payload = _payload("bad-cni", "1.36.0", ssh_profile["id"])
        payload["cniVersion"] = "3.28.2"
        response = client.post(
            "/api/cluster-builds", json=payload,
            headers=auth_headers(admin_token),
        )
        assert response.status_code == 400
        error = response.get_json()["error"]
        assert "3.28.2" in error and "1.36" in error and "3.32.1" in error

    def test_the_default_addon_version_follows_the_kubernetes_version(
        self, client, admin_token, ssh_profile
    ):
        for minor, expected in (("1.36", "0.9.0"), ("1.32", "0.7.2")):
            payload = _payload(f"addon-{minor}", f"{minor}.4", ssh_profile["id"])
            payload["addons"] = ["metrics-server"]
            response = client.post(
                "/api/cluster-builds", json=payload,
                headers=auth_headers(admin_token),
            )
            assert response.status_code == 201, response.get_json()
            assert response.get_json()["data"]["addons"] == [
                {"id": "metrics-server", "version": expected}
            ], minor

    def test_an_incompatible_addon_version_is_rejected_at_create(
        self, client, admin_token, ssh_profile
    ):
        payload = _payload("bad-addon", "1.36.0", ssh_profile["id"])
        payload["addons"] = [{"id": "metrics-server", "version": "0.7.2"}]
        response = client.post(
            "/api/cluster-builds", json=payload,
            headers=auth_headers(admin_token),
        )
        assert response.status_code == 400
        assert "0.9.0" in response.get_json()["error"]

    def test_changing_a_drafts_kubernetes_version_realigns_its_cni(
        self, client, admin_token, ssh_profile, app
    ):
        """Editing 1.32 → 1.36 must not leave the draft on a Calico that
        cannot serve 1.36."""
        created = client.post(
            "/api/cluster-builds",
            json=_payload("realign", "1.32.4", ssh_profile["id"]),
            headers=auth_headers(admin_token),
        ).get_json()["data"]
        with app.app_context():
            assert db.session.get(
                ClusterBuild, created["id"]
            ).cni_version == "3.28.2"

        response = client.put(
            f"/api/cluster-builds/{created['id']}",
            json=_payload("realign", "1.36.0", ssh_profile["id"]),
            headers=auth_headers(admin_token),
        )
        assert response.status_code == 200, response.get_json()
        with app.app_context():
            assert db.session.get(
                ClusterBuild, created["id"]
            ).cni_version == "3.32.1"


# ---------------------------------------------------------------------------
# Preflight gates the version before provisioning
# ---------------------------------------------------------------------------

def _build_for(minor: str, **overrides) -> ClusterBuild:
    fields = {
        "name": "vcheck",
        "k8s_version": f"{minor}.4",
        "topology_type": "single_cp",
        "endpoint_mode": "manual_endpoint",
        "control_plane_endpoint": "10.0.0.100:6443",
        "cni_plugin": "calico",
        # The Calico release validated on *this* minor — 3.32.1 covers
        # 1.34-1.36 and 3.28.2 the older ones, so there is no single default.
        "cni_version": CALICO.default_version_for_k8s_minor(minor),
        "pod_cidr": "10.244.0.0/16",
        "service_cidr": "10.96.0.0/12",
    }
    fields.update(overrides)
    return ClusterBuild(**fields)


def _statuses(checks) -> dict:
    return {check["id"]: check["status"] for check in checks}


class TestPreflightVersionGate:
    @pytest.mark.parametrize("minor", ENABLED_MINORS)
    def test_a_supported_minor_passes_every_version_check(self, app, minor):
        with app.app_context():
            checks = preflight_mod.version_checks(
                _build_for(minor), default_profile()
            )
        statuses = _statuses(checks)
        assert statuses["k8s_version"] == "pass"
        assert statuses["k8s_kubeadm_config"] == "pass"
        assert statuses["k8s_cni_support"] == "pass"

    def test_an_unsupported_minor_fails_before_any_node_is_touched(self, app):
        disabled = next(
            record for record in k8s_versions.SUPPORTED_MINORS
            if not record.enabled
        )
        with app.app_context():
            checks = preflight_mod.version_checks(
                _build_for(disabled.minor), default_profile()
            )
        assert _statuses(checks)["k8s_version"] == "fail"
        detail = checks[0]["detail"]
        assert disabled.minor in detail
        # The recorded blocker is surfaced, not a bare refusal.
        assert any(blocker[:20] in detail for blocker in disabled.blockers)

    def test_a_cni_version_that_does_not_cover_the_minor_fails(self, app):
        """Real pairing, no patching: Calico 3.28.2 cannot serve 1.36, and
        3.32.1 cannot serve 1.29. Each must be refused on the other's minor."""
        cases = [("1.36", "3.28.2"), (ENABLED_MINORS[-1], "3.32.1")]
        for minor, cni_version in cases:
            with app.app_context():
                checks = preflight_mod.version_checks(
                    _build_for(minor, cni_version=cni_version), default_profile()
                )
            statuses = _statuses(checks)
            assert statuses["k8s_cni_support"] == "fail", (minor, cni_version)
            detail = next(
                c for c in checks if c["id"] == "k8s_cni_support"
            )["detail"]
            assert cni_version in detail and minor in detail

    def test_an_addon_version_that_does_not_cover_the_minor_fails(self, app):
        # Metrics Server 0.9.0 serves 1.34+; 0.7.2 serves the older minors.
        with app.app_context():
            assert _statuses(preflight_mod.version_checks(
                _build_for(
                    "1.36",
                    addons_json=[{"id": "metrics-server", "version": "0.9.0"}],
                ),
                default_profile(),
            ))["k8s_addon_support"] == "pass"

            assert _statuses(preflight_mod.version_checks(
                _build_for(
                    "1.36",
                    addons_json=[{"id": "metrics-server", "version": "0.7.2"}],
                ),
                default_profile(),
            ))["k8s_addon_support"] == "fail"

            assert _statuses(preflight_mod.version_checks(
                _build_for(
                    ENABLED_MINORS[-1],
                    addons_json=[{"id": "metrics-server", "version": "0.9.0"}],
                ),
                default_profile(),
            ))["k8s_addon_support"] == "fail"

    def test_offline_mode_requires_the_vendored_manifests(self, app, tmp_path):
        minor = ENABLED_MINORS[0]
        with app.app_context():
            row = BuildProfile(
                name="air-gapped", repo_mode="offline",
                offline_bundle_path="/srv/kubesight/does-not-exist.tar",
            )
            db.session.add(row)
            db.session.commit()

            # Calico 3.28.2 is vendored in this repo, so only the declared
            # bundle path is missing on this host.
            offline = next(
                c for c in preflight_mod.version_checks(
                    _build_for(minor), resolve(row)
                )
                if c["id"] == "k8s_offline_bundle"
            )
            assert offline["status"] == "fail"
            assert "does-not-exist.tar" in offline["detail"]

            # A CNI version that was never vendored is named explicitly.
            bundle = tmp_path / "bundle.tar"
            bundle.write_bytes(b"")
            row.offline_bundle_path = str(bundle)
            db.session.commit()
            offline = next(
                c for c in preflight_mod.version_checks(
                    _build_for(minor, cni_version="9.9.9"), resolve(row)
                )
                if c["id"] == "k8s_offline_bundle"
            )
            assert offline["status"] == "fail"
            assert "9.9.9" in offline["detail"]

            # Everything this build needs is vendored → pass.
            offline = next(
                c for c in preflight_mod.version_checks(
                    _build_for(
                        minor, cni_version="3.27.4",
                        addons_json=[{"id": "metrics-server",
                                      "version": "0.7.2"}],
                    ),
                    resolve(row),
                )
                if c["id"] == "k8s_offline_bundle"
            )
            assert offline["status"] == "pass", offline["detail"]

    def test_online_and_mirrored_modes_skip_the_offline_artifact_check(
        self, app
    ):
        minor = ENABLED_MINORS[0]
        with app.app_context():
            internet = preflight_mod.version_checks(
                _build_for(minor), default_profile()
            )
            assert "k8s_offline_bundle" not in _statuses(internet)

            row = BuildProfile(
                name="mirror", repo_mode="mirror",
                k8s_pkg_repo_url="https://mirror.corp/k8s/v{minor}/deb/",
                k8s_image_registry="nexus.corp:5000/kubernetes",
            )
            db.session.add(row)
            db.session.commit()
            mirrored = preflight_mod.version_checks(
                _build_for(minor), resolve(row)
            )
            assert "k8s_offline_bundle" not in _statuses(mirrored)
            assert _statuses(mirrored)["k8s_kubeadm_config"] == "pass"


class TestPreflightNodeProbe:
    @pytest.mark.parametrize("minor", ENABLED_MINORS)
    def test_the_probe_targets_the_exact_patch_and_pause_tag(self, app, minor):
        version = f"{minor}.4"
        with app.app_context():
            script = preflight_mod._probe_script(
                _build_for(minor), ClusterBuildNode(role="worker"),
                default_profile(),
            )
        tag = k8s_versions._BY_MINOR[minor].pause_image_tag
        assert f"core:/stable:/v{minor}/deb/Packages" in script
        assert f"grep -q '^Version: {version}-'" in script
        assert f"https://registry.k8s.io/v2/pause/manifests/{tag}" in script
        assert "KS_PKG_EXACT" in script and "KS_IMG_OK" in script

    def test_a_mirror_is_probed_under_its_repository_path(self, app):
        with app.app_context():
            row = BuildProfile(
                name="mirror2", repo_mode="mirror",
                k8s_pkg_repo_url="https://mirror.corp/k8s/v{minor}/deb/",
                k8s_image_registry="nexus.corp:5000/kubernetes",
            )
            db.session.add(row)
            db.session.commit()
            script = preflight_mod._probe_script(
                _build_for(ENABLED_MINORS[0]),
                ClusterBuildNode(role="worker"), resolve(row),
            )
        minor = ENABLED_MINORS[0]
        tag = k8s_versions._BY_MINOR[minor].pause_image_tag
        assert f"https://mirror.corp/k8s/v{minor}/deb/Packages" in script
        assert (
            f"https://nexus.corp:5000/v2/kubernetes/pause/manifests/{tag}"
            in script
        )

    def test_offline_mode_probes_neither_repository_nor_registry(self, app):
        with app.app_context():
            row = BuildProfile(
                name="offline2", repo_mode="offline",
                offline_bundle_path="/srv/kubesight/bundle.tar",
            )
            db.session.add(row)
            db.session.commit()
            script = preflight_mod._probe_script(
                _build_for(ENABLED_MINORS[0]),
                ClusterBuildNode(role="worker"), resolve(row),
            )
        assert "pkgs.k8s.io" not in script
        assert "/manifests/" not in script
        assert 'if [ "0" = "1" ]' in script

    def test_load_balancers_are_not_asked_for_kubernetes_packages(self, app):
        with app.app_context():
            script = preflight_mod._probe_script(
                _build_for(ENABLED_MINORS[0]),
                ClusterBuildNode(role="loadbalancer"), default_profile(),
            )
        assert 'if [ "0" = "1" ]' in script
        assert "/manifests/" not in script

    @pytest.mark.parametrize(
        "state,expected",
        [("ok", "pass"), ("repo_only", "warn"), ("missing", "fail")],
    )
    def test_package_probe_results_become_checks(self, app, state, expected):
        with app.app_context():
            checks = preflight_mod._node_checks(
                _build_for(ENABLED_MINORS[0]),
                ClusterBuildNode(role="worker"),
                {"KS_OS_ID": "ubuntu", "KS_OS_LIKE": "debian",
                 "KS_OS_VERSION": "24.04", "KS_ARCH": "x86_64",
                 "KS_PKG_EXACT": state, "repo_ok": [], "repo_fail": [],
                 "image_ok": [], "image_fail": []},
                default_profile(),
            )
        assert _statuses(checks)["k8s_packages"] == expected

    def test_a_missing_pause_tag_in_the_registry_fails(self, app):
        with app.app_context():
            checks = preflight_mod._node_checks(
                _build_for(ENABLED_MINORS[0]),
                ClusterBuildNode(role="worker"),
                {"KS_OS_ID": "ubuntu", "KS_OS_LIKE": "debian",
                 "KS_OS_VERSION": "24.04", "KS_ARCH": "x86_64",
                 "repo_ok": [], "repo_fail": [], "image_ok": [],
                 "image_fail": ["https://nexus.corp:5000/v2/pause/manifests/3.10"]},
                default_profile(),
            )
        assert _statuses(checks)["k8s_images"] == "fail"
