"""What KubeSight will accept from a pipeline it did not write.

Something else proposes a pipeline — today Hermes, tomorrow whatever else — and
this module decides whether it may exist. It is the security boundary of that
whole feature, and it is deliberately the *only* boundary: everything upstream
of it is advice, and everything downstream of it is ordinary KubeSight.

Three properties make that safe to say.

**It validates a dict.** Nothing here imports Hermes, an LLM client, or any AI
code path — which is why it can live inside ``services/ci`` beside the engine
without breaking that package's promise. Hand it a pipeline payload from
anywhere and it answers the same way.

**It rejects rather than repairs.** A proposal quietly rewritten into something
saveable is a proposal nobody reviewed: it passes the human's glance precisely
because it looks like what they asked for. Every problem is reported with the
stage it is about and goes back to the generator to fix, or to the person.

**Unknown fields are fatal.** This is what makes ``privileged: true``,
``hostPath``, ``serviceAccount`` and every other field a generator might invent
a hard failure instead of something silently dropped. A stage model that has no
field for an escalation cannot carry one, and refusing unknown keys is what
keeps that true as the model grows.

Errors block. Warnings do not — they are things a person should see before
approving (an unconfigured build environment, a stage type with no executor yet)
but which are legitimately what the user asked for.
"""

from __future__ import annotations

import re
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple

from ...models_ci import RUNNER_TYPES, STAGE_TYPES, CiRunner
from ..application_intelligence_security import (
    GENERIC_SECRET_ASSIGNMENT_PATTERN,
    PEM_PRIVATE_KEY_PATTERN,
    SECRET_ASSIGNMENT_PATTERN,
    URL_CREDENTIAL_PATTERN,
    validate_relative_path,
)
from . import build_environments, pipelines, portability
from .runners.base import available_runner_types, capabilities_cover

# Everything a stage payload may contain. `id` and `position` are accepted and
# ignored so an edited draft can be re-validated without stripping them first;
# `image` is accepted only to be REFUSED with a useful message, because a
# generator that names an image needs to hear why rather than "unknown field".
STAGE_KEYS = frozenset(
    {
        "name",
        "stageType",
        "runnerType",
        "runnerLabels",
        "buildEnvironment",
        "image",
        "workingDirectory",
        "commands",
        "env",
        "secretRefs",
        "artifacts",
        "resources",
        "hostAliases",
        "runCondition",
        "timeoutSeconds",
        "continueOnFailure",
        "parallelGroup",
        "enabled",
        "id",
        "position",
    }
)
PIPELINE_KEYS = frozenset(
    {"name", "description", "isDefault", "enabled", "parameters", "stages"}
)
IGNORED_STAGE_KEYS = frozenset({"id", "position"})

# Stage types that validate today but have no executor yet. A pipeline may
# contain them — the engine skips them with an explanation, which is a
# deliberate product decision — so this is a warning, never an error.
_NOT_YET_EXECUTABLE = frozenset({"publish_artifact", "scan"})

MAX_STAGES = pipelines.MAX_STAGES


def _issue(code: str, message: str, *, stage: str = "", field: str = "") -> Dict[str, str]:
    return {"code": code, "stage": stage, "field": field, "message": message}


# ---------------------------------------------------------------------------
# Embedded credentials
# ---------------------------------------------------------------------------

# A value that is nothing but a shell variable reference is the CORRECT way to
# use a secret — `NEXUS_PASSWORD=$NEXUS_PASSWORD` is a stage wiring an injected
# secret into a tool, not a leak. Only literals are refused.
_PURE_VARIABLE = re.compile(r"^[\"']?\$(?:\{[A-Za-z_][A-Za-z0-9_]*\}|[A-Za-z_][A-Za-z0-9_]*)[\"']?$")

# The shared redaction patterns are tuned for CONFIGURATION FILES: an uppercase
# assignment at the start of a line, or a quoted value. A build command is
# neither. `./gradlew build -PnexusPassword=hunter2` is lowercase, mid-line and
# unquoted, and it is by some distance the most common way a credential ends up
# hardcoded in CI — so commands get a pattern of their own rather than a
# loosened version of the shared ones, which are also what redacts evidence on
# the way out to a model.
_BUILD_FLAG_SECRET = re.compile(
    r"""(?ix)
    (?:^|[\s"'])
    (?:-P|-D|--|-)?
    (?P<key>[A-Za-z0-9_.\-]*
      (?:password|passwd|secret|token|api[_-]?key|private[_-]?key|credential|access[_-]?key)
     [A-Za-z0-9_.\-]*)
    \s*=\s*
    (?P<value>[^\s"']+|"[^"]*"|'[^']*')
    """
)


def _is_reference(value: str) -> bool:
    return bool(_PURE_VARIABLE.match(str(value or "").strip()))


def embedded_secret(text: str) -> Optional[str]:
    """The name of the credential a literal embeds, or None.

    Detection reuses the same patterns that redact evidence on the way OUT to a
    model — one definition of "this looks like a credential", used in both
    directions.
    """
    body = str(text or "")
    if PEM_PRIVATE_KEY_PATTERN.search(body):
        return "a private key"
    match = URL_CREDENTIAL_PATTERN.search(body)
    if match and not _is_reference(match.group(3)):
        return "a credential inside a URL"
    for pattern in (SECRET_ASSIGNMENT_PATTERN, GENERIC_SECRET_ASSIGNMENT_PATTERN):
        for found in pattern.finditer(body):
            value = found.group(2)
            if value and not _is_reference(value):
                name = found.group(1).strip().rstrip(":=").strip().strip("\"'")
                return name or "a credential"
    for found in _BUILD_FLAG_SECRET.finditer(body):
        if not _is_reference(found.group("value")):
            return found.group("key")
    return None


# ---------------------------------------------------------------------------
# Runner reachability
# ---------------------------------------------------------------------------

def _registered_runners() -> List[CiRunner]:
    """Every runner KubeSight knows about, whatever state it is in.

    Deliberately not ``scheduler.eligible_runners()``, and deliberately not
    filtered on ``enabled`` either. A pipeline is being DESIGNED here, not
    dispatched, and the two states that are not "ready right now" are both
    normal:

    * The Mac agent is asleep at 2am. Refusing to design an iOS pipeline
      because of that would make the feature useless on the fleets it helps most.
    * The built-in Kubernetes runner ships DISABLED until an operator applies
      its manifest. On a fresh installation that is every capability it
      advertises, so treating disabled as absent would mean no pipeline could
      be generated until the fleet was fully set up — exactly backwards.

    So only "nothing in the fleet has ever heard of this capability" is an
    error. The rest is something to tell somebody.
    """
    return CiRunner.query.all()


def _label_coverage(labels: Iterable[str]) -> Tuple[str, Set[str]]:
    """``(state, labels nobody advertises)``.

    State is the best any covering runner manages: ``online`` (will run),
    ``offline`` (enabled, will queue until it checks in), ``disabled``
    (registered but switched off), or ``none`` (nothing covers it).
    """
    wanted = [str(item).strip().lower() for item in (labels or []) if str(item).strip()]
    if not wanted:
        return "online", set()
    runners = _registered_runners()
    covering = [r for r in runners if capabilities_cover(r.capabilities, wanted)]
    advertised: Set[str] = set()
    for runner in runners:
        advertised.update(
            str(c).strip().lower() for c in (runner.capabilities or []) if str(c).strip()
        )
    missing = set(wanted) - advertised
    if not covering:
        return "none", missing
    if any(r.enabled and r.status == "online" for r in covering):
        return "online", missing
    if any(r.enabled for r in covering):
        return "offline", missing
    return "disabled", missing


# ---------------------------------------------------------------------------
# Stage validation
# ---------------------------------------------------------------------------

def _resolve_build_environment(
    stage: Dict[str, Any], name: str, errors: List[Dict], warnings: List[Dict]
) -> Dict[str, Any]:
    """Turn a requested environment key into an image and runner labels.

    This is the substitution that keeps image choice inside KubeSight: the
    proposal says what it needs, and the answer comes from the catalog.
    """
    resolved = dict(stage)
    key = str(stage.get("buildEnvironment") or "").strip()
    literal = str(stage.get("image") or "").strip()

    if literal and not build_environments.is_permitted_image(literal):
        errors.append(
            _issue(
                "image_not_permitted",
                f"Stage '{name}' names the container image '{literal}'. A generated "
                "pipeline may not choose its own image — request a build environment "
                "instead, and KubeSight resolves the approved image for it.",
                stage=name,
                field="image",
            )
        )
        resolved.pop("image", None)

    if not key:
        resolved.pop("buildEnvironment", None)
        return resolved

    environment = build_environments.resolve(key)
    if environment is None:
        available = ", ".join(sorted(build_environments.ENVIRONMENTS))
        errors.append(
            _issue(
                "unknown_build_environment",
                f"Stage '{name}' asks for build environment '{key}', which is not in "
                f"the catalog. Available: {available}.",
                stage=name,
                field="buildEnvironment",
            )
        )
        resolved.pop("buildEnvironment", None)
        return resolved

    if not environment["configured"]:
        warnings.append(
            _issue(
                "build_environment_unconfigured",
                f"Stage '{name}' uses the '{environment['label']}' environment, which "
                "this installation has not pointed at an image yet. The stage will run "
                "on whatever the runner provides. Set its image in the build "
                "environment catalog before relying on this pipeline.",
                stage=name,
                field="buildEnvironment",
            )
        )

    if environment["image"] and not literal:
        resolved["image"] = environment["image"]
    # The environment's labels are ADDED to whatever the proposal asked for,
    # never substituted: the proposal may legitimately need more (a build cache,
    # a specific site), and dropping those would silently change where it runs.
    merged = list(resolved.get("runnerLabels") or [])
    for label in environment["labels"]:
        if label not in merged:
            merged.append(label)
    resolved["runnerLabels"] = merged
    if environment["runnerType"] and not str(resolved.get("runnerType") or "").strip():
        resolved["runnerType"] = environment["runnerType"]
    resolved.pop("buildEnvironment", None)
    return resolved


def _check_paths(stage: Dict[str, Any], name: str, errors: List[Dict]) -> None:
    working = str(stage.get("workingDirectory") or "").strip()
    if working and not working.startswith("$"):
        try:
            validate_relative_path(working, "Working directory")
        except ValueError:
            errors.append(
                _issue(
                    "path_escape",
                    f"Stage '{name}' has the working directory '{working}'. It must be "
                    "relative to the checkout, or a $KUBESIGHT_ variable — never an "
                    "absolute path, which is not the same directory on every runner.",
                    stage=name,
                    field="workingDirectory",
                )
            )
    for spec in stage.get("artifacts") or []:
        if not isinstance(spec, dict):
            continue
        path = str(spec.get("path") or "").strip()
        if not path:
            continue
        if path.startswith("/") or ".." in path.replace("\\", "/").split("/"):
            errors.append(
                _issue(
                    "path_escape",
                    f"Stage '{name}' collects the artifact '{path}'. Artifact paths are "
                    "relative to the workspace and may not step outside it.",
                    stage=name,
                    field="artifacts",
                )
            )


def _check_embedded_secrets(stage: Dict[str, Any], name: str, errors: List[Dict]) -> None:
    for key, value in (stage.get("env") or {}).items():
        found = embedded_secret(f"{key}={value}")
        if found:
            errors.append(
                _issue(
                    "embedded_secret",
                    f"Stage '{name}' sets the environment variable '{key}' to what looks "
                    "like a literal credential. Declare it as a required secret and "
                    "reference it, so the value is stored encrypted and masked out of "
                    "the build log.",
                    stage=name,
                    field="env",
                )
            )
            break
    for line in stage.get("commands") or []:
        found = embedded_secret(str(line))
        if found:
            errors.append(
                _issue(
                    "embedded_secret",
                    f"Stage '{name}' has a command containing {found} as a literal. "
                    "Reference a secret instead — a literal reaches the build log and "
                    "the pipeline definition in plain text.",
                    stage=name,
                    field="commands",
                )
            )
            break


def _check_runner(stage: Dict[str, Any], name: str, errors: List[Dict], warnings: List[Dict]) -> None:
    runner_type = str(stage.get("runnerType") or "").strip().lower()
    if runner_type:
        if runner_type not in RUNNER_TYPES:
            return  # normalize_stage already reported it
        if runner_type not in available_runner_types():
            errors.append(
                _issue(
                    "runner_type_unavailable",
                    f"Stage '{name}' requires a '{runner_type}' runner, which this "
                    "KubeSight has no executor for.",
                    stage=name,
                    field="runnerType",
                )
            )
            return

    state, unknown = _label_coverage(stage.get("runnerLabels"))
    if state == "none":
        detail = ", ".join(sorted(unknown)) if unknown else "those capabilities"
        errors.append(
            _issue(
                "unsatisfiable_runner_labels",
                f"Stage '{name}' needs a runner providing {detail}, and no registered "
                "runner advertises that. The build would queue forever. Register a "
                "runner with that capability, or relax the stage's labels.",
                stage=name,
                field="runnerLabels",
            )
        )
    elif state == "offline":
        warnings.append(
            _issue(
                "runner_offline",
                f"Stage '{name}' can only run on a runner that is registered but "
                "currently offline. The build will queue until it checks in.",
                stage=name,
                field="runnerLabels",
            )
        )
    elif state == "disabled":
        warnings.append(
            _issue(
                "runner_disabled",
                f"Stage '{name}' can only run on a runner that is registered but "
                "switched off. Enable it before the first build, or the build queues "
                "with nothing able to take it.",
                stage=name,
                field="runnerLabels",
            )
        )


def _check_secret_refs(
    stage: Dict[str, Any], name: str, known: Set[str], declared: Set[str], errors: List[Dict]
) -> None:
    for ref in stage.get("secretRefs") or []:
        if isinstance(ref, str):
            ref = {"name": ref}
        if not isinstance(ref, dict):
            continue
        secret_name = str(ref.get("name") or "").strip()
        if not secret_name:
            continue
        if secret_name not in known and secret_name not in declared:
            errors.append(
                _issue(
                    "undeclared_secret_ref",
                    f"Stage '{name}' reads the secret '{secret_name}', which this service "
                    "does not have and which is not listed as a required input. Either "
                    "declare it as a required secret, or reference one that exists.",
                    stage=name,
                    field="secretRefs",
                )
            )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def validate(
    service,
    payload: Dict[str, Any],
    *,
    declared_inputs: Optional[Iterable[Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    """Whether this pipeline may be saved, and what is wrong with it if not.

    ``declared_inputs`` are the required inputs the proposal came with: a stage
    may reference a secret that does not exist YET as long as the proposal also
    asks the user to supply it, because that is precisely the flow — propose,
    collect, then save. A reference to neither is a dangling reference.

    Returns ``pipeline`` as the normalized, ready-to-save editor payload when
    valid, and None when it is not. There is no partial success.
    """
    errors: List[Dict[str, str]] = []
    warnings: List[Dict[str, str]] = []

    if not isinstance(payload, dict):
        return {
            "valid": False,
            "errors": [_issue("malformed", "The pipeline must be an object.")],
            "warnings": [],
            "pipeline": None,
        }

    unknown_top = sorted(set(payload) - PIPELINE_KEYS)
    if unknown_top:
        errors.append(
            _issue(
                "unknown_field",
                "The pipeline carries fields KubeSight does not have: "
                f"{', '.join(unknown_top)}.",
            )
        )

    raw_stages = payload.get("stages")
    if not isinstance(raw_stages, list) or not raw_stages:
        errors.append(
            _issue("no_stages", "The pipeline has no stages, so it would build nothing.")
        )
        return {"valid": False, "errors": errors, "warnings": warnings, "pipeline": None}
    if len(raw_stages) > MAX_STAGES:
        errors.append(
            _issue(
                "too_many_stages",
                f"The pipeline has {len(raw_stages)} stages; the limit is {MAX_STAGES}.",
            )
        )
        raw_stages = raw_stages[:MAX_STAGES]

    try:
        parameters = pipelines._parameters(payload.get("parameters"))
    except pipelines.PipelineError as exc:
        errors.append(_issue("invalid_parameter", str(exc), field="parameters"))
        parameters = []

    known_keys = pipelines._known_secret_keys(service.id) if service is not None else set()
    declared = {
        str(item.get("name") or "").strip()
        for item in (declared_inputs or [])
        if isinstance(item, dict) and str(item.get("kind") or "") == "secret"
    }

    resolved_stages: List[Dict[str, Any]] = []
    names_seen: List[str] = []

    for index, raw in enumerate(raw_stages):
        if not isinstance(raw, dict):
            errors.append(
                _issue("malformed", f"Stage {index + 1} is not an object.")
            )
            continue
        name = " ".join(str(raw.get("name") or "").split())[:120] or f"Stage {index + 1}"

        unknown = sorted(set(raw) - STAGE_KEYS)
        if unknown:
            errors.append(
                _issue(
                    "unknown_field",
                    f"Stage '{name}' carries fields KubeSight has no place for: "
                    f"{', '.join(unknown)}. Anything not in the stage model is refused "
                    "rather than ignored.",
                    stage=name,
                )
            )
            continue

        stage = {k: v for k, v in raw.items() if k not in IGNORED_STAGE_KEYS}
        stage = _resolve_build_environment(stage, name, errors, warnings)

        # Structural validation is the SAME function that guards a hand-written
        # save, so a generated pipeline can never be held to a weaker standard
        # than one a person typed.
        try:
            pipelines.normalize_stage(stage, index, known_keys | declared)
        except pipelines.PipelineError as exc:
            errors.append(_issue("invalid_stage", str(exc), stage=name))
            continue

        stage_type = str(stage.get("stageType") or "command").strip().lower()
        if stage_type in _NOT_YET_EXECUTABLE:
            warnings.append(
                _issue(
                    "stage_type_not_executable",
                    f"Stage '{name}' is a {stage_type.replace('_', ' ')} stage, which has "
                    "no executor yet. It is skipped with an explanation rather than run.",
                    stage=name,
                    field="stageType",
                )
            )
        if stage_type == "container_image" and service is not None:
            if not getattr(service, "registry_connection_id", None):
                warnings.append(
                    _issue(
                        "container_image_without_registry",
                        f"Stage '{name}' builds a container image, but this service has no "
                        "registry connection to push it to. Link one before the first "
                        "build, or the stage is skipped.",
                        stage=name,
                    )
                )

        _check_paths(stage, name, errors)
        _check_embedded_secrets(stage, name, errors)
        _check_runner(stage, name, errors, warnings)
        _check_secret_refs(stage, name, known_keys, declared, errors)

        lowered = name.lower()
        if lowered in names_seen:
            errors.append(
                _issue(
                    "duplicate_stage_name",
                    f"Two stages are both called '{name}'. Stage names must be unique.",
                    stage=name,
                )
            )
        names_seen.append(lowered)
        resolved_stages.append(stage)

    # Runner portability, on the stages as KubeSight resolved them. Its error
    # findings are genuine "cannot work there" facts (docker in a build pod, an
    # absolute /workspace on an agent), so they block.
    lint = portability.analyze(
        [{**stage, "position": index} for index, stage in enumerate(resolved_stages)]
    )
    for finding in lint["findings"]:
        entry = _issue(
            f"portability_{finding['code']}",
            f"{finding['message']} {finding['fix']}".strip(),
            stage=finding.get("stageName") or "",
        )
        if finding["level"] == portability.ERROR:
            errors.append(entry)
        elif finding["level"] == portability.WARNING:
            warnings.append(entry)

    if errors:
        return {"valid": False, "errors": errors, "warnings": warnings, "pipeline": None}

    return {
        "valid": True,
        "errors": [],
        "warnings": warnings,
        "pipeline": {
            "name": " ".join(str(payload.get("name") or "default").split())[:120] or "default",
            "description": " ".join(str(payload.get("description") or "").split())[:2000],
            "isDefault": True,
            "enabled": True,
            "parameters": parameters,
            "stages": resolved_stages,
        },
    }


def error_feedback(errors: Iterable[Dict[str, str]]) -> List[Dict[str, str]]:
    """Validation errors reduced to what may be sent back to a generator.

    Codes, stage names and messages only. Nothing from the repository, nothing
    from a secret, and nothing that was not already said to the user — a repair
    prompt must not become a second, quieter channel out of KubeSight.
    """
    return [
        {
            "code": item.get("code", ""),
            "stage": item.get("stage", ""),
            "field": item.get("field", ""),
            "message": item.get("message", ""),
        }
        for item in errors or []
    ]
