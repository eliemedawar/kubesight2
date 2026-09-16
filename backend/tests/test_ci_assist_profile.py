"""The application profile: what a repository IS, apart from how it is filed.

The point of the profile is that ``java_gradle`` stops being the answer to three
different questions. These tests hold the two halves of that: the detail is
kept in full, and the old discriminator is still derived correctly from it — so
nothing that already reads ``application_type`` starts getting a different
answer than it did before.
"""

from __future__ import annotations

import pytest

from api.services.ci_assist import profile


def java_gradle(**overrides):
    return {
        "language": "java",
        "languageVersion": "17",
        "framework": "spring-boot",
        "frameworkVersion": "3.3.2",
        "buildSystem": "gradle",
        "buildSystemVersion": "8.7",
        "usesBuildWrapper": True,
        "packaging": "jar",
        "testsDetected": True,
        "artifactPaths": ["build/libs/*.jar"],
        "containerization": {"type": "dockerfile", "dockerfilePath": "Dockerfile"},
        **overrides,
    }


# ---------------------------------------------------------------------------
# Normalization
# ---------------------------------------------------------------------------

def test_a_full_profile_survives_normalization_intact():
    """Every detected value is kept. The whole feature rests on the detail
    being available later — to pick a build image, to warn about a mismatch —
    so a normalizer that dropped fields would quietly undo it."""
    result = profile.normalize(java_gradle())

    assert result["language"] == "java"
    assert result["languageVersion"] == "17"
    assert result["framework"] == "spring-boot"
    assert result["frameworkVersion"] == "3.3.2"
    assert result["buildSystem"] == "gradle"
    assert result["buildSystemVersion"] == "8.7"
    assert result["usesBuildWrapper"] is True
    assert result["packaging"] == "jar"
    assert result["containerization"] == {
        "type": "dockerfile",
        "dockerfilePath": "Dockerfile",
    }
    assert result["schemaVersion"] == profile.SCHEMA_VERSION


def test_a_person_typing_a_bad_value_is_told_about_it():
    """At a form, a typo is worth naming: "Jva" should not silently become
    "other" and quietly change which build image the pipeline gets."""
    with pytest.raises(profile.ProfileError) as exc:
        profile.normalize(java_gradle(language="jva"), source="manual")
    assert "jva" in str(exc.value)


def test_a_model_using_a_different_word_is_understood_not_refused():
    """"executable jar" is exactly what a Spring Boot fat jar is. Losing a
    correct reading of the repository over a synonym is the same mistake as
    refusing `type` for `stageType`."""
    result = profile.normalize(java_gradle(packaging="executable jar"))
    assert result["packaging"] == "jar"
    assert any("executable jar" in note for note in result["notes"])


@pytest.mark.parametrize(
    ("field", "given", "expected"),
    [
        ("packaging", "fat jar", "jar"),
        ("packaging", "docker image", "container-image"),
        ("packaging", "android app bundle", "aab"),
        ("buildSystem", "Gradle Wrapper", "gradle"),
        ("buildSystem", "mvnw", "maven"),
        ("language", "Node.js", "javascript"),
        ("language", "golang", "go"),
        ("projectStructure", "multi module", "multi-module"),
    ],
)
def test_the_common_synonyms_are_understood(field, given, expected):
    assert profile.normalize(java_gradle(**{field: given}))[field] == expected


def test_a_value_nobody_recognises_is_recorded_rather_than_fatal():
    """Failing the whole analysis because one field used an unfamiliar word
    throws away everything else that was read correctly."""
    result = profile.normalize(java_gradle(language="brainfuck"))
    assert result["language"] == "other"
    assert any("brainfuck" in note for note in result["notes"])


def test_what_was_adjusted_is_always_said():
    """A reading KubeSight changed must be visible, or the profile silently
    differs from what the model actually reported."""
    result = profile.normalize(
        java_gradle(packaging="uber jar", buildSystem="gradlew", projectStructure="multimodule")
    )
    assert len(result["notes"]) == 3
    assert result["notes"] == sorted(result["notes"], key=result["notes"].index)


def test_versions_keep_the_form_the_project_wrote_them_in():
    """'17', '1.8' and '8.7-rc-2' are all real versions of real things. Parsing
    them into numbers would lose the only form the build tool understands."""
    for version in ("17", "1.8", "8.7-rc-2", "3.12.0", "21.0.1+12"):
        assert profile.normalize(java_gradle(languageVersion=version))["languageVersion"] == version


def test_a_version_a_person_typed_badly_is_refused():
    with pytest.raises(profile.ProfileError):
        profile.normalize(java_gradle(languageVersion="17; rm -rf /"), source="manual")


def test_a_decorated_version_from_a_model_is_trimmed_to_the_version():
    """Models write "17 (LTS)". The version is still 17, and the whole analysis
    should not die over the parenthetical."""
    result = profile.normalize(java_gradle(languageVersion="17 (LTS)"))
    assert result["languageVersion"] == "17"
    assert any("17 (LTS)" in note for note in result["notes"])


def test_a_dangerous_looking_version_never_survives_whole():
    """Tolerant is not the same as credulous — whatever comes back is still
    only the characters a version may contain."""
    result = profile.normalize(java_gradle(languageVersion="17; rm -rf /"))
    assert result["languageVersion"] == "17"
    assert ";" not in result["languageVersion"]


def test_an_absolute_dockerfile_path_is_refused():
    """Every path in KubeSight is relative to a checkout. An absolute one is
    either a mistake or an attempt to read outside the workspace."""
    with pytest.raises(profile.ProfileError):
        profile.normalize(
            java_gradle(containerization={"type": "dockerfile", "dockerfilePath": "/etc/passwd"})
        )
    with pytest.raises(profile.ProfileError):
        profile.normalize(
            java_gradle(containerization={"type": "dockerfile", "dockerfilePath": "../../Dockerfile"})
        )


def test_evidence_is_kept_per_field_with_a_confidence_word_not_a_number():
    """Application Intelligence removed model-chosen scores because a model
    asked for 0.96 produces 0.96. The same reasoning applies here, so a
    confidence is one of four words and a stray number is normalised away
    rather than presented as precision."""
    result = profile.normalize(
        java_gradle(
            evidence={
                "languageVersion": {
                    "value": "17",
                    "confidence": "Confirmed",
                    "source": "build.gradle",
                    "detail": "toolchain { languageVersion = 17 }",
                },
                "frameworkVersion": {"value": "3.3.2", "confidence": "0.96"},
            }
        )
    )
    assert result["evidence"]["languageVersion"]["confidence"] == "Confirmed"
    assert result["evidence"]["languageVersion"]["source"] == "build.gradle"
    assert result["evidence"]["frameworkVersion"]["confidence"] == "Medium"


def test_what_could_not_be_established_is_named_rather_than_guessed():
    result = profile.normalize(java_gradle(frameworkVersion="", unknown=["frameworkVersion"]))
    assert result["frameworkVersion"] == ""
    assert "frameworkVersion" in result["unknown"]


# ---------------------------------------------------------------------------
# Derivation — the bridge back to everything that already exists
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    ("overrides", "expected"),
    [
        ({}, "java_gradle"),
        ({"buildSystem": "maven"}, "java_maven"),
        ({"language": "kotlin", "buildSystem": "gradle"}, "java_gradle"),
        ({"language": "javascript", "buildSystem": "npm", "packaging": "bundle"}, "node"),
        ({"language": "typescript", "buildSystem": "pnpm", "packaging": "static-site"}, "node"),
        ({"language": "python", "buildSystem": "poetry", "packaging": "wheel"}, "python"),
        ({"language": "python", "buildSystem": "pip", "packaging": "none"}, "python"),
        ({"language": "dart", "buildSystem": "flutter", "packaging": "apk"}, "flutter"),
        ({"language": "swift", "buildSystem": "xcodebuild", "packaging": "ipa"}, "ios"),
        (
            {"language": "kotlin", "buildSystem": "gradle", "packaging": "aab",
             "platformTargets": ["android"]},
            "android",
        ),
        (
            {"language": "other", "buildSystem": "none", "packaging": "container-image"},
            "container",
        ),
    ],
)
def test_the_existing_application_type_is_derived_from_the_detail(overrides, expected):
    """The discriminator every existing reader uses — templates, fallback
    pipelines, card icons, readiness — is now computed rather than typed. It
    has to land on the same values those readers already understand."""
    assert profile.normalize(java_gradle(**overrides))["derivedApplicationType"] == expected


def test_android_beats_java_gradle_because_it_is_the_more_specific_truth():
    """A Kotlin Android app is genuinely Gradle AND JVM AND Android. Filing it
    as java_gradle would hand it a JAR pipeline for an APK project."""
    android = java_gradle(
        language="kotlin", buildSystem="gradle", packaging="apk", platformTargets=["android"]
    )
    assert profile.normalize(android)["derivedApplicationType"] == "android"


def test_a_jvm_project_with_no_known_build_system_is_not_silently_called_maven():
    """Guessing Maven would give it ./mvnw commands it cannot run. Unclassified
    is the honest answer, and 'generic' is what KubeSight already calls that."""
    result = profile.normalize(
        java_gradle(buildSystem="", containerization={"type": "none", "dockerfilePath": ""})
    )
    assert result["derivedApplicationType"] == "generic"


def test_a_jvm_project_with_no_build_system_but_a_dockerfile_builds_the_dockerfile():
    result = profile.normalize(java_gradle(buildSystem=""))
    assert result["derivedApplicationType"] == "container"


# ---------------------------------------------------------------------------
# Overrides
# ---------------------------------------------------------------------------

def test_a_user_override_replaces_the_value_and_is_recorded_as_theirs():
    """Recorded, not just applied. A regenerate has to build around a
    correction instead of detecting over the top of it — otherwise the user
    makes the same correction every time and stops trusting the feature."""
    detected = profile.normalize(java_gradle())
    corrected = profile.apply_overrides(detected, {"languageVersion": "21"})

    assert corrected["languageVersion"] == "21"
    assert corrected["overrides"]["languageVersion"]["was"] == "17"
    assert profile.is_overridden(corrected, "languageVersion")
    assert not profile.is_overridden(corrected, "buildSystem")


def test_an_override_that_changes_the_kind_of_project_re_derives_the_type():
    detected = profile.normalize(java_gradle())
    assert detected["derivedApplicationType"] == "java_gradle"
    corrected = profile.apply_overrides(detected, {"buildSystem": "maven"})
    assert corrected["derivedApplicationType"] == "java_maven"


def test_an_override_is_validated_like_any_other_input():
    """It is user input arriving over an API, and it reaches a build image
    lookup. A hand-typed 'Jva' has to fail here, not there."""
    detected = profile.normalize(java_gradle())
    with pytest.raises(profile.ProfileError):
        profile.apply_overrides(detected, {"language": "jva"})


def test_only_detected_fields_can_be_overridden():
    """Evidence and derived values are not opinions to be corrected; letting
    them be set by hand would make provenance meaningless."""
    detected = profile.normalize(java_gradle())
    with pytest.raises(profile.ProfileError) as exc:
        profile.apply_overrides(detected, {"evidence": {"languageVersion": {"value": "99"}}})
    assert "cannot be overridden" in str(exc.value)


def test_overriding_a_field_clears_it_from_unknown():
    """It was unknown to the analysis. It is not unknown to the user who just
    typed it, and continuing to flag it would be telling them they are missing
    something they have supplied."""
    detected = profile.normalize(java_gradle(frameworkVersion="", unknown=["frameworkVersion"]))
    corrected = profile.apply_overrides(detected, {"frameworkVersion": "3.3.2"})
    assert corrected["frameworkVersion"] == "3.3.2"
    assert "frameworkVersion" not in corrected["unknown"]


def test_summary_reads_as_a_sentence_and_survives_missing_pieces():
    assert profile.summary(None) == "Not analyzed"
    assert profile.summary(profile.normalize(java_gradle())) == (
        "Java 17 · spring-boot 3.3.2 · gradle 8.7"
    )
    sparse = profile.normalize({"language": "python", "buildSystem": "pip"})
    assert profile.summary(sparse) == "Python · pip"
