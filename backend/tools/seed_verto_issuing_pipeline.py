#!/usr/bin/env python3
"""Register VERTO-ISSUING-WEB-APPLICATION as a native KubeSight CI service.

A port of the Jenkins pipeline of the same name, stage for stage and parameter
for parameter. Run it once; it is idempotent, so re-running updates the pipeline
in place rather than creating a second service.

Usage (from ``backend/``)::

    python tools/seed_verto_issuing_pipeline.py            # create or update
    python tools/seed_verto_issuing_pipeline.py --dry-run  # print, write nothing
    python tools/seed_verto_issuing_pipeline.py --force    # overwrite stage edits

What it deliberately does NOT do
--------------------------------
It writes no credentials. Three things must be attached by hand afterwards,
because each is a secret this file has no business holding:

1. The **Bitbucket credential profile** on the service's Source panel — the
   ``ocijenkins`` equivalent. Without it the checkout cannot run.
2. The **registry connection** on the service's settings — ``registry.areeba.com``
   port 9443. Without it the image stage is skipped with that as the reason.
3. The two **CI secrets** it creates as placeholders:
   ``DEPLOY_FILES_TOKEN`` (a Bitbucket app password that can read
   ``areebasal/deployment-files``) and ``ANSIBLE_SSH_KEY`` (the private key for
   ``ocijenkins`` on 10.4.27.3). Both are created holding the string
   ``REPLACE_ME`` so the pipeline saves; replace them under Secrets before the
   first deploy.

How the Jenkins job maps
------------------------
``repotag``            the Run Build dialog's own branch/tag picker. Jenkins
                       needed a parameter plus an ``if`` to decide whether to
                       check out a branch or ``refs/tags/``; KubeSight's dialog
                       is that choice, so duplicating it as a parameter would
                       mean two fields that must agree.
``version``            read from package.json by the "Resolve version" stage and
                       exported to ``$KUBESIGHT_ENV``, which every later stage
                       sources — the replacement for a Groovy binding shared by
                       one interpreter.
``${BUILD_NUMBER}``    ``$KUBESIGHT_BUILD_NUMBER``.
``when { equals }``    each deploy stage's run condition.
``cleanWs()``          not needed: every build gets a fresh workspace.
``agent { label }``    runner labels; every stage here wants a linux runner.
``agent { docker }``   the stage's own image, which is how KubeSight runs any
                       stage that names one.
"""

from __future__ import annotations

import argparse
import json

import sys
from pathlib import Path

_BACKEND_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_BACKEND_DIR))

SERVICE_SLUG = "verto-issuing-web-application"

# --- The four documents the Jenkins job carries as text parameters -----------
# Kept verbatim. They are defaults: whoever runs a build sees them in the Run
# Build dialog and can edit that run's copy without touching the pipeline.

DOCKERFILE = """FROM registry.areeba.com/nginx

RUN rm -f /etc/nginx/conf.d/default.conf
COPY default.conf /etc/nginx/conf.d/default.conf
COPY ./dist/  /usr/share/nginx/html/

WORKDIR  /usr/share/nginx/html/

COPY entrypoint.sh /docker-entrypoint.sh
RUN chmod 777 /docker-entrypoint.sh
"""

ENVI = """VITE_MUI_DATAGRID_PRO_LICENSE_KEY=ISSUING_MUI_DATAGRID_PRO_LICENSE_KEY
VITE_BASE_URL_ONBOARDING=ISSUING_BASE_URL_ONBOARDING
VITE_BASE_URL_MERCHANT_SERVICE=ISSUING_BASE_URL_MERCHANT_SERVICE
VITE_BASE_URL_PROCESSING=ISSUING_BASE_URL_PROCESSING
VITE_BASE_URL_PROFILE=ISSUING_BASE_URL_PROFILE
VITE_BASE_URL_PERSONA=ISSUING_BASE_URL_PERSONA
VITE_BASE_URL_ISSUING=ISSUING_BASE_URL_ISSUING
VITE_BASE_URL_FRONTEND=ISSUING_BASE_URL_FRONTEND
VITE_BASE_URL_INTEGRATION=ISSUING_BASE_URL_INTEGRATION
VITE_BASE_URL_CLEARING=ISSUING_BASE_URL_CLEARING
VITE_BASE_URL_QUERY=ISSUING_BASE_URL_QUERY
VITE_BASE_URL_DV=ISSUING_BASE_URL_DV
VITE_BASE_URL_REVOLVING=ISSUING_BASE_URL_REVOLVING
VITE_BASE_URL_LOYALTY=ISSUING_BASE_URL_LOYALTY
VITE_BASE_TOKEN_DV=ISSUING_BASE_TOKEN_DV
VITE_ENCRYPTION_KEY_PAN=ISSUING_ENCRYPTION_KEY_PAN
VITE_BASE_URL_NEO=ISSUING_BASE_URL_NEO
VITE_BASE_URL_PIN_VAULT=ISSUING_BASE_URL_PIN_VAULT
VITE_EXECUTABLE_REPORTS_SAHARA=ISSUING_EXECUTABLE_REPORTS_SAHARA
VITE_EXECUTABLE_REPORTS_FAB=ISSUING_EXECUTABLE_REPORTS_FAB
VITE_EXECUTABLE_REPORTS=ISSUING_EXECUTABLE_REPORTS
VITE_BASE_URL_SAHARA=ISSUING_BASE_URL_SAHARA
VITE_EXECUTION_ID=ISSUING_EXECUTION_ID
VITE_BASE_URL_AUDI=ISSUING_BASE_URL_AUDI
VITE_BASE_URL_AMB=ISSUING_BASE_URL_AMB
VITE_BASE_URL_DIM=ISSUING_BASE_URL_DIM
VITE_BASE_URL_FAB=ISSUING_BASE_URL_FAB
VITE_BASE_URL_PAYMENT_SCHEDULER=ISSUING_BASE_URL_PAYMENT_SCHEDULER
VITE_EXECUTABLE_LEGACY_REPORTS=ISSUING_EXECUTABLE_LEGACY_REPORTS
VITE_WSO2_ENABLED=ISSUING_WSO2_ENABLED
VITE_WSO2_LOGIN_URL=ISSUING_WSO2_LOGIN_URL
VITE_WSO2_LOGOUT_URL=ISSUING_WSO2_LOGOUT_URL
VITE_BASE_URL_CEDRUS=ISSUING_BASE_URL_CEDRUS
"""

CONFIGURATION = """server {
    listen      80;

    server_name  verto-ui.areeba.com;
    root /usr/share/nginx/html;
    index index.html;
    add_header Cache-Control "no-cache, no-store, must-revalidate";

    location / {
        try_files $uri /index.html  =404;
    }
}
"""

# The entrypoint REPLACES the nginx image's own /docker-entrypoint.sh (the
# Dockerfile copies it there), so it has to do that script's work itself before
# exec'ing the command — calling /docker-entrypoint.sh would re-enter this file
# and loop forever. The Jenkins parameter carried the upstream body inlined for
# exactly that reason; this is the same thing with the forty hand-written sed
# lines collapsed into the loop that generates them, derived from the VITE_
# names in `envi` above.
ENTRYPOINT = """#!/bin/sh
set -e

# Every VITE_* value is baked into the bundle as an ISSUING_* placeholder at
# build time and rewritten here, so one image serves every environment.
cd /usr/share/nginx/html
env | grep '^VITE_' | while IFS='=' read -r name value; do
  placeholder="ISSUING_${name#VITE_}"
  [ -n "$value" ] || continue
  find . -type f -exec sed -i "s,${placeholder},${value},g" {} \\;
done

entrypoint_log() {
  if [ -z "${NGINX_ENTRYPOINT_QUIET_LOGS:-}" ]; then
    echo "$@"
  fi
}

if [ "$1" = "nginx" ] || [ "$1" = "nginx-debug" ]; then
  if find "/docker-entrypoint.d/" -mindepth 1 -maxdepth 1 -type f -print -quit 2>/dev/null | read -r v; then
    entrypoint_log "$0: /docker-entrypoint.d/ is not empty, will attempt to perform configuration"
    find "/docker-entrypoint.d/" -follow -type f -print | sort -V | while read -r f; do
      case "$f" in
        *.envsh)
          if [ -x "$f" ]; then
            entrypoint_log "$0: Sourcing $f"
            . "$f"
          else
            entrypoint_log "$0: Ignoring $f, not executable"
          fi
          ;;
        *.sh)
          if [ -x "$f" ]; then
            entrypoint_log "$0: Launching $f"
            "$f"
          else
            entrypoint_log "$0: Ignoring $f, not executable"
          fi
          ;;
        *) entrypoint_log "$0: Ignoring $f" ;;
      esac
    done
    entrypoint_log "$0: Configuration complete; ready for start up"
  else
    entrypoint_log "$0: No files found in /docker-entrypoint.d/, skipping configuration"
  fi
fi

exec "$@"
"""


# --- Build inputs ------------------------------------------------------------

PARAMETERS = [
    {
        "name": "Dockerfile",
        "type": "multiline",
        "label": "Dockerfile",
        "description": "Written to ./Dockerfile before the image is built.",
        "default": DOCKERFILE,
    },
    {
        "name": "envi",
        "type": "multiline",
        "label": "Build environment (.env)",
        "description": "Written to ./.env before yarn build. Vite reads it at build time.",
        "default": ENVI,
    },
    {
        "name": "configuration",
        "type": "multiline",
        "label": "nginx configuration",
        "description": "Written to ./default.conf and copied to /etc/nginx/conf.d/.",
        "default": CONFIGURATION,
    },
    {
        "name": "entrypoint",
        "type": "multiline",
        "label": "Container entrypoint",
        "description": "Written to ./entrypoint.sh and copied to /docker-entrypoint.sh.",
        "default": ENTRYPOINT,
    },
    # The deploy switches. Boolean parameters hold the strings "true"/"false",
    # which is what each deploy stage's run condition compares against.
    {
        "name": "Lebanondev",
        "type": "boolean",
        "label": "Deploy to dev (Lebanon)",
        "description": (
            "Also changes the image tag: dev builds are tagged "
            "V<version>-<build>-dev, everything else V<version>-prod."
        ),
        "default": "false",
    },
    {
        "name": "Lebanonsit",
        "type": "boolean",
        "label": "Deploy to SIT (Lebanon)",
        "default": "false",
    },
    {
        "name": "Lebanonuat",
        "type": "boolean",
        "label": "Deploy to UAT (Lebanon)",
        "default": "false",
    },
    {
        "name": "Lebanonpreprod",
        "type": "boolean",
        "label": "Deploy to preprod (Lebanon)",
        "default": "false",
    },
    {
        "name": "Lebanonsibedge",
        "type": "boolean",
        "label": "Deploy to sibedge (Lebanon)",
        "default": "false",
    },
]


# --- Stages ------------------------------------------------------------------

IMAGE_NAME = "verto-issuing-app"
ANSIBLE_HOST = "10.4.27.3"
ANSIBLE_USER = "ocijenkins"
NODE_IMAGE = "registry.areeba.com/node:lts-alpine"
# Any image with ansible-core and an ssh client. Kept as a stage image rather
# than a runner label so changing it is a pipeline edit, not a cluster change.
ANSIBLE_IMAGE = "registry.areeba.com/ansible:latest"
TOOLS_IMAGE = "registry.areeba.com/alpine/git:latest"


def _deploy_stage(name: str, environment: str, variable: str):
    """One ansible deploy, gated on its own checkbox.

    ``$APP_VERSION_TAG`` was exported by "Resolve version" and is the exact tag
    the image stage pushed, so the deploy can never name a tag that was not
    built — the failure mode of carrying the version string twice.
    """
    return {
        "name": name,
        "stageType": "command",
        "image": ANSIBLE_IMAGE,
        "runnerLabels": ["linux"],
        "runCondition": {"variable": variable, "operator": "equals", "value": "true"},
        "commands": [
            'test -d deployment-files || { echo "deployment-files was not cloned"; exit 1; }',
            # Written under HOME (a tmpfs on the Kubernetes runner, the agent's
            # own workspace otherwise) rather than the source tree, so it cannot
            # end up in the image build context.
            'umask 077 && printf %s "$ANSIBLE_SSH_KEY" > "$HOME/.ssh_key"',
            "export ANSIBLE_HOST_KEY_CHECKING=False",
            f'ansible-playbook ./deployment-files/ansibleImageUpdate.yml -i {ANSIBLE_HOST}, '
            f'--private-key "$HOME/.ssh_key" '
            f'--extra-vars "deployment_name={IMAGE_NAME} version=$APP_VERSION_TAG '
            f'ansible_user={ANSIBLE_USER} env={environment}"',
            'rm -f $HOME/.ssh_key',
        ],
        "secretRefs": [{"name": "ANSIBLE_SSH_KEY", "envVar": "ANSIBLE_SSH_KEY"}],
        "timeoutSeconds": 1800,
    }


STAGES = [
    {
        "name": "Checkout",
        "stageType": "checkout",
        "runnerLabels": ["linux"],
        "commands": [],
        "timeoutSeconds": 600,
    },
    {
        "name": "Pre Build",
        "stageType": "command",
        "image": TOOLS_IMAGE,
        "runnerLabels": ["linux"],
        # printf, not echo: the Jenkins job used `echo '$envi' > .env`, which
        # breaks on any value containing a single quote and mangles backslashes
        # in the entrypoint's sed expressions. printf %s writes the parameter
        # exactly as it was typed.
        "commands": [
            'printf %s "$envi" > .env',
            'printf %s "$Dockerfile" > Dockerfile',
            'printf %s "$configuration" > default.conf',
            'printf %s "$entrypoint" > entrypoint.sh',
            "chmod +x entrypoint.sh",
            "echo '[pre-build] wrote .env, Dockerfile, default.conf, entrypoint.sh'",
            "wc -l .env Dockerfile default.conf entrypoint.sh",
        ],
        "timeoutSeconds": 300,
    },
    {
        "name": "Build Project",
        "stageType": "command",
        "image": NODE_IMAGE,
        "runnerLabels": ["linux", "node"],
        "commands": [
            "rm -rf package-lock.json",
            "yarn add @babel/runtime",
            "yarn install",
            "yarn build",
        ],
        "timeoutSeconds": 3600,
    },
    {
        "name": "Resolve version",
        "stageType": "command",
        "image": TOOLS_IMAGE,
        "runnerLabels": ["linux"],
        # The Jenkins `script { version = sh(...) }` binding, written to the
        # file every later stage sources. Exporting the finished TAG rather than
        # just the version keeps the dev/prod choice in one place — the image
        # stage and all five deploys then read the same string.
        "commands": [
            'VERSION=$(sed -n \'s/.*"version"[[:space:]]*:[[:space:]]*"\\([^"]*\\)".*/\\1/p\' '
            "package.json | head -n 1)",
            '[ -n "$VERSION" ] || { echo "No \\"version\\" in package.json"; exit 1; }',
            'if [ "$Lebanondev" = "true" ]; then',
            '  TAG="V${VERSION}-${KUBESIGHT_BUILD_NUMBER}-dev"',
            "else",
            '  TAG="V${VERSION}-prod"',
            "fi",
            'printf "APP_VERSION=%s\\n" "$VERSION" >> "$KUBESIGHT_ENV"',
            'printf "APP_VERSION_TAG=%s\\n" "$TAG" >> "$KUBESIGHT_ENV"',
            'echo "[version] package.json says $VERSION; image will be tagged $TAG"',
        ],
        "timeoutSeconds": 300,
    },
    {
        "name": "Build and push image",
        "stageType": "container_image",
        "runnerLabels": ["linux"],
        # IMAGE_TAG is a template: APP_VERSION_TAG does not exist until the
        # stage above runs, so the build pod's own shell finishes the tag.
        "env": {
            "IMAGE_NAME": IMAGE_NAME,
            "IMAGE_TAG": "${APP_VERSION_TAG}",
            "DOCKERFILE_PATH": "Dockerfile",
        },
        "timeoutSeconds": 3600,
    },
    {
        "name": "Get deployment files",
        "stageType": "command",
        "image": TOOLS_IMAGE,
        "runnerLabels": ["linux"],
        # Unconditional, as in the Jenkins job. A run condition reads ONE
        # variable, and "any of the five environments was ticked" is five — so
        # gating this would mean either five conditions or a fake parameter that
        # must be kept in step with the checkboxes. A shallow clone of one small
        # repository is cheaper than either.
        "commands": [
            "rm -rf deployment-files",
            "git clone https://$DEPLOY_FILES_USER:$DEPLOY_FILES_TOKEN@"
            "bitbucket.org/areebasal/deployment-files.git",
        ],
        "secretRefs": [
            {"name": "DEPLOY_FILES_USER", "envVar": "DEPLOY_FILES_USER"},
            {"name": "DEPLOY_FILES_TOKEN", "envVar": "DEPLOY_FILES_TOKEN"},
        ],
        "timeoutSeconds": 600,
    },
    _deploy_stage("Deploy dev Lebanon", "verto-dev", "Lebanondev"),
    _deploy_stage("Deploy sit Lebanon", "verto-sit", "Lebanonsit"),
    _deploy_stage("Deploy UAT Lebanon", "verto-uat", "Lebanonuat"),
    _deploy_stage("Deploy preprod Lebanon", "verto-preprod", "Lebanonpreprod"),
    _deploy_stage("Deploy sibedge Lebanon", "sibedge", "Lebanonsibedge"),
]


# Placeholder secrets, created so the pipeline's secretRefs validate. Values are
# replaced under the service's Secrets panel; nothing real is written here.
PLACEHOLDER_SECRETS = {
    "DEPLOY_FILES_USER": "REPLACE_ME",
    "DEPLOY_FILES_TOKEN": "REPLACE_ME",
    "ANSIBLE_SSH_KEY": "REPLACE_ME",
}

SERVICE_PAYLOAD = {
    "name": "VERTO-ISSUING-WEB-APPLICATION",
    "slug": SERVICE_SLUG,
    "description": (
        "Verto issuing web application (Vite + nginx). Ported from the Jenkins "
        "pipeline of the same name."
    ),
    "applicationType": "node",
    "criticality": "high",
    # Source is applied separately: apply_source() requires a credential
    # profile, and which one to use is a decision this file cannot make.
    "createDefaultPipeline": False,
}

SOURCE_PAYLOAD = {
    "repositoryProvider": "bitbucket",
    "repositoryUrl": "https://bitbucket.org/areebasal/issuing-app.git",
    "defaultBranch": "master",
}

REGISTRY_HINT = "registry.areeba.com"


def _summary() -> str:
    lines = [f"Service : {SERVICE_PAYLOAD['name']} ({SERVICE_SLUG})"]
    lines.append(f"Repo    : {SOURCE_PAYLOAD['repositoryUrl']}")
    lines.append("")
    lines.append("Build inputs:")
    for param in PARAMETERS:
        detail = param["type"]
        if param["type"] == "multiline":
            detail += f", {len(param['default'].splitlines())} lines"
        lines.append(f"  {param['name']:<16} {detail}")
    lines.append("")
    lines.append("Stages:")
    for index, stage in enumerate(STAGES):
        condition = stage.get("runCondition")
        gate = f"   when {condition['variable']} = {condition['value']}" if condition else ""
        lines.append(f"  {index + 1:>2}. {stage['name']:<26} {stage['stageType']}{gate}")
    return "\n".join(lines)


def _stage_signature(stages) -> list:
    """What "still the seeded pipeline" means.

    Names, types and conditions - the shape of the pipeline. Command text is
    deliberately excluded: tuning a yarn flag should not make the seed refuse to
    re-run, while adding or reordering a stage should.
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
        "--credential-profile",
        help="source credential profile, by id or name (default: the only one, if there is one)",
    )
    parser.add_argument(
        "--registry",
        help="registry connection, by id or name (default: the only enabled one)",
    )
    parser.add_argument("--json", action="store_true", help="print the pipeline payload as JSON")
    args = parser.parse_args()

    if args.json:
        print(
            json.dumps(
                {
                    "service": SERVICE_PAYLOAD,
                    "source": SOURCE_PAYLOAD,
                    "parameters": PARAMETERS,
                    "stages": STAGES,
                },
                indent=2,
            )
        )
        return 0

    print(_summary())
    if args.dry_run:
        print("\n--dry-run: nothing was written.")
        return 0

    from api import create_app  # noqa: E402  (needs the sys.path insert above)
    from api.db import db  # noqa: E402
    from api.models import RegistryConnection  # noqa: E402
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

        # --- registry -------------------------------------------------------
        if service.registry_connection_id and not args.registry:
            print("  registry: already linked, left alone")
        else:
            registries = (
                RegistryConnection.query.filter_by(enabled=True)
                .order_by(RegistryConnection.id.asc())
                .all()
            )
            if not args.registry:
                # The one the Jenkins job pushed to, when it is recognisable.
                matches = [r for r in registries if REGISTRY_HINT in (r.base_url or "")]
                if len(matches) == 1:
                    registries = matches
            registry = _pick_one(registries, args.registry, "registry", "--registry")
            if registry is not None:
                service.registry_connection_id = registry.id
                db.session.add(service)
                db.session.commit()
                print("  registry: linked to #%s %s" % (registry.id, registry.base_url))

        # --- secrets (references must resolve before the pipeline saves) -----
        existing_keys = {row["key"] for row in secrets_service.list_secrets(service.id)}
        for key, value in PLACEHOLDER_SECRETS.items():
            if key in existing_keys:
                print("  secret %s: already set, left alone" % key)
                continue
            secrets_service.create_secret({"key": key, "value": value}, service_id=service.id)
            print("  secret %s: created as %s - replace it before deploying" % (key, value))

        payload = {
            "name": "default",
            "description": "Ported from the VERTO-ISSUING-WEB-APPLICATION Jenkins pipeline.",
            "isDefault": True,
            "enabled": True,
            "parameters": PARAMETERS,
            "stages": STAGES,
        }

        pipeline = next((p for p in service.pipelines if p.name == "default"), None)
        if pipeline is None:
            pipelines_service.create_pipeline(service, payload)
            print("  pipeline: created")
        elif _stage_signature(pipeline.stages) != _stage_signature(STAGES) and not args.force:
            print(
                "  pipeline: left alone - its stages differ from this file "
                "(renamed, reordered, added or removed). Re-run with --force to replace them."
            )
        else:
            pipelines_service.update_pipeline(pipeline, payload)
            print("  pipeline: updated")

    print(
        "\nBefore the first build, in the Service Catalog:\n"
        "  1. Source - a Bitbucket credential profile that can read areebasal/issuing-app.\n"
        "  2. Settings - the registry.areeba.com:9443 connection.\n"
        "  3. Secrets - real values for DEPLOY_FILES_USER, DEPLOY_FILES_TOKEN, ANSIBLE_SSH_KEY.\n"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
