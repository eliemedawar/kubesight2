#!/usr/bin/env python3
"""Register AREEBAPAY-MOBILE-ANDROID as a native KubeSight CI service.

A port of the Android half of the areebapay-v2 Jenkins pipeline: the
``BUILD_APK_ONLY`` and ``BUILD_AAB_ONLY`` stages, plus the configuration stage
they depend on. The Mac agent and every iOS stage are deliberately left behind —
these stages run on the Kubernetes runner, in a Linux container.

Usage (from ``backend/``)::

    python tools/seed_areebapay_android_pipeline.py --dry-run   # print, write nothing
    python tools/seed_areebapay_android_pipeline.py --image registry.areeba.com/android-build:34
    python tools/seed_areebapay_android_pipeline.py --force     # overwrite stage edits

What it deliberately does NOT do
--------------------------------
It writes no credentials and no environment files. Everything secret is created
as a placeholder holding ``REPLACE_ME`` so the pipeline validates, and is filled
in afterwards under the service's Secrets panel:

    ENV_UAT / ENV_PREPROD / ENV_PROD   the three .env bodies the build writes out
    NPMRC                              the .npmrc body (optional; leave it at
                                       REPLACE_ME and the build writes none)
    ANDROID_KEYSTORE_B64               base64 of areebapay.keystore
                                       (``base64 -w0 areebapay.keystore``)
    ANDROID_STORE_PASS                 AREEBA_STORE_PASS
    ANDROID_KEY_ALIAS                  AREEBA_KEY_ALIAS
    ANDROID_KEY_PASS                   AREEBA_KEY_PASS

The Bitbucket credential profile that can read ``areebasal/areebapay-v2`` — the
``ocijenkins`` equivalent — is attached on the service's Source panel.

Why the .env bodies are secrets and not multiline build parameters
------------------------------------------------------------------
The Jenkins job carried them as text parameters and printed them with
``cat .env`` into the build log. They contain an RSA private key, an HMAC
secret and two API passwords. As CI secrets they are encrypted at rest, never
returned by any read API, and run through the log masker — so this port writes
line counts to the log where the Jenkins job wrote contents.

How the Jenkins job maps
------------------------
``repotag``               the Run Build dialog's own branch/tag picker. The
                          version check reads ``$KUBESIGHT_TAG``, so tag and
                          package.json are still held to agree; building a
                          branch skips the check rather than failing it.
``BUILD_NUMBER``          ``$KUBESIGHT_BUILD_NUMBER`` — see VERSION_CODE_OFFSET.
``script { Build_Version = sh(...) }``
                          "Resolve version" exports APP_VERSION and VERSION_CODE
                          to ``$KUBESIGHT_ENV``, which every later stage sources.
``when { equals }``       each build stage's run condition.
``agent { label 'mac' }`` gone. ``runnerLabels: [linux, android]``, which the
                          built-in Kubernetes runner satisfies.
``cleanWs()``             not needed: every build gets a fresh workspace.
``archiveArtifacts``      the stage's ``artifacts`` list. APKs and AABs land in
                          the artifact store typed ``apk`` / ``aab``, which is
                          what Mobile Applications ingests via ``ci_service_id``.
``/Users/devops/...``     nothing on the build host. The keystore arrives as a
                          secret and is written under $HOME, a pod-local tmpfs
                          that dies with the build.

Four things in the Jenkins job that are bugs, and are not reproduced
--------------------------------------------------------------------
1. ``sh "echo '${npmrc}' > .nmprc"`` — misspelt, so the npmrc never applied.
   Written here as ``.npmrc``.
2. ``envpreprod`` was written to ``./.env.uat``, but BUILD_AAB_ONLY then copies
   ``../.env.preprod`` — a file nothing creates. The preprod AAB therefore built
   against whatever ``.env`` happened to be there. Here each environment has its
   own file and the build fails if the selected one is empty.
3. BUILD_APK_ONLY swallowed its exception (``echo`` with no rethrow), so a
   failed APK build reported success and archived nothing. Here it fails.
4. ``echo '${envuat}' > ...`` breaks on any value containing a single quote and
   mangles the backslash escapes in the PEM blocks. ``printf %s`` writes the
   value exactly as it was pasted.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

_BACKEND_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_BACKEND_DIR))

SERVICE_SLUG = "areebapay-mobile-android"

# The one thing this file cannot know. A single image is used for every stage
# because a React Native Gradle build shells out to node for the JS bundle and
# for Hermes, so "the Android image" has to carry JDK 17, the Android SDK with
# build-tools, node 20 and yarn all at once. Override with --image.
DEFAULT_IMAGE = "registry.areeba.com/android-build:latest"


# --- Build inputs ------------------------------------------------------------

PARAMETERS = [
    {
        "name": "BuildOnlyAab",
        "type": "boolean",
        "label": "Build AAB (signed, for Play)",
        "description": "Runs ./gradlew bundleRelease with the upload keystore injected.",
        "default": "true",
    },
    {
        "name": "BuildOnlyApk",
        "type": "boolean",
        "label": "Build APK",
        "description": "Runs ./gradlew assembleRelease, signed with the same keystore.",
        "default": "false",
    },
    {
        "name": "BUILD_ENV",
        "type": "choice",
        "label": "Build environment",
        "description": (
            "Which .env is baked into the bundle. Replaces the Jenkins job's "
            "deploypreprod / DeployAndroidUAT flags, which were deploy switches "
            "being read as build configuration."
        ),
        "choices": ["uat", "preprod", "prod"],
        "default": "uat",
    },
    {
        "name": "VERSION_CODE_OFFSET",
        "type": "text",
        "label": "versionCode offset",
        "description": (
            "versionCode = offset + this build's number. Jenkins used 700 + a "
            "BUILD_NUMBER in the hundreds; KubeSight numbers builds per service "
            "from 1, so set this to the last versionCode you shipped or Play "
            "rejects the upload as a downgrade."
        ),
        "default": "700",
    },
]


# --- Stages ------------------------------------------------------------------

LABELS = ["linux", "android"]
KEYSTORE_PATH = "$HOME/areebapay.keystore"

SIGNING_SECRETS = [
    {"name": "ANDROID_KEYSTORE_B64", "envVar": "ANDROID_KEYSTORE_B64"},
    {"name": "ANDROID_STORE_PASS", "envVar": "ANDROID_STORE_PASS"},
    {"name": "ANDROID_KEY_ALIAS", "envVar": "ANDROID_KEY_ALIAS"},
    {"name": "ANDROID_KEY_PASS", "envVar": "ANDROID_KEY_PASS"},
]

# Kept on one line on purpose: the command list is joined with newlines, so a
# backslash-continued invocation would only survive by accident.
_GRADLE_SIGNING = (
    '-Pandroid.injected.signing.store.file="$KEYSTORE" '
    '-Pandroid.injected.signing.store.password="$ANDROID_STORE_PASS" '
    '-Pandroid.injected.signing.key.alias="$ANDROID_KEY_ALIAS" '
    '-Pandroid.injected.signing.key.password="$ANDROID_KEY_PASS"'
)


def _gradle_stage(name, task, variable, artifact_path, artifact_type, image):
    """One Gradle build, gated on its own checkbox and signed the same way.

    The Jenkins job injected signing only for the AAB and let the APK fall
    through to whatever ``signingConfig`` android/app/build.gradle names — which
    on the Mac resolved to a keystore under /Users/devops. Nothing on a Linux
    build pod can resolve that path, so both tasks inject the same keystore here.
    """
    return {
        "name": name,
        "stageType": "command",
        "image": image,
        "runnerLabels": LABELS,
        "workingDirectory": "android",
        "runCondition": {"variable": variable, "operator": "equals", "value": "true"},
        "commands": [
            # The release buildType's own signingConfig would win over the
            # injected one. The same sed as the Jenkins job, minus the BSD ''.
            "sed -i '/release {/,/^    }/ s/.*signingConfig.*//' app/build.gradle",
            'KEYSTORE="%s"' % KEYSTORE_PATH,
            'umask 077 && printf %s "$ANDROID_KEYSTORE_B64" | base64 -d > "$KEYSTORE"',
            '[ -s "$KEYSTORE" ] || { echo "ANDROID_KEYSTORE_B64 did not decode to a keystore"; exit 1; }',
            'echo "[gradle] %s versionName=$APP_VERSION versionCode=$VERSION_CODE"' % task,
            './gradlew --no-daemon %s -PversionCode="$VERSION_CODE" '
            '-PversionName="$APP_VERSION" %s' % (task, _GRADLE_SIGNING),
            # $HOME is a pod-local tmpfs that dies with the build, so this is
            # tidiness rather than the thing that protects the key.
            'rm -f "$KEYSTORE"',
        ],
        "secretRefs": list(SIGNING_SECRETS),
        "artifacts": [
            {"path": artifact_path, "type": artifact_type, "name": "areebapay"}
        ],
        "timeoutSeconds": 5400,
    }


def build_stages(image):
    return [
        {
            "name": "Checkout",
            "stageType": "checkout",
            "runnerLabels": LABELS,
            "commands": [],
            "timeoutSeconds": 900,
        },
        {
            "name": "Resolve version",
            "stageType": "command",
            "image": image,
            "runnerLabels": LABELS,
            # The Jenkins `script { Build_Version = sh(...) }` binding, written
            # to the file every later stage sources. Both values are exported
            # here so the APK and the AAB can never disagree about what they are.
            "commands": [
                'VERSION=$(sed -n \'s/.*"version"[[:space:]]*:[[:space:]]*"\\([^"]*\\)".*/\\1/p\' '
                "package.json | head -n 1)",
                '[ -n "$VERSION" ] || { echo "No \\"version\\" in package.json"; exit 1; }',
                'if [ -n "${KUBESIGHT_TAG:-}" ]; then',
                '  if [ "$VERSION" != "$KUBESIGHT_TAG" ]; then',
                '    echo "package.json says $VERSION but the tag is $KUBESIGHT_TAG"',
                "    exit 1",
                "  fi",
                '  echo "[version] tag and package.json agree on $VERSION"',
                "else",
                '  echo "[version] building branch ${KUBESIGHT_BRANCH:-?}; no tag to check $VERSION against"',
                "fi",
                "VERSION_CODE=$(( ${VERSION_CODE_OFFSET:-700} + KUBESIGHT_BUILD_NUMBER ))",
                'printf "APP_VERSION=%s\\n" "$VERSION" >> "$KUBESIGHT_ENV"',
                'printf "VERSION_CODE=%s\\n" "$VERSION_CODE" >> "$KUBESIGHT_ENV"',
                'echo "[version] versionName=$VERSION versionCode=$VERSION_CODE"',
            ],
            "timeoutSeconds": 300,
        },
        {
            "name": "Write configuration",
            "stageType": "command",
            "image": image,
            "runnerLabels": LABELS,
            # printf %s, not echo: the Jenkins job's `echo '${envuat}' > .env.uat`
            # breaks on any value containing a single quote and mangles the
            # backslash-escaped newlines in the PEM blocks.
            "commands": [
                "umask 077",
                'printf %s "$ENV_UAT" > .env.uat',
                'printf %s "$ENV_PREPROD" > .env.preprod',
                'printf %s "$ENV_PROD" > .env.prod',
                'if [ -n "${NPMRC:-}" ] && [ "$NPMRC" != "REPLACE_ME" ]; then',
                '  printf %s "$NPMRC" > .npmrc',
                '  echo "[config] wrote .npmrc"',
                "fi",
                'case "$BUILD_ENV" in',
                "  uat) SRC=.env.uat ;;",
                "  preprod) SRC=.env.preprod ;;",
                "  prod) SRC=.env.prod ;;",
                "  *) echo \"Unknown BUILD_ENV '$BUILD_ENV'\"; exit 1 ;;",
                "esac",
                '[ -s "$SRC" ] && [ "$(cat "$SRC")" != "REPLACE_ME" ] || '
                '{ echo "$SRC is empty - set the matching secret under Secrets"; exit 1; }',
                # react-native-config reads the project root; the Jenkins job
                # also dropped a copy in android/, and the Gradle plugin reads
                # that one.
                'cp "$SRC" .env',
                'cp "$SRC" android/.env',
                # Line counts, never contents: this file holds a private key.
                'echo "[config] $BUILD_ENV -> .env ($(wc -l < .env) lines)"',
            ],
            "secretRefs": [
                {"name": "ENV_UAT", "envVar": "ENV_UAT"},
                {"name": "ENV_PREPROD", "envVar": "ENV_PREPROD"},
                {"name": "ENV_PROD", "envVar": "ENV_PROD"},
                {"name": "NPMRC", "envVar": "NPMRC"},
            ],
            "timeoutSeconds": 300,
        },
        {
            "name": "Install dependencies",
            "stageType": "command",
            "image": image,
            "runnerLabels": LABELS,
            # Unconditional, as in the Jenkins job, where both build stages
            # opened with their own `yarn install`. A run condition reads ONE
            # variable and "either checkbox is ticked" is two, so gating this
            # would need a third input that must be kept in step with them.
            "commands": [
                "node -v",
                "yarn --version",
                "yarn install",
            ],
            "timeoutSeconds": 3600,
        },
        _gradle_stage(
            "Build APK",
            "assembleRelease",
            "BuildOnlyApk",
            "app/build/outputs/apk/release/*.apk",
            "apk",
            image,
        ),
        _gradle_stage(
            "Build AAB",
            "bundleRelease",
            "BuildOnlyAab",
            "app/build/outputs/bundle/release/*.aab",
            "aab",
            image,
        ),
    ]


# Placeholders, created so the pipeline's secretRefs resolve and it saves. Real
# values are pasted under the service's Secrets panel; nothing this file writes
# is a credential.
PLACEHOLDER_SECRETS = {
    "ENV_UAT": "REPLACE_ME",
    "ENV_PREPROD": "REPLACE_ME",
    "ENV_PROD": "REPLACE_ME",
    "NPMRC": "REPLACE_ME",
    "ANDROID_KEYSTORE_B64": "REPLACE_ME",
    "ANDROID_STORE_PASS": "REPLACE_ME",
    "ANDROID_KEY_ALIAS": "REPLACE_ME",
    "ANDROID_KEY_PASS": "REPLACE_ME",
}

SERVICE_PAYLOAD = {
    "name": "AREEBAPAY-MOBILE-ANDROID",
    "slug": SERVICE_SLUG,
    "description": (
        "AreebaPay React Native app, Android binaries. Ported from the "
        "areebapay-v2 Jenkins pipeline (APK and AAB stages only)."
    ),
    "applicationType": "android",
    "criticality": "high",
    # Source is applied separately: apply_source() requires a credential
    # profile, and which one to use is a decision this file cannot make.
    "createDefaultPipeline": False,
}

SOURCE_PAYLOAD = {
    "repositoryProvider": "bitbucket",
    "repositoryUrl": "https://bitbucket.org/areebasal/areebapay-v2.git",
    "defaultBranch": "master",
}


def _summary(stages) -> str:
    lines = ["Service : %s (%s)" % (SERVICE_PAYLOAD["name"], SERVICE_SLUG)]
    lines.append("Repo    : %s" % SOURCE_PAYLOAD["repositoryUrl"])
    lines.append("Image   : %s" % (stages[1].get("image") or "-"))
    lines.append("")
    lines.append("Build inputs:")
    for param in PARAMETERS:
        detail = param["type"]
        if param["type"] == "choice":
            detail += " (%s)" % ", ".join(param["choices"])
        lines.append("  %-20s %-22s default %s" % (param["name"], detail, param["default"]))
    lines.append("")
    lines.append("Stages:")
    for index, stage in enumerate(stages):
        condition = stage.get("runCondition")
        gate = ""
        if condition:
            gate = "   when %s = %s" % (condition["variable"], condition["value"])
        lines.append("  %2d. %-22s %s%s" % (index + 1, stage["name"], stage["stageType"], gate))
    lines.append("")
    lines.append("Secrets created as REPLACE_ME (fill them in the UI):")
    lines.append("  " + ", ".join(sorted(PLACEHOLDER_SECRETS)))
    return "\n".join(lines)


def _stage_signature(stages) -> list:
    """What "still the seeded pipeline" means.

    Names, types and conditions — the shape of the pipeline. Command text is
    deliberately excluded: tuning a Gradle flag should not make the seed refuse
    to re-run, while adding or reordering a stage should.
    """
    out = []
    for stage in stages:
        if isinstance(stage, dict):
            name = stage.get("name")
            kind = stage.get("stageType")
            condition = stage.get("runCondition")
        else:
            name = stage.name
            kind = stage.stage_type
            condition = stage.run_condition
        out.append(
            (
                (name or "").lower(),
                kind or "command",
                json.dumps(condition or None, sort_keys=True),
            )
        )
    return out


def _pick_one(rows, wanted, kind, flag):
    """Resolve a named row, or the only candidate, or nothing.

    Nothing is a normal outcome, not an error: the service is still created and
    the UI says what is missing. Guessing between several would attach the wrong
    credential to a build that then fails somewhere far less obvious.
    """
    if wanted:
        for row in rows:
            if str(row.id) == str(wanted) or (row.name or "").lower() == str(wanted).lower():
                return row
        print("  %s: no match for %r - leaving it unset" % (kind, wanted))
        return None
    if len(rows) == 1:
        return rows[0]
    if not rows:
        print("  %s: none configured - attach one in the UI" % kind)
    else:
        names = ", ".join("%s:%s" % (row.id, row.name) for row in rows)
        print("  %s: %d to choose from (%s) - pass %s" % (kind, len(rows), names, flag))
    return None


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--dry-run", action="store_true", help="print what would be written, change nothing"
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="replace the pipeline even if its stages were changed since seeding",
    )
    parser.add_argument(
        "--image",
        default=DEFAULT_IMAGE,
        help="build image for every stage: JDK 17 + Android SDK + node 20 + yarn "
        "(default: %s)" % DEFAULT_IMAGE,
    )
    parser.add_argument(
        "--credential-profile",
        help="source credential profile, by id or name (default: the only one, if there is one)",
    )
    parser.add_argument("--json", action="store_true", help="print the pipeline payload as JSON")
    args = parser.parse_args()

    stages = build_stages(args.image)

    if args.json:
        print(
            json.dumps(
                {
                    "service": SERVICE_PAYLOAD,
                    "source": SOURCE_PAYLOAD,
                    "parameters": PARAMETERS,
                    "stages": stages,
                },
                indent=2,
            )
        )
        return 0

    print(_summary(stages))
    if args.dry_run:
        print("\n--dry-run: nothing was written.")
        return 0

    from api import create_app  # noqa: E402  (needs the sys.path insert above)
    from api.models_application_intelligence import BitbucketCredentialProfile  # noqa: E402
    from api.models_ci import CiService  # noqa: E402
    from api.services.ci import catalog as catalog_service  # noqa: E402
    from api.services.ci import pipelines as pipelines_service  # noqa: E402
    from api.services.ci import secrets as secrets_service  # noqa: E402

    app = create_app()
    with app.app_context():
        service = CiService.query.filter_by(slug=SERVICE_SLUG).first()
        if service is None:
            catalog_service.create_service(dict(SERVICE_PAYLOAD))
            service = CiService.query.filter_by(slug=SERVICE_SLUG).first()
            print("\nCreated service #%s." % service.id)
        else:
            print("\nService #%s already exists - updating it in place." % service.id)

        # --- source ---------------------------------------------------------
        if service.credential_profile_id and not args.credential_profile:
            print("  source: already configured, left alone")
        else:
            profiles = (
                BitbucketCredentialProfile.query.filter_by(provider="bitbucket", enabled=True)
                .order_by(BitbucketCredentialProfile.name.asc())
                .all()
            )
            profile = _pick_one(
                profiles, args.credential_profile, "source credential", "--credential-profile"
            )
            if profile is not None:
                catalog_service.update_source(
                    service, dict(SOURCE_PAYLOAD, credentialProfileId=profile.id)
                )
                print("  source: %s via '%s'" % (SOURCE_PAYLOAD["repositoryUrl"], profile.name))

        # --- secrets (references must resolve before the pipeline saves) -----
        existing_keys = {row["key"] for row in secrets_service.list_secrets(service.id)}
        for key, value in PLACEHOLDER_SECRETS.items():
            if key in existing_keys:
                print("  secret %s: already set, left alone" % key)
                continue
            secrets_service.create_secret({"key": key, "value": value}, service_id=service.id)
            print("  secret %s: created as %s - replace it before building" % (key, value))

        payload = {
            "name": "default",
            "description": "Ported from the areebapay-v2 Jenkins pipeline (Android only).",
            "isDefault": True,
            "enabled": True,
            "parameters": PARAMETERS,
            "stages": stages,
        }

        pipeline = next((p for p in service.pipelines if p.name == "default"), None)
        if pipeline is None:
            pipelines_service.create_pipeline(service, payload)
            print("  pipeline: created")
        elif _stage_signature(pipeline.stages) != _stage_signature(stages) and not args.force:
            print(
                "  pipeline: left alone - its stages differ from this file "
                "(renamed, reordered, added or removed). Re-run with --force to replace them."
            )
        else:
            pipelines_service.update_pipeline(pipeline, payload)
            print("  pipeline: updated")

    print(
        "\nBefore the first build, in the Service Catalog:\n"
        "  1. Source  - a Bitbucket credential profile that can read areebasal/areebapay-v2.\n"
        "  2. Secrets - real values for the eight placeholders above. The keystore is\n"
        "               base64: base64 -w0 areebapay.keystore\n"
        "  3. Runners - the Kubernetes runner enabled, and a build image that carries\n"
        "               JDK 17, the Android SDK, node 20 and yarn (--image).\n"
        "  4. Set VERSION_CODE_OFFSET to the last versionCode Jenkins shipped.\n"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
