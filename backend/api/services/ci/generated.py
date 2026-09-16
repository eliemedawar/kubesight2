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

**It advises rather than refuses.** Under the default ``advise`` policy nothing
a generator proposes is thrown away: the pipeline is normalized into the nearest
form KubeSight can actually store, and everything that had to change — or that
will bite later — is reported against the stage it belongs to and shown in the
review screen. The person approving the pipeline is the gate.

That is a deliberate trade and worth being plain about. A stage labelled for a
runner nobody has still queues forever; a ``docker build`` in a build pod still
finds no socket. Refusing those caught them a few seconds earlier than the build
would have. What refusing ALSO did was throw away pipelines that were entirely
correct apart from one detail, and that is the worse of the two failures.

**Nothing is silently granted.** The privilege fields are why this is safe
rather than merely permissive: ``CiPipelineStage`` has no column for
``privileged``, ``hostPath`` or ``serviceAccount``, so a proposal asking for one
is not obeyed whatever this module decides. Letting them through drops them and
says so. Refusing was only ever a way of telling somebody.

``CI_ASSIST_POLICY=enforce`` restores hard refusals for an installation that
wants the stricter gate. Every check keeps its severity in both modes — the two
differ in what BLOCKS, never in what is noticed.
"""

from __future__ import annotations

import os
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

# Other spellings of fields KubeSight already has.
#
# Refusing `type` because the field is called `stageType` blocks a pipeline that
# was entirely correct over a synonym, which is not a security boundary — it is
# a vocabulary mismatch. Accepting the synonym grants no capability that
# `stageType` did not already grant, so it is normalized rather than rejected.
# snake_case appears for the same reason: the model sees the field names in a
# Python-shaped world and reaches for them.
FIELD_ALIASES = {
    "type": "stageType",
    "stage_type": "stageType",
    "runner_type": "runnerType",
    "runner_labels": "runnerLabels",
    "labels": "runnerLabels",
    "build_environment": "buildEnvironment",
    "environment": "buildEnvironment",
    "working_directory": "workingDirectory",
    "workdir": "workingDirectory",
    "script": "commands",
    "secret_refs": "secretRefs",
    "secrets": "secretRefs",
    "host_aliases": "hostAliases",
    "run_condition": "runCondition",
    "timeout_seconds": "timeoutSeconds",
    "timeout": "timeoutSeconds",
    "continue_on_failure": "continueOnFailure",
    "parallel_group": "parallelGroup",
}

# Fields that are NOT a vocabulary mismatch.
#
# Every one of these names something a build stage in KubeSight cannot have and
# must not be able to acquire: a privilege, a host mount, an identity, a
# placement. There is no field to put them in, so dropping them would work —
# and that is exactly the problem. A proposal asking for root could then be
# approved by somebody reading a review screen that never showed it. These stay
# hard errors so the request is said out loud.
PRIVILEGE_FIELDS = frozenset(
    {
        "privileged", "privilegeescalation", "allowprivilegeescalation",
        "hostpath", "hostnetwork", "hostpid", "hostipc", "hostports",
        "securitycontext", "podsecuritycontext", "capabilities", "seccompprofile",
        "serviceaccount", "serviceaccountname", "automountserviceaccounttoken",
        "nodeselector", "nodename", "tolerations", "affinity",
        "volumes", "volumemounts", "mounts", "devices",
        "runasuser", "runasgroup", "fsgroup", "sysctls",
        "imagepullsecrets", "dockersocket", "namespace", "rbac",
    }
)

# Stage types that validate today but have no executor yet. A pipeline may
# contain them — the engine skips them with an explanation, which is a
# deliberate product decision — so this is a warning, never an error.
_NOT_YET_EXECUTABLE = frozenset({"publish_artifact", "scan"})

MAX_STAGES = pipelines.MAX_STAGES

# How a problem is treated once it has been found.
#
# "advise" (the default): nothing is refused. The proposal is normalized into
# what KubeSight can store and every problem is reported for a person to weigh
# in the review screen.
# "enforce": a problem blocks, as it did before — for an installation that would
# rather not save a pipeline it can already see is wrong.
POLICY_ADVISE = "advise"
POLICY_ENFORCE = "enforce"


def policy() -> str:
    chosen = os.getenv("CI_ASSIST_POLICY", POLICY_ADVISE).strip().lower()
    return POLICY_ENFORCE if chosen == POLICY_ENFORCE else POLICY_ADVISE


def _issue(code: str, message: str, *, stage: str = "", field: str = "") -> Dict[str, str]:
    return {"code": code, "stage": stage, "field": field, "message": message}


def _apply_aliases(raw: Dict[str, Any]) -> Tuple[Dict[str, Any], List[str]]:
    """Rewrite known synonyms onto the field they mean.

    A key the stage model already defines always wins, so an alias can never
    overwrite an explicit value — ``{"stageType": "command", "type": "scan"}``
    stays a command stage and reports the ignored ``type``.
    """
    out: Dict[str, Any] = {}
    renamed: List[str] = []
    for key, value in raw.items():
        target = FIELD_ALIASES.get(key)
        if target and target not in raw:
            out[target] = value
            renamed.append(f"{key} -> {target}")
        else:
            out[key] = value
    return out, renamed


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

def _storable(stage: Dict[str, Any], name: str, notes: List[Dict[str, str]]) -> Dict[str, Any]:
    """Bend a stage into something ``pipelines`` will actually accept.

    Only ever applied in ``advise`` mode, and only to the handful of things that
    would otherwise make the SAVE fail rather than merely be unwise: a timeout
    outside the allowed range, and a command stage with nothing to run. Both are
    reported. Everything else is left exactly as proposed — the point is to stop
    losing a pipeline over a detail, not to quietly redesign it.
    """
    out = dict(stage)

    raw_timeout = out.get("timeoutSeconds")
    try:
        timeout = int(raw_timeout) if raw_timeout not in (None, "") else 1800
    except (TypeError, ValueError):
        timeout = 1800
    clamped = max(pipelines.MIN_TIMEOUT_SECONDS, min(timeout, pipelines.MAX_TIMEOUT_SECONDS))
    if clamped != timeout:
        notes.append(
            _issue(
                "timeout_clamped",
                f"Stage '{name}' asked for a {timeout}s timeout; KubeSight allows "
                f"{pipelines.MIN_TIMEOUT_SECONDS}s to "
                f"{pipelines.MAX_TIMEOUT_SECONDS // 3600} hours, so it was set to "
                f"{clamped}s.",
                stage=name,
                field="timeoutSeconds",
            )
        )
    out["timeoutSeconds"] = clamped

    stage_type = str(out.get("stageType") or "command").strip().lower()
    if stage_type == "command" and not [
        line for line in (out.get("commands") or []) if str(line).strip()
    ]:
        # A command stage with nothing in it cannot be saved and would do
        # nothing if it could. Disabled keeps it visible in the editor — the
        # author can see what was proposed and fill it in — without pretending
        # a build ran it.
        out["enabled"] = False
        notes.append(
            _issue(
                "empty_command_stage",
                f"Stage '{name}' is a command stage with nothing to run. It was kept "
                "but switched off; add a command to enable it.",
                stage=name,
                field="commands",
            )
        )
    return out


def validate(
    service,
    payload: Dict[str, Any],
    *,
    declared_inputs: Optional[Iterable[Dict[str, Any]]] = None,
    enforce: Optional[bool] = None,
) -> Dict[str, Any]:
    """What is wrong with this pipeline, and whether that stops it being saved.

    ``declared_inputs`` are the required inputs the proposal came with: a stage
    may reference a secret that does not exist YET as long as the proposal also
    asks the user to supply it, because that is precisely the flow — propose,
    collect, then save. A reference to neither is a dangling reference.

    Under the default ``advise`` policy ``valid`` is true for anything KubeSight
    can store, and every objection travels in ``warnings`` for the review screen
    to show. Under ``enforce`` the objections block, and ``pipeline`` is None.
    ``enforce=True`` forces the strict answer regardless of the setting, which
    is what the repair loop uses to decide whether a correction round is worth
    asking for.
    """
    strict = policy() == POLICY_ENFORCE if enforce is None else bool(enforce)
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

        raw, renamed = _apply_aliases(raw)
        if renamed:
            warnings.append(
                _issue(
                    "field_renamed",
                    f"Stage '{name}' used other names for fields KubeSight already "
                    f"has ({', '.join(renamed)}); they were read as the fields they "
                    "mean.",
                    stage=name,
                )
            )

        unknown = sorted(set(raw) - STAGE_KEYS)
        privileged = [
            key for key in unknown if key.replace("_", "").lower() in PRIVILEGE_FIELDS
        ]
        if privileged:
            # Reported loudly, and then simply not obeyed. ``CiPipelineStage``
            # has no column for any of these, so the request cannot be honoured
            # whatever is decided here — which is why dropping it is the honest
            # outcome rather than a concession. Under enforce it still blocks,
            # for an installation that would rather see the proposal refused.
            note = _issue(
                "unknown_field",
                f"Stage '{name}' asks for {', '.join(privileged)}, which a KubeSight "
                "build stage cannot have. Build containers run as a non-root user "
                "with a read-only root filesystem and no host access, and that is "
                "not configurable from a pipeline. The request was ignored.",
                stage=name,
            )
            if strict:
                errors.append(note)
                continue
            warnings.append(note)

        dropped = [key for key in unknown if key not in privileged]
        if dropped:
            # Not refused. A pipeline that is otherwise correct must not be
            # thrown away over a field KubeSight simply does not have — but the
            # reviewer is told what was ignored, because "it did not do the
            # thing I asked for" is the failure that follows silence here.
            warnings.append(
                _issue(
                    "unsupported_field",
                    f"Stage '{name}' set {', '.join(dropped)}, which KubeSight has no "
                    "equivalent for. It was ignored — the stage will run without it.",
                    stage=name,
                )
            )

        stage = {
            k: v
            for k, v in raw.items()
            if k in STAGE_KEYS and k not in IGNORED_STAGE_KEYS
        }
        stage = _resolve_build_environment(stage, name, errors, warnings)
        if not strict:
            stage = _storable(stage, name, warnings)

        # Structural validation is the SAME function that guards a hand-written
        # save, so a generated pipeline is never held to a weaker standard than
        # one a person typed. What differs is the consequence: in advise mode a
        # stage that still will not normalize is dropped and reported, rather
        # than taking the whole pipeline down with it.
        try:
            pipelines.normalize_stage(stage, index, known_keys | declared)
        except pipelines.PipelineError as exc:
            if strict:
                errors.append(_issue("invalid_stage", str(exc), stage=name))
            else:
                warnings.append(
                    _issue(
                        "stage_dropped",
                        f"{exc} The stage was left out; the rest of the pipeline was "
                        "kept.",
                        stage=name,
                    )
                )
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

    # A pipeline that builds nothing.
    #
    # Checkout produces no artifact — it puts the source on disk for the stages
    # that follow. A pipeline consisting only of checkout stages is not a
    # minimal pipeline, it is a missing one, and it will report success while
    # having done nothing at all. That is the single worst outcome available
    # here, so it is said loudly even though the pipeline is storable.
    runnable = [
        stage
        for stage in resolved_stages
        if str(stage.get("stageType") or "command").strip().lower() != "checkout"
    ]
    if resolved_stages and not runnable:
        warnings.append(
            _issue(
                "builds_nothing",
                "This pipeline only checks the source out — no stage builds, tests or "
                "packages anything, so a build of it would report success having "
                "produced nothing. Add the build stage this project needs, or "
                "regenerate.",
            )
        )

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

    if not strict and errors:
        # Advise mode: the objections stand, they simply do not veto. They move
        # into the warning list so the review screen shows every one of them
        # next to the stage it is about, and the person decides.
        warnings = [*errors, *warnings]
        errors = []

    if errors or not resolved_stages:
        return {
            "valid": False,
            "errors": errors
            or [_issue("no_stages", "Nothing in this pipeline could be stored.")],
            "warnings": warnings,
            "pipeline": None,
        }

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
