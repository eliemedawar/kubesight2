"""The build environment catalog: one owner for what a stage may run on.

The behaviour these lock down is mostly about honesty. The catalog's job is not
to always have an answer — this installation genuinely has only a Java 11 image
— but to say so rather than quietly hand a Java 17 project something else and
let the mismatch surface as a runtime error months later.
"""

from __future__ import annotations

import pytest

from api.services.ci import build_environments as be


# ---------------------------------------------------------------------------
# Resolution
# ---------------------------------------------------------------------------

def test_the_catalog_keeps_the_images_the_templates_already_used():
    """This was a refactor, not a change. The same images must come out."""
    assert be.image("java-jdk11").endswith("adoptopenjdk/openjdk11:jdk-11.0.11_9-alpine-slim")
    assert be.image("node-22") == "node:22-alpine"
    assert be.image("python-3.12") == "python:3.12-slim"
    assert be.image("maven-3.9-jdk21").endswith("maven:3.9-eclipse-temurin-21")


def test_an_image_can_be_repointed_by_environment(monkeypatch):
    monkeypatch.setenv("CI_TEMPLATE_NODE_IMAGE", "nexus.example.test/node:20")
    assert be.image("node-22") == "nexus.example.test/node:20"


def test_the_registry_prefix_applies_to_mirrored_images(monkeypatch):
    monkeypatch.setenv("CI_TEMPLATE_IMAGE_REGISTRY", "nexus.example.test")
    assert be.image("java-jdk11").startswith("nexus.example.test/")


def test_an_unknown_key_resolves_to_nothing_rather_than_a_guess():
    assert be.resolve("java-jdk42") is None
    assert be.image("java-jdk42") == ""


def test_an_environment_reports_whether_it_is_actually_configured():
    """Android has no mirrored image here. Reporting that is the difference
    between a warning a person can act on and a pull that fails in-cluster."""
    assert be.resolve("java-jdk11")["configured"] is True
    assert be.resolve("android")["configured"] is False
    # macOS is imageless by nature — an agent runs it — not unconfigured.
    assert be.resolve("macos-xcode")["configured"] is True
    assert be.resolve("macos-xcode")["imageless"] is True


def test_an_environment_carries_the_runner_labels_its_image_implies():
    """Coarse on purpose: the image pins the toolchain, so the runner only has
    to be able to run Linux containers. A 'java11' label would exclude the
    built-in Kubernetes runner for no benefit — a mistake already made once."""
    assert be.resolve("java-jdk11")["labels"] == ["linux", "java"]
    assert "java11" not in be.resolve("java-jdk11")["labels"]
    assert be.resolve("macos-xcode")["runnerType"] == "agent_macos"


# ---------------------------------------------------------------------------
# Matching
# ---------------------------------------------------------------------------

def test_the_build_tool_is_a_filter_not_a_tie_break():
    """A Gradle project must never be offered the Maven image because its JDK
    happens to be closer — that is an image that cannot build the project."""
    key, _ = be.best_for(language="java", language_version="17", build_system="gradle")
    assert key is not None
    assert "maven" not in key

    key, _ = be.best_for(language="java", language_version="11", build_system="maven")
    assert key is not None
    assert "gradle" not in key


def test_an_exact_version_match_is_silent():
    key, warning = be.best_for(language="java", language_version="11", build_system="gradle")
    assert key in ("java-jdk11", "gradle-8-jdk11")
    assert warning == ""


def test_a_version_this_installation_cannot_provide_says_so_plainly():
    """The honest state of this installation today, and the single most useful
    thing the catalog says: a silent downgrade produces a green build that
    compiled against the wrong runtime."""
    key, warning = be.best_for(language="java", language_version="17", build_system="gradle")
    assert key is not None
    assert "targets Java 17" in warning
    assert "CI_TEMPLATE_JDK_IMAGE" in warning


@pytest.mark.parametrize(
    ("language", "expected"),
    [
        ("javascript", "node-22"),
        ("typescript", "node-22"),
        ("python", "python-3.12"),
        ("swift", "macos-xcode"),
        ("dart", "flutter"),
    ],
)
def test_each_language_lands_on_its_environment(language, expected):
    assert be.best_for(language=language)[0] == expected


@pytest.mark.parametrize(
    ("platform", "expected"),
    [("ios", "macos-xcode"), ("android", "android"), ("flutter", "flutter")],
)
def test_a_platform_target_outranks_the_language(platform, expected):
    """A Kotlin Android app is a JVM project, but it needs the Android SDK."""
    assert be.best_for(language="kotlin", platform=platform)[0] == expected


def test_a_language_with_no_approved_environment_returns_nothing():
    assert be.best_for(language="rust")[0] is None


# ---------------------------------------------------------------------------
# What a generated pipeline may name
# ---------------------------------------------------------------------------

def test_a_catalog_image_is_permitted_verbatim():
    assert be.is_permitted_image(be.image("java-jdk11")) is True


def test_an_arbitrary_public_image_is_not_permitted():
    assert be.is_permitted_image("gradle:8-jdk21") is False
    assert be.is_permitted_image("ubuntu:latest") is False


def test_an_operator_can_allow_a_prefix(monkeypatch):
    monkeypatch.setenv("CI_ALLOWED_IMAGE_PREFIXES", "registry.areeba.com/,mirror.test/")
    assert be.is_permitted_image("registry.areeba.com/anything:1") is True
    assert be.is_permitted_image("mirror.test/tool:2") is True
    assert be.is_permitted_image("docker.io/library/ubuntu") is False


def test_no_image_at_all_is_permitted():
    """A stage with no image runs on whatever the runner provides, which is
    exactly right for an agent."""
    assert be.is_permitted_image("") is True


def test_the_catalog_lists_everything_for_the_capability_block():
    keys = {item["key"] for item in be.catalog()}
    assert {"java-jdk11", "node-22", "python-3.12", "android", "macos-xcode"} <= keys
    configured = {item["key"] for item in be.catalog(configured_only=True)}
    assert "android" not in configured
    assert "java-jdk11" in configured
