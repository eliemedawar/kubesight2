"""Pipeline definitions: CRUD, validation, and template instantiation.

A pipeline is saved as a whole — name, flags, and the complete ordered stage
list in one request — matching how ``service_blueprint_service`` saves a
blueprint with its components. Per-stage endpoints would make reordering a
multi-request transaction the UI would have to get right on every drag.

Validation is strict about the two things that can hurt: a stage may not
reference a secret that does not exist, and a stage's declared runner type must
be one KubeSight knows. Everything else is normalized rather than rejected.
"""

from __future__ import annotations

import ipaddress
import re
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any, Dict, List, Optional, Tuple

from ...audit import log_audit
from ...db import db
from ...models_ci import (
    IMAGE_SCAN_ON_FAIL,
    IMAGE_SCAN_SEVERITIES,
    IMAGE_SCANNERS,
    PIPELINE_PURPOSES,
    RUNNER_TYPES,
    RETIRED_STAGE_TYPES,
    SAVEABLE_STAGE_TYPES,
    SERVER_STAGE_TYPES,
    STAGE_TYPES,
    CiPipeline,
    CiPipelineStage,
    CiSecret,
    CiService,
)
from . import build_inputs, code_scan, default_pipelines, deploy_config, jenkinsfile, scan_stage, templates
from . import approval_config, parallel_groups, store_upload_config
from . import resources as ci_resources
from .serializers import pipeline_to_dict

MAX_STAGES = 40
MAX_COMMANDS_PER_STAGE = 100
MAX_COMMAND_CHARS = 4000
MIN_TIMEOUT_SECONDS = 30
MAX_TIMEOUT_SECONDS = 24 * 3600


class PipelineError(ValueError):
    """A pipeline definition was rejected. Message is user-facing.

    ``code`` optionally names the rule, so the generated-pipeline validator can
    report it (and Hermes' repair loop act on it) instead of a generic
    ``invalid_stage``.
    """

    def __init__(self, message: str, *, code: Optional[str] = None):
        super().__init__(message)
        self.code = code


def _clean(value: Any, limit: int) -> str:
    return " ".join(str(value or "").split())[:limit]


def _string_list(value: Any, *, limit: int, item_limit: int) -> List[str]:
    if isinstance(value, str):
        items = [line for line in value.splitlines()]
    elif isinstance(value, (list, tuple)):
        items = list(value)
    else:
        items = []
    out: List[str] = []
    for item in items[:limit]:
        text = str(item or "").strip()
        if text:
            out.append(text[:item_limit])
    return out


PARAMETER_TYPES = ("text", "multiline", "choice", "boolean", "dynamic_choice")
# What a dynamic_choice can be filled from. Resolved server-side at run time so
# the Run Build dialog receives a ready list rather than discovering how to
# build one.
PARAMETER_SOURCES = ("branches", "tags", "branches_and_tags")

# Trigger metadata the ENGINE reads, not questions for a person. Deploy
# automation pins the image tag this way, and ``engine._registry_for`` reads
# IMAGE_NAME / IMAGE_TAG / DOCKERFILE_PATH off the same dict. They are accepted
# whatever the pipeline declares: otherwise adding a single build parameter to a
# pipeline would silently stop the automation that was already driving it, with
# "this pipeline has no parameter named 'IMAGE_TAG'" as the only explanation. A
# pipeline that declares one of these names by hand keeps its own definition —
# the pass-through only covers names it does not define.
RESERVED_VARIABLES = (
    "IMAGE_NAME",
    "IMAGE_TAG",
    "DOCKERFILE_PATH",
    "TICKET_TAG",
    "KUBESIGHT_TICKET",
)
MAX_PARAMETERS = 25
MAX_CHOICES = 100
# A single-line value is a branch name or a flag; a multiline one is a whole
# file — a Dockerfile, an nginx server block, a 40-line .env. The cap is on the
# document, not on the field, which is why the two differ by an order of
# magnitude.
MAX_TEXT_CHARS = 4000
MAX_MULTILINE_CHARS = 64000
# Values become environment variables for every stage, so a name has to be one.
_PARAM_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _parameter_choices(value: Any, name: str) -> List[str]:
    if isinstance(value, str):
        items = value.splitlines()
    elif isinstance(value, (list, tuple)):
        items = list(value)
    else:
        items = []
    cleaned: List[str] = []
    for item in items[:MAX_CHOICES]:
        text = str(item or "").strip()[:255]
        if text and text not in cleaned:
            cleaned.append(text)
    if not cleaned:
        raise PipelineError(f"Parameter '{name}' is a choice but lists no options.")
    return cleaned


def _parameters(value: Any) -> List[Dict[str, Any]]:
    """What a person is asked before a build starts.

    Rejected rather than repaired when malformed: a parameter whose definition
    is wrong produces a build configured differently from what was intended,
    which is worse than being told to fix the definition.
    """
    if value in (None, "", [], {}):
        return []
    if not isinstance(value, (list, tuple)):
        raise PipelineError("Build parameters must be a list.")
    if len(value) > MAX_PARAMETERS:
        raise PipelineError(f"A pipeline may not define more than {MAX_PARAMETERS} parameters.")

    out: List[Dict[str, Any]] = []
    seen = set()
    for entry in value:
        if not isinstance(entry, dict):
            raise PipelineError("Each build parameter must be an object.")
        name = _clean(entry.get("name"), 128)
        if not name:
            raise PipelineError("Every build parameter needs a name.")
        if not _PARAM_NAME_RE.match(name):
            raise PipelineError(
                f"Parameter name '{name}' is not usable as an environment variable "
                "— letters, digits and underscores only, not starting with a digit."
            )
        if name in seen:
            raise PipelineError(f"Build parameter '{name}' is defined twice.")
        seen.add(name)

        param_type = _clean(entry.get("type"), 24).lower() or "text"
        if param_type not in PARAMETER_TYPES:
            raise PipelineError(
                f"Parameter '{name}' has unknown type '{param_type}'. "
                f"Use one of: {', '.join(PARAMETER_TYPES)}."
            )

        param: Dict[str, Any] = {
            "name": name,
            "type": param_type,
            "label": _clean(entry.get("label"), 160) or name,
            "description": _clean(entry.get("description"), 500) or "",
            "required": bool(entry.get("required")),
        }

        if param_type == "boolean":
            # Stored as the strings a shell sees, since that is what a stage gets.
            param["default"] = "true" if _truthy(entry.get("default")) else "false"
            param["required"] = False  # A checkbox always has a value.
        elif param_type == "choice":
            param["choices"] = _parameter_choices(entry.get("choices"), name)
            default = _clean(entry.get("default"), 255)
            if default and default not in param["choices"]:
                raise PipelineError(
                    f"Parameter '{name}' defaults to '{default}', which is not one of its options."
                )
            param["default"] = default or param["choices"][0]
        elif param_type == "dynamic_choice":
            source = _clean(entry.get("source"), 32).lower() or "branches"
            if source not in PARAMETER_SOURCES:
                raise PipelineError(
                    f"Parameter '{name}' has unknown source '{source}'. "
                    f"Use one of: {', '.join(PARAMETER_SOURCES)}."
                )
            param["source"] = source
            # No validation against the live list: it is resolved at run time and
            # a repository that is briefly unreachable must not invalidate a
            # saved pipeline.
            param["default"] = _clean(entry.get("default"), 255)
        elif param_type == "multiline":
            # Newlines are the point: this is a file the build writes out, so
            # it is never collapsed or stripped the way _clean() would.
            param["default"] = str(entry.get("default") or "")[:MAX_MULTILINE_CHARS]
        else:
            param["default"] = str(entry.get("default") or "")[:MAX_TEXT_CHARS]

        out.append(param)
    return out


def _truthy(value: Any) -> bool:
    if isinstance(value, str):
        return value.strip().lower() in ("1", "true", "yes", "on")
    return bool(value)


def _command_lines(value: Any) -> List[str]:
    """Stage commands, kept as the author wrote them.

    These lines are joined back into one shell script, so unlike a list of
    labels they are whitespace-sensitive: a heredoc that writes a Dockerfile or
    a properties file breaks if interior blank lines vanish or leading
    indentation is stripped. Only trailing whitespace is removed, and only
    leading/trailing blank lines are dropped — an entirely empty list still
    fails the "command stage with no commands" check above.
    """
    if isinstance(value, str):
        items = value.splitlines()
    elif isinstance(value, (list, tuple)):
        items = list(value)
    else:
        items = []
    lines = [str(item or "").rstrip()[:MAX_COMMAND_CHARS] for item in items[:MAX_COMMANDS_PER_STAGE]]
    while lines and not lines[0].strip():
        lines.pop(0)
    while lines and not lines[-1].strip():
        lines.pop()
    return lines


def _label_list(value: Any) -> List[str]:
    seen: List[str] = []
    for item in _string_list(value, limit=20, item_limit=64):
        label = item.strip().lower()
        if label and label not in seen:
            seen.append(label)
    return seen


def _env_map(value: Any) -> Dict[str, str]:
    if not isinstance(value, dict):
        return {}
    out: Dict[str, str] = {}
    for key, raw in list(value.items())[:100]:
        name = _clean(key, 128)
        if name:
            out[name] = str(raw if raw is not None else "")[:4000]
    return out


def _secret_refs(value: Any, known_keys: set) -> List[Dict[str, str]]:
    """Normalize ``[{name, envVar}]`` and reject unknown secret names.

    Failing here rather than at build time means a pipeline that references a
    deleted secret is caught while someone is looking at the editor.
    """
    if not isinstance(value, (list, tuple)):
        return []
    out: List[Dict[str, str]] = []
    for item in value[:50]:
        if isinstance(item, str):
            item = {"name": item}
        if not isinstance(item, dict):
            continue
        name = _clean(item.get("name"), 120)
        if not name:
            continue
        if name not in known_keys:
            raise PipelineError(
                f"Stage references secret '{name}', which is not defined for this service."
            )
        out.append({"name": name, "envVar": _clean(item.get("envVar"), 128) or name})
    return out


def _artifact_specs(value: Any) -> List[Dict[str, Any]]:
    if not isinstance(value, (list, tuple)):
        return []
    out: List[Dict[str, Any]] = []
    for item in value[:20]:
        if not isinstance(item, dict):
            continue
        path = _clean(item.get("path"), 512)
        if not path:
            continue
        entry: Dict[str, Any] = {"path": path, "type": _clean(item.get("type"), 32) or "binary"}
        name = _clean(item.get("name"), 255)
        if name:
            entry["name"] = name
        out.append(entry)
    return out


_HOSTNAME_RE = re.compile(
    r"^(?=.{1,253}$)[A-Za-z0-9]([A-Za-z0-9-]{0,61}[A-Za-z0-9])?"
    r"(\.[A-Za-z0-9]([A-Za-z0-9-]{0,61}[A-Za-z0-9])?)*$"
)

MAX_HOST_ALIASES = 20
MAX_HOSTNAMES_PER_ALIAS = 10


def _host_aliases(value: Any, stage_name: str) -> List[Dict[str, Any]]:
    """Extra /etc/hosts entries for the build pod.

    Accepts either the text form the editor shows (``ip=host,host`` per line)
    or the structured form the API round-trips (``[{"ip", "hostnames"}]``), so
    a saved stage reloads and re-saves unchanged.

    A bad entry raises rather than being dropped: silently ignoring a typo'd
    mapping would surface much later as a connect timeout inside a build tool,
    which is exactly the failure this field exists to prevent.
    """
    if value in (None, "", [], {}):
        return []

    entries: List[Any] = []
    if isinstance(value, str):
        entries = value.splitlines()
    elif isinstance(value, (list, tuple)):
        entries = list(value)
    else:
        raise PipelineError(f"Stage '{stage_name}' has malformed host aliases.")

    merged: List[Dict[str, Any]] = []
    by_ip: Dict[str, Dict[str, Any]] = {}

    for entry in entries[: MAX_HOST_ALIASES * MAX_HOSTNAMES_PER_ALIAS]:
        if isinstance(entry, dict):
            raw_ip = _clean(entry.get("ip"), 64)
            raw_names = entry.get("hostnames")
            names = (
                [str(name) for name in raw_names]
                if isinstance(raw_names, (list, tuple))
                else str(raw_names or "").split(",")
            )
        else:
            line = str(entry or "").strip()
            if not line:
                continue  # blank lines are formatting, not entries
            if "=" not in line:
                raise PipelineError(
                    f"Stage '{stage_name}' has a host alias without '=': '{line}'. "
                    "Use ip=hostname, for example 10.10.10.20=nexus.areeba.com."
                )
            raw_ip, _, raw_names = line.partition("=")
            raw_ip = raw_ip.strip()
            names = raw_names.split(",")

        if not raw_ip and not any(name.strip() for name in names):
            continue
        try:
            ip = str(ipaddress.ip_address(raw_ip))
        except ValueError:
            raise PipelineError(
                f"Stage '{stage_name}' has an invalid host alias IP address: '{raw_ip}'."
            )

        cleaned: List[str] = []
        for name in names:
            name = name.strip()
            if not name:
                continue
            if not _HOSTNAME_RE.match(name):
                raise PipelineError(
                    f"Stage '{stage_name}' has an invalid host alias hostname: '{name}'."
                )
            if name not in cleaned:
                cleaned.append(name)
        if not cleaned:
            raise PipelineError(
                f"Stage '{stage_name}' has a host alias for {ip} with no hostname."
            )

        # One entry per IP: Kubernetes accepts duplicates, but merging keeps the
        # generated pod spec readable and the round-trip stable.
        existing = by_ip.get(ip)
        if existing is None:
            existing = {"ip": ip, "hostnames": []}
            by_ip[ip] = existing
            merged.append(existing)
        for name in cleaned:
            if name not in existing["hostnames"]:
                existing["hostnames"].append(name)
        del existing["hostnames"][MAX_HOSTNAMES_PER_ALIAS:]

    if len(merged) > MAX_HOST_ALIASES:
        raise PipelineError(
            f"Stage '{stage_name}' may not define more than {MAX_HOST_ALIASES} host aliases."
        )
    return merged


CONDITION_OPERATORS = ("equals", "not_equals")


def _run_condition(value: Any, stage_name: str) -> Optional[Dict[str, str]]:
    """When this stage runs, or None for always.

    ``{"variable": "DEPLOY_UAT", "operator": "equals", "value": "true"}`` — the
    Jenkins ``when { equals expected: 'true', actual: DEPLOY_UAT }`` clause with
    the Groovy removed. Only build variables are readable, which is the whole
    point: a condition over an arbitrary expression would need an evaluator, and
    an evaluator over pipeline text is a shell of its own.

    Comparison is against the *string* the variable holds, because that is what
    a stage receives as environment. A boolean parameter is therefore matched
    with the value "true", not True.
    """
    if value in (None, "", {}, []):
        return None
    if not isinstance(value, dict):
        raise PipelineError(f"Stage '{stage_name}' has a malformed run condition.")

    variable = _clean(value.get("variable"), 128)
    if not variable:
        # An operator with nothing to compare is an editor half-filled, not a
        # condition — treated as "always", so an abandoned row saves cleanly.
        return None
    if not _PARAM_NAME_RE.match(variable):
        raise PipelineError(
            f"Stage '{stage_name}' has a run condition on '{variable}', which is not "
            "a usable variable name — letters, digits and underscores only."
        )

    operator = _clean(value.get("operator"), 24).lower() or "equals"
    if operator not in CONDITION_OPERATORS:
        raise PipelineError(
            f"Stage '{stage_name}' has an unknown run condition operator "
            f"'{operator}'. Use one of: {', '.join(CONDITION_OPERATORS)}."
        )

    return {
        "variable": variable,
        "operator": operator,
        "value": str(value.get("value") or "")[:255].strip(),
    }


# What may appear in a templated IMAGE_TAG. The template is expanded by the
# build's own shell, so it has to be safe there: no quotes, no $( ), no
# backticks, no semicolons. Everything a version string needs survives.
_IMAGE_TAG_TEMPLATE_RE = re.compile(r"^[A-Za-z0-9._${}-]{1,255}$")


def _check_image_tag_template(env: Dict[str, str], stage_name: str) -> None:
    """Reject an IMAGE_TAG whose ``${...}`` expansion could run a command.

    A tag containing ``$`` is resolved at build time against the variables an
    earlier stage exported (see KUBESIGHT_ENV), which means the string reaches a
    shell. Pipeline authors already run arbitrary commands in stages, so this is
    not a privilege boundary — it is a guard against a tag that silently becomes
    something other than a tag.
    """
    tag = env.get("IMAGE_TAG") or ""
    if "$" not in tag:
        return
    if not _IMAGE_TAG_TEMPLATE_RE.match(tag):
        raise PipelineError(
            f"Stage '{stage_name}' has an image tag template that is not a tag: "
            f"'{tag}'. Use letters, digits, dot, dash, underscore and ${{VARIABLE}}."
        )


def _check_stage_build_inputs(
    env: Dict[str, str], working_directory: Any, stage_name: str
) -> None:
    """The engine-read values a stage's own env sets, checked on save.

    A literal IMAGE_TAG / IMAGE_NAME here is still tidied by the engine (it has
    always been), so only the values that reach the build line verbatim —
    the Dockerfile path and the working directory — are held to a grammar.
    """
    dockerfile = env.get("DOCKERFILE_PATH") or ""
    if dockerfile:
        problem = build_inputs.dockerfile_path_problem(dockerfile)
        if problem:
            raise PipelineError(f"Stage '{stage_name}': {problem}", code="path_escape")
    problem = build_inputs.working_directory_problem(working_directory)
    if problem:
        raise PipelineError(f"Stage '{stage_name}': {problem}", code="path_escape")


def _image_scan(value: Any, stage_type: str, stage_name: str) -> Optional[Dict[str, Any]]:
    """Normalize a container_image stage's scan gate, or None.

    None and ``{"enabled": false}`` are different things and both are kept:
    None is "nobody has considered scanning this", false is "somebody looked at
    it and turned it off". The editor shows them differently and the stage log
    says which one it is, because "the scan did not run" needs a reason.

    Rejected on a non-image stage rather than ignored. Silently dropping a gate
    somebody configured is the one failure mode that matters here — they would
    believe the image was scanned.
    """
    if value in (None, "", {}):
        return None
    if not isinstance(value, dict):
        raise PipelineError(f"Stage '{stage_name}' has an invalid image scan configuration.")
    if stage_type != "container_image":
        raise PipelineError(
            f"Stage '{stage_name}' is a {stage_type} stage, so it builds no image to scan. "
            "Image scanning is configured on the container image stage that pushes it."
        )

    scanner = _clean(value.get("scanner"), 32).lower() or "trivy"
    if scanner not in IMAGE_SCANNERS:
        raise PipelineError(
            f"Stage '{stage_name}' names an unknown image scanner '{scanner}'. "
            f"Supported: {', '.join(IMAGE_SCANNERS)}."
        )

    threshold = _clean(value.get("threshold"), 16).lower() or "critical"
    if threshold not in IMAGE_SCAN_SEVERITIES:
        raise PipelineError(
            f"Stage '{stage_name}' has an unknown scan threshold '{threshold}'. "
            f"Supported: {', '.join(IMAGE_SCAN_SEVERITIES)}."
        )

    on_fail = _clean(value.get("onFail"), 16).lower() or "block"
    if on_fail not in IMAGE_SCAN_ON_FAIL:
        raise PipelineError(
            f"Stage '{stage_name}' has an unknown scan failure policy '{on_fail}'. "
            f"Supported: {', '.join(IMAGE_SCAN_ON_FAIL)}."
        )

    return {
        "enabled": value.get("enabled") is not False,
        "scanner": scanner,
        "threshold": threshold,
        "onFail": on_fail,
        # A CVE with no released fix cannot be acted on by rebuilding, so a gate
        # that counts it blocks a build nobody can unblock. Off by default all
        # the same: ignoring unfixed findings is a policy choice, not a default
        # KubeSight makes on somebody's behalf.
        "ignoreUnfixed": bool(value.get("ignoreUnfixed")),
    }


def _deploy(value: Any, stage_type: str, stage_name: str) -> Optional[Dict[str, Any]]:
    """A Deploy stage's target, or None. See ``deploy_config.normalize``."""
    try:
        return deploy_config.normalize(value, stage_type, stage_name)
    except deploy_config.DeployConfigError as exc:
        raise PipelineError(str(exc), code="invalid_deploy")


def _approval(value: Any, stage_type: str, stage_name: str) -> Optional[Dict[str, Any]]:
    """An Approval stage's approvers and rules, or None. See ``approval_config``.

    Named approvers are resolved here, against active users, so the stage
    stores their names for the drawer and can never wait on an account that
    could not log in to answer it.
    """
    known = None
    if stage_type == "approval" and isinstance(value, dict) and value.get("users"):
        from ...models import User

        ids = []
        for item in value.get("users") or []:
            raw = item.get("id") if isinstance(item, dict) else item
            try:
                ids.append(int(raw))
            except (TypeError, ValueError):
                continue
        rows = User.query.filter(User.id.in_(ids or [0])).all() if ids else []
        known = {row.id: row.username for row in rows if getattr(row, "is_active", True)}
    try:
        return approval_config.normalize(value, stage_type, stage_name, known_users=known)
    except approval_config.ApprovalConfigError as exc:
        raise PipelineError(str(exc), code="invalid_approval")


def _store_upload(value: Any, stage_type: str, stage_name: str) -> Optional[Dict[str, Any]]:
    """An App store upload stage's target, or None. See ``store_upload_config``."""
    try:
        return store_upload_config.normalize(value, stage_type, stage_name)
    except store_upload_config.StoreUploadConfigError as exc:
        raise PipelineError(str(exc), code="invalid_store_upload")


# Server stages other than Deploy run nothing in a container, so a command,
# image or file pattern sent with one was written by somebody expecting it to
# run. (Deploy predates this check and keeps accepting — and ignoring — them.)
_SERVER_KIND_LABELS = {"deploy": "Deploy", "approval": "Approval", "store_upload": "App store upload"}


def _server_stage_fields(payload: Dict[str, Any], stage_type: str, stage_name: str) -> None:
    label = _SERVER_KIND_LABELS.get(stage_type, stage_type)
    if _command_lines(payload.get("commands")) or _clean(payload.get("image"), 512):
        raise PipelineError(
            f"Stage '{stage_name}' is an {label} stage, which KubeSight runs itself — it has no "
            "container, so no image or commands. Remove them, or use a command stage.",
            code=f"invalid_{stage_type}",
        )
    if _artifact_specs(payload.get("artifacts")):
        raise PipelineError(
            f"Stage '{stage_name}' is an {label} stage and produces no files. Keep files on the "
            "stage that builds them.",
            code=f"invalid_{stage_type}",
        )


def _code_scan(value: Any, stage_type: str, stage_name: str) -> Optional[Dict[str, Any]]:
    """A command stage's code scan quality gate, or None. See ``code_scan.normalize``."""
    try:
        return code_scan.normalize(value, stage_type, stage_name)
    except code_scan.CodeScanConfigError as exc:
        raise PipelineError(str(exc), code="invalid_code_scan")


def _scan(
    payload: Dict[str, Any], stage_type: str, stage_name: str, known_keys: set
) -> Tuple[Optional[Dict[str, Any]], Optional[Dict[str, Any]]]:
    """A scan stage's scanner, and the code scan gate that goes with it.

    Returned together because they are decided together: a Semgrep scan stage
    always carries the quality gate (defaulted when absent), and a scan stage
    with any other tool may not carry it at all. See ``scan_stage``.
    """
    gate = _code_scan(payload.get("codeScan"), stage_type, stage_name)
    try:
        config = scan_stage.normalize(payload.get("scan"), stage_type, stage_name, known_keys)
        if config is None:
            return None, gate
        return config, scan_stage.gate_for(config, gate, stage_name)
    except scan_stage.ScanConfigError as exc:
        raise PipelineError(str(exc), code="invalid_scan")


def _scan_stage_fields(payload: Dict[str, Any], stage_name: str) -> None:
    """Refuse what a scan stage would silently ignore.

    Its image and script are generated from the scanner it names, and its
    report is collected by name — so commands, an image or file patterns sent
    with one were written by somebody who expects them to run.
    """
    if _command_lines(payload.get("commands")):
        raise PipelineError(
            f"Stage '{stage_name}' is a scan stage: KubeSight writes its script from the scanner "
            "it names. Remove the commands, or make it a command stage to run your own.",
            code="invalid_scan",
        )
    if _clean(payload.get("image"), 512):
        raise PipelineError(
            f"Stage '{stage_name}' is a scan stage, which runs in the scanner's approved image. "
            "Remove the image; an administrator repoints the scanner image for the installation.",
            code="invalid_scan",
        )
    if _artifact_specs(payload.get("artifacts")):
        raise PipelineError(
            f"Stage '{stage_name}' is a scan stage: its report is kept on the build "
            "automatically. Remove the files to keep.",
            code="invalid_scan",
        )


def _resources(value: Any, stage_name: str) -> Optional[Dict[str, str]]:
    """This stage's override of the service's Build resources.

    Same vocabulary as the service-level setting — a quantity, or "off" for no
    limit — because they are the same field at different scopes, and the runner
    reads whichever is nearest. See ``resources.py`` for how the layers resolve.
    """
    try:
        return ci_resources.normalize(value, source=f"Stage '{stage_name}' resources")
    except ci_resources.ResourceError as exc:
        raise PipelineError(str(exc))


def _known_secret_keys(service_id: int) -> set:
    rows = CiSecret.query.filter(
        db.or_(CiSecret.service_id == service_id, CiSecret.scope == "global")
    ).all()
    return {row.key for row in rows}


def _post_actions(value: Any, service_id: int) -> List[Dict[str, Any]]:
    """A pipeline's post actions (notifications + cleanup when a build ends),
    validated by services/ci/post_actions.py."""
    from . import post_actions

    try:
        return post_actions.normalize(value, _known_secret_keys(service_id))
    except post_actions.PostActionError as exc:
        raise PipelineError(str(exc), code="invalid_post_action")


def _parallel_group_name(value: Any, stage_name: str) -> Optional[str]:
    try:
        return parallel_groups.normalize_name(value)
    except parallel_groups.GroupError as exc:
        raise PipelineError(f"Stage '{stage_name}': {exc}", code=exc.code)


def check_parallel_groups(normalized: List[Dict[str, Any]]) -> None:
    """Every parallel group in the list follows the rules in parallel_groups.

    Canonicalizes as it goes: every member ends up with the first member's
    spelling of the name and one fail-fast switch for the group.
    """
    try:
        parallel_groups.validate(normalized)
    except parallel_groups.GroupError as exc:
        raise PipelineError(str(exc), code=exc.code)


def normalize_stage(payload: Dict[str, Any], position: int, known_keys: set) -> Dict[str, Any]:
    """Validate and normalize one stage payload into model kwargs."""
    name = _clean(payload.get("name"), 120)
    if not name:
        raise PipelineError(f"Stage {position + 1} needs a name.")

    stage_type = _clean(payload.get("stageType"), 32).lower() or "command"
    if stage_type in RETIRED_STAGE_TYPES:
        raise PipelineError(
            f"Stage '{name}' is a '{stage_type}' stage, which KubeSight has no "
            "executor for — a build would only ever skip it. "
            "Declare the files as artifacts on the stage that produces them "
            "and remove this stage."
        )
    if stage_type not in STAGE_TYPES:
        raise PipelineError(
            f"Stage '{name}' has an unknown type '{stage_type}'. "
            f"Supported: {', '.join(SAVEABLE_STAGE_TYPES)}."
        )

    runner_type = _clean(payload.get("runnerType"), 24).lower() or None
    if runner_type and runner_type not in RUNNER_TYPES:
        raise PipelineError(
            f"Stage '{name}' targets an unknown runner type '{runner_type}'."
        )

    commands = _command_lines(payload.get("commands"))
    if stage_type == "command" and not commands:
        raise PipelineError(f"Stage '{name}' is a command stage but has no commands.")
    if stage_type == "scan":
        _scan_stage_fields(payload, name)
    if stage_type in ("approval", "store_upload"):
        _server_stage_fields(payload, stage_type, name)
    scan_config, gate = _scan(payload, stage_type, name, known_keys)

    timeout = payload.get("timeoutSeconds")
    try:
        timeout = int(timeout) if timeout not in (None, "") else 1800
    except (TypeError, ValueError):
        raise PipelineError(f"Stage '{name}' has an invalid timeout.")
    if not MIN_TIMEOUT_SECONDS <= timeout <= MAX_TIMEOUT_SECONDS:
        raise PipelineError(
            f"Stage '{name}' timeout must be between {MIN_TIMEOUT_SECONDS} seconds "
            f"and {MAX_TIMEOUT_SECONDS // 3600} hours."
        )

    env = _env_map(payload.get("env"))
    _check_image_tag_template(env, name)
    _check_stage_build_inputs(env, payload.get("workingDirectory"), name)

    return {
        "position": position,
        "name": name,
        "stage_type": stage_type,
        "runner_type": runner_type,
        "runner_labels": _label_list(payload.get("runnerLabels")),
        "image": _clean(payload.get("image"), 512) or None,
        "working_directory": _clean(payload.get("workingDirectory"), 512) or None,
        "commands": commands,
        "env": env,
        "secret_refs": _secret_refs(payload.get("secretRefs"), known_keys),
        "artifacts": _artifact_specs(payload.get("artifacts")),
        "resources": _resources(payload.get("resources"), name),
        "host_aliases": _host_aliases(payload.get("hostAliases"), name),
        "run_condition": _run_condition(payload.get("runCondition"), name),
        "image_scan": _image_scan(payload.get("imageScan"), stage_type, name),
        "deploy": _deploy(payload.get("deploy"), stage_type, name),
        "approval": _approval(payload.get("approval"), stage_type, name),
        "store_upload": _store_upload(payload.get("storeUpload"), stage_type, name),
        "code_scan": gate,
        "scan": scan_config,
        "timeout_seconds": timeout,
        "continue_on_failure": bool(payload.get("continueOnFailure")),
        # Consecutive stages sharing a name run at the same time. Only the
        # name is cleaned here; whether the group is a valid one (consecutive,
        # runner stages, 2-8 members) is a property of the whole list, checked
        # by parallel_groups.validate in _apply_stages.
        "parallel_group": _parallel_group_name(payload.get("parallelGroup"), name),
        "parallel_fail_fast": bool(payload.get("parallelFailFast")),
        "enabled": payload.get("enabled") is not False,
    }


def check_deploy_stages_last(normalized: List[Dict[str, Any]]) -> None:
    """Server stages (Deploy, Approval, App store upload) come after every stage
    a runner executes.

    They run on the KubeSight server once the runner is done. A whole-build
    runner (one Kubernetes Job per build) has no way to pause its pod for a
    server-side step, so a stage placed after one would run before it — a
    smoke test passing against the version that was about to be replaced, or a
    "post-approval" step that ran before anybody approved. Server stages may
    follow each other in any order: an Approval before a Deploy is the point.
    """
    first_server = next(
        (s for s in normalized if s["stage_type"] in SERVER_STAGE_TYPES), None
    )
    if first_server is None:
        return
    after = [
        s for s in normalized
        if s["position"] > first_server["position"] and s["stage_type"] not in SERVER_STAGE_TYPES
    ]
    if after:
        kind = _SERVER_KIND_LABELS.get(first_server["stage_type"], first_server["stage_type"])
        raise PipelineError(
            f"Stage '{after[0]['name']}' comes after the {kind} stage '{first_server['name']}'. "
            f"{kind} stages run on the KubeSight server once the build itself has finished, so "
            f"they must be the last stages. Move it above the {kind} stage.",
            code="deploy_not_last" if first_server["stage_type"] == "deploy" else "server_stage_not_last",
        )


# The name the ordering rule has now that it covers every server stage kind.
check_server_stages_last = check_deploy_stages_last


def _stamp_deploy_authority(
    pipeline: CiPipeline, normalized: List[Dict[str, Any]], actor
) -> None:
    """Record who a Deploy stage deploys as — whoever last saved its target.

    A build is often started by a webhook or a ticket, with no person behind it,
    so the stage cannot borrow the permissions of whoever started it. Instead,
    pointing a stage at a namespace requires being able to deploy there, and
    that person's name is stamped on the target. Saving the pipeline for any
    other reason keeps the stamp; changing anything the stamp covers (see
    ``deploy_config.signature``) needs someone who could deploy it themselves.

    The stamp is never read from the request — only carried over from the
    stored stage with the same signature, or written here.
    """
    from ...access_engine import can_access_namespace, user_has_permission

    stored: Dict[str, Dict[str, Any]] = {}
    for row in pipeline.stages:
        config = row.deploy if isinstance(row.deploy, dict) else None
        if row.stage_type == "deploy" and config and isinstance(config.get("authorizedBy"), dict):
            stored.setdefault(deploy_config.signature(config), config["authorizedBy"])

    for stage in normalized:
        config = stage.get("deploy")
        if stage["stage_type"] != "deploy" or not config:
            continue
        kept = stored.get(deploy_config.signature(config))
        if kept:
            config["authorizedBy"] = dict(kept)
            continue
        if deploy_config.is_linked(config):
            _stamp_linked_deploy(pipeline, stage, config, actor)
            continue
        where = f"{config['clusterId']}/{config['namespace']}"
        if actor is None:
            raise PipelineError(
                f"Stage '{stage['name']}' deploys to {where}, and only a person who can deploy "
                "there can set that target.",
                code="deploy_not_authorized",
            )
        if not (
            user_has_permission(actor, "apps:deploy")
            and can_access_namespace(actor, config["clusterId"], config["namespace"])
        ):
            raise PipelineError(
                f"You cannot deploy to {where}, so you cannot point the stage "
                f"'{stage['name']}' there. Builds deploy with the rights of whoever saved the "
                "target — ask someone who can deploy to that namespace to set it.",
                code="deploy_not_authorized",
            )
        config["authorizedBy"] = {
            "userId": actor.id,
            "username": actor.username,
            "at": datetime.now(timezone.utc).isoformat(),
        }
        log_audit(
            "ci_deploy_target_authorized",
            actor=actor,
            target_type="ci_pipeline",
            target_id=str(pipeline.id),
            details={
                "stage": stage["name"],
                "cluster": config["clusterId"],
                "namespace": config["namespace"],
                "deployment": config["deploymentName"],
                "createIfMissing": config["createIfMissing"],
            },
            commit=False,
        )


def _check_deploy_templates(normalized: List[Dict[str, Any]]) -> None:
    """A Deploy stage that creates its deployment from an inventory template
    must name one a build can actually render — checked now, while someone is
    looking at the editor, rather than when the first deployment is missing."""
    from . import deploy_templates

    for stage in normalized:
        config = stage.get("deploy")
        if stage["stage_type"] != "deploy" or not config or stage.get("enabled") is False:
            continue
        if deploy_config.is_linked(config) or not config.get("createIfMissing"):
            continue
        if not deploy_templates.uses_template(config):
            continue
        try:
            deploy_templates.check(config, stage["name"])
        except deploy_templates.DeployTemplateError as exc:
            raise PipelineError(str(exc), code="invalid_deploy")
        config["create"]["templateName"] = deploy_templates.template_label(
            config["create"]["templateId"]
        )


def _stamp_linked_deploy(pipeline: CiPipeline, stage: Dict[str, Any], config: Dict[str, Any], actor) -> None:
    """A stage that deploys to the service's linked deployment.

    It has no namespace of its own to check: each build deploys where the
    building service's link points, with the rights of whoever made that link
    (re-checked at run time, see deployment_links.resolve_snapshot_targets).
    Turning a stage into one still needs someone who may deploy at all.
    """
    from ...access_engine import user_has_permission

    if actor is None or not user_has_permission(actor, "apps:deploy"):
        raise PipelineError(
            f"Stage '{stage['name']}' deploys to each service's linked deployment, and only "
            "someone who can deploy applications can set that.",
            code="deploy_not_authorized",
        )
    config["authorizedBy"] = {
        "userId": actor.id,
        "username": actor.username,
        "at": datetime.now(timezone.utc).isoformat(),
    }
    log_audit(
        "ci_deploy_target_authorized",
        actor=actor,
        target_type="ci_pipeline",
        target_id=str(pipeline.id),
        details={
            "stage": stage["name"],
            "target": "linked",
            "environment": config.get("environment") or "",
        },
        commit=False,
    )


def _default_store_upload_apps(pipeline: CiPipeline, normalized: List[Dict[str, Any]]) -> None:
    """Fill an App store upload stage's app with the one linked to this service.

    The editor pre-selects it; this makes an API or MCP save that leaves it out
    mean the same thing, and refuses one that names an app that is not there.
    """
    from ...models import MobileApplication

    for stage in normalized:
        config = stage.get("store_upload")
        if stage["stage_type"] != "store_upload" or not config:
            continue
        if config.get("appId"):
            if db.session.get(MobileApplication, int(config["appId"])) is None:
                raise PipelineError(
                    f"Stage '{stage['name']}' publishes mobile application #{config['appId']}, "
                    "which does not exist. Pick a registered app.",
                    code="invalid_store_upload",
                )
            continue
        linked = MobileApplication.query.filter_by(ci_service_id=pipeline.service_id).all()
        if len(linked) != 1:
            raise PipelineError(
                f"Stage '{stage['name']}' does not say which mobile application to publish"
                + (
                    f", and {len(linked)} are linked to this service. Pick one."
                    if linked
                    else ", and none is linked to this service. Register it under Mobile Apps "
                    "(linked to this CI service), or pick one."
                ),
                code="invalid_store_upload",
            )
        config["appId"] = linked[0].id


def _stamp_store_upload_authority(
    pipeline: CiPipeline, normalized: List[Dict[str, Any]], actor
) -> None:
    """Record who an App store upload stage publishes as — the administrator
    who last saved its target.

    The same rule as ``_stamp_deploy_authority``, with the permission the
    Mobile Apps publish route itself requires: publishing to a store is
    admin-only (``routes/mobile_apps.publish_build`` is ``@require_admin``), so
    pointing a stage at a store is too. Unrelated edits keep the stamp; a
    change to anything ``store_upload_config.signature`` covers needs an
    administrator. Never read from the request.
    """
    from ...access_engine import is_admin
    from ...models import MobileApplication

    stored: Dict[str, Dict[str, Any]] = {}
    for row in pipeline.stages:
        config = row.store_upload if isinstance(getattr(row, "store_upload", None), dict) else None
        if row.stage_type == "store_upload" and config and isinstance(config.get("authorizedBy"), dict):
            stored.setdefault(store_upload_config.signature(config), config["authorizedBy"])

    for stage in normalized:
        config = stage.get("store_upload")
        if stage["stage_type"] != "store_upload" or not config:
            continue
        kept = stored.get(store_upload_config.signature(config))
        if kept:
            config["authorizedBy"] = dict(kept)
            continue
        where = store_upload_config.target_label(config)
        if actor is None or not is_admin(actor):
            raise PipelineError(
                f"Only an administrator can point the stage '{stage['name']}' at {where}: "
                "publishing to a store is admin-only, and builds publish with the rights of "
                "whoever saved the target. Ask an administrator to set it.",
                code="store_upload_not_authorized",
            )
        config["authorizedBy"] = {
            "userId": actor.id,
            "username": actor.username,
            "at": datetime.now(timezone.utc).isoformat(),
        }
        app = db.session.get(MobileApplication, int(config["appId"]))
        log_audit(
            "ci_store_upload_target_authorized",
            actor=actor,
            target_type="ci_pipeline",
            target_id=str(pipeline.id),
            details={
                "stage": stage["name"],
                "app": app.name if app else config["appId"],
                "store": config["store"],
                "target": config["target"],
                "artifactType": config["artifactType"],
                "artifactPattern": config["artifactPattern"],
            },
            commit=False,
        )


def _apply_stages(
    pipeline: CiPipeline, stage_payloads: List[Dict[str, Any]], *, actor=None
) -> None:
    if len(stage_payloads) > MAX_STAGES:
        raise PipelineError(f"A pipeline may not exceed {MAX_STAGES} stages.")
    known_keys = _known_secret_keys(pipeline.service_id)
    normalized = [
        normalize_stage(payload if isinstance(payload, dict) else {}, index, known_keys)
        for index, payload in enumerate(stage_payloads)
    ]
    names = [stage["name"].lower() for stage in normalized]
    duplicates = {name for name in names if names.count(name) > 1}
    if duplicates:
        raise PipelineError(
            f"Stage names must be unique: {', '.join(sorted(duplicates))} is repeated."
        )
    check_parallel_groups(normalized)
    check_deploy_stages_last(normalized)
    _check_deploy_templates(normalized)
    _default_store_upload_apps(pipeline, normalized)
    # Before the clear below: the stamps being carried over live on the rows
    # that are about to be replaced.
    _stamp_deploy_authority(pipeline, normalized, actor)
    _stamp_store_upload_authority(pipeline, normalized, actor)
    # The inventory link a fixed-target Deploy stage implies ("this service is
    # that deployment"), unless the stage opts out. Never fails the save.
    from . import deployment_links

    deployment_links.record_from_stages(pipeline, normalized, actor)

    # Full replace. Stage ids are not stable across a save, which is why builds
    # snapshot their pipeline rather than pointing at live stage rows.
    pipeline.stages.clear()
    db.session.flush()
    for kwargs in normalized:
        pipeline.stages.append(CiPipelineStage(**kwargs))


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def _generated_pipeline(service: CiService, base=None, revision: str = ""):
    """Build an unsaved native pipeline from the application-type fallback."""
    payload = default_pipelines.for_service(service, revision)
    metadata = payload["metadata"]
    if metadata.get("requiresCustomization"):
        raise PipelineError(
            "Custom services need at least one command stage before they can run. "
            "Choose Customize Pipeline and provide the command."
        )

    known_keys = _known_secret_keys(service.id)
    stages = []
    for index, stage_payload in enumerate(payload.get("stages") or []):
        normalized = normalize_stage(stage_payload, index, known_keys)
        stages.append(SimpleNamespace(id=None, **normalized))

    return SimpleNamespace(
        id=getattr(base, "id", None),
        service_id=service.id,
        name=getattr(base, "name", None) or payload["name"],
        description=payload["description"],
        is_default=True,
        enabled=True,
        version=getattr(base, "version", None) or 0,
        parameters=_parameters(payload.get("parameters")),
        stages=stages,
        created_at=getattr(base, "created_at", None),
        updated_at=getattr(base, "updated_at", None),
        generated_default=True,
        default_metadata=metadata,
    )


def _generated_pipeline_dict(service: CiService, base=None) -> Dict[str, Any]:
    try:
        pipeline = _generated_pipeline(service, base, service.default_branch)
        data = pipeline_to_dict(pipeline)
        metadata = pipeline.default_metadata
    except PipelineError:
        payload = default_pipelines.for_service(service, service.default_branch)
        metadata = payload["metadata"]
        data = {
            "id": getattr(base, "id", None),
            "serviceId": service.id,
            "name": getattr(base, "name", None) or "default",
            "description": payload["description"],
            "isDefault": True,
            "enabled": True,
            "version": getattr(base, "version", None) or 0,
            "parameters": payload.get("parameters") or [],
            "stageCount": 0,
            "stages": [],
            "createdAt": None,
            "updatedAt": None,
        }
    data.update(
        {
            "isGeneratedDefault": True,
            "defaultMetadata": metadata,
        }
    )
    return data


def list_pipelines(service: CiService | int) -> List[Dict[str, Any]]:
    if not isinstance(service, CiService):
        service = db.session.get(CiService, int(service))
        if service is None:
            raise LookupError("Service not found.")
    # Build pipelines only. A merge check pipeline belongs to the Merge Checks
    # tab, which reads it through its own configuration — listing it here would
    # put a pipeline that runs no build and produces no artifact at the top of
    # the Pipeline tab on any service whose default is still the generated one.
    rows = (
        CiPipeline.query.filter_by(service_id=service.id)
        .filter(
            db.or_(CiPipeline.purpose == "build", CiPipeline.purpose.is_(None))
        )
        .order_by(CiPipeline.is_default.desc(), CiPipeline.id.asc())
        .all()
    )
    if not rows:
        return [_generated_pipeline_dict(service)]

    items = []
    for row in rows:
        if row.linked_pipeline_id:
            items.append(_linked_pipeline_dict(row))
        elif row.is_default and not row.stages:
            items.append(_generated_pipeline_dict(service, row))
        else:
            data = pipeline_to_dict(row)
            data["isGeneratedDefault"] = False
            items.append(data)
    return items


def _linked_pipeline_dict(row: CiPipeline) -> Dict[str, Any]:
    """A service pipeline that builds with a shared one, shown as what runs:
    the shared stages, read-only here, with what it is linked to."""
    from .shared_pipelines import shared_summary

    shared = row.linked_pipeline
    data = pipeline_to_dict(row)
    data["isGeneratedDefault"] = False
    data["linkedPipeline"] = shared_summary(row)
    data["ownStageCount"] = len(row.stages)
    if shared is not None:
        shown = pipeline_to_dict(shared)
        data["stages"] = shown["stages"]
        data["stageCount"] = shown["stageCount"]
        data["parameters"] = shown["parameters"]
        data["postActions"] = shown["postActions"]
    return data


def _refuse_linked_edit(pipeline: CiPipeline, payload: Dict[str, Any]) -> None:
    if not pipeline.linked_pipeline_id:
        return
    if not any(key in payload for key in ("stages", "parameters", "postActions")):
        return
    from .shared_pipelines import home_of

    home = home_of(pipeline.linked_pipeline)
    name = f"'{home.name}'" if home else "a shared pipeline"
    raise PipelineError(
        f"This service builds with the shared pipeline {name}, so its stages are edited there, "
        "on the Pipelines page. To change them for this service only, stop using it here "
        "(and copy its stages in) first.",
        code="pipeline_is_shared",
    )


def get_pipeline(pipeline_id: int) -> CiPipeline:
    row = db.session.get(CiPipeline, int(pipeline_id))
    if row is None:
        raise LookupError("Pipeline not found.")
    return row


def create_pipeline(
    service: CiService, payload: Dict[str, Any], *, actor=None
) -> Dict[str, Any]:
    name = _clean(payload.get("name"), 120) or "default"
    if CiPipeline.query.filter_by(service_id=service.id, name=name).first():
        raise PipelineError(f"This service already has a pipeline named '{name}'.")

    is_default = payload.get("isDefault")
    purpose = str(payload.get("purpose") or "build").strip().lower()
    if purpose not in PIPELINE_PURPOSES:
        raise PipelineError(f"'{purpose}' is not a pipeline purpose.")
    pipeline = CiPipeline(
        service_id=service.id,
        name=name,
        description=_clean(payload.get("description"), 2000) or None,
        purpose=purpose,
        # The first BUILD pipeline is the default whatever the payload says, so
        # a service can never end up with build pipelines and no default. A
        # merge check pipeline is never a default — it is not a build.
        is_default=(
            purpose == "build" and (bool(is_default) or not service.build_pipelines())
        ),
        enabled=payload.get("enabled") is not False,
        # Validated here rather than defaulted to []: a create that silently
        # dropped its build inputs produced a pipeline whose Run Build dialog
        # asked for nothing and whose conditional stages could never fire.
        parameters=_parameters(payload.get("parameters")),
        created_by_user_id=getattr(actor, "id", None),
    )
    db.session.add(pipeline)
    db.session.flush()
    _apply_stages(pipeline, payload.get("stages") or [], actor=actor)
    if "postActions" in payload:
        pipeline.post_actions = _post_actions(payload.get("postActions"), service.id)
    if pipeline.is_default:
        _demote_other_defaults(service.id, pipeline.id)
    db.session.commit()

    log_audit(
        "ci_pipeline_saved",
        actor=actor,
        target_type="ci_pipeline",
        target_id=str(pipeline.id),
        details={
            "service": service.slug,
            "pipeline": pipeline.name,
            "stageCount": len(pipeline.stages),
            "created": True,
        },
    )
    return pipeline_to_dict(pipeline)


def update_pipeline(
    pipeline: CiPipeline, payload: Dict[str, Any], *, actor=None
) -> Dict[str, Any]:
    name = _clean(payload.get("name"), 120) or pipeline.name
    clash = (
        CiPipeline.query.filter_by(service_id=pipeline.service_id, name=name)
        .filter(CiPipeline.id != pipeline.id)
        .first()
    )
    if clash:
        raise PipelineError(f"This service already has a pipeline named '{name}'.")

    _refuse_linked_edit(pipeline, payload)
    pipeline.name = name
    if "description" in payload:
        pipeline.description = _clean(payload.get("description"), 2000) or None
    if "enabled" in payload:
        pipeline.enabled = bool(payload.get("enabled"))
    if payload.get("isDefault") and (pipeline.purpose or "build") == "build":
        pipeline.is_default = True

    if "parameters" in payload:
        pipeline.parameters = _parameters(payload.get("parameters"))

    if "stages" in payload:
        _apply_stages(pipeline, payload.get("stages") or [], actor=actor)
    # Absent from the payload = unchanged, like stages: a client that predates
    # post actions (or an MCP stage edit) must not wipe them.
    if "postActions" in payload:
        pipeline.post_actions = _post_actions(payload.get("postActions"), pipeline.service_id)
    # Bumped on every save so a build's snapshot records which revision ran.
    pipeline.version = int(pipeline.version or 1) + 1
    pipeline.updated_at = datetime.now(timezone.utc)
    if pipeline.is_default:
        _demote_other_defaults(pipeline.service_id, pipeline.id)
    db.session.commit()

    log_audit(
        "ci_pipeline_saved",
        actor=actor,
        target_type="ci_pipeline",
        target_id=str(pipeline.id),
        details={
            "service": pipeline.service.slug if pipeline.service else None,
            "pipeline": pipeline.name,
            "stageCount": len(pipeline.stages),
            "version": pipeline.version,
        },
    )
    return pipeline_to_dict(pipeline)


def delete_pipeline(pipeline: CiPipeline, *, actor=None) -> None:
    service_id = pipeline.service_id
    was_default = pipeline.is_default
    name = pipeline.name
    db.session.delete(pipeline)
    db.session.flush()
    if was_default:
        # Never leave a service with pipelines but no default.
        replacement = (
            CiPipeline.query.filter_by(service_id=service_id, purpose="build")
            .order_by(CiPipeline.id.asc())
            .first()
        )
        if replacement:
            replacement.is_default = True
            db.session.add(replacement)
    db.session.commit()
    log_audit(
        "ci_pipeline_deleted",
        actor=actor,
        target_type="ci_pipeline",
        target_id=str(pipeline.id),
        details={"pipeline": name, "serviceId": service_id},
    )


def create_from_template(
    service: CiService, application_type: Optional[str] = None, *, actor=None
) -> Dict[str, Any]:
    selected_type = application_type or service.application_type
    if selected_type == service.application_type:
        payload = default_pipelines.for_service(service, service.default_branch)
    else:
        payload = templates.default_pipeline_payload(selected_type)
    payload.pop("metadata", None)
    existing = CiPipeline.query.filter_by(
        service_id=service.id, name=payload["name"]
    ).first()
    if existing:
        return update_pipeline(existing, payload, actor=actor)
    return create_pipeline(service, payload, actor=actor)


def import_jenkinsfile(
    content: str, *, service: Optional[CiService] = None
) -> Dict[str, Any]:
    """Read a Jenkinsfile into a draft this service could save.

    The draft is *not* written. It goes back to the editor as unsaved changes so
    a person reviews a translation before it replaces a pipeline that works.

    The one thing reconciled against the database here is secrets. A stage may
    not reference a secret that does not exist — :func:`_secret_refs` refuses it
    on save — so credential bindings the Jenkinsfile named are matched against
    what this service actually has: the ones that exist become stage references,
    and the ones that do not are reported as work to do, with the stage
    references left off so the draft still saves.
    """
    draft = jenkinsfile.parse(content)

    known = _known_secret_keys(service.id) if service is not None else set()
    missing: List[str] = []
    for entry in draft["secrets"]:
        entry["defined"] = entry["name"] in known
        if not entry["defined"]:
            missing.append(entry["name"])
    if missing:
        for stage in draft["stages"]:
            stage["secretRefs"] = [
                ref for ref in stage["secretRefs"] if ref["name"] not in missing
            ]
        draft["notes"].insert(
            0,
            {
                "level": jenkinsfile.ERROR if service is not None else jenkinsfile.WARNING,
                "stage": "",
                "message": (
                    "This job read credentials that this service does not have: "
                    + ", ".join(sorted(set(missing)))
                    + ". Add them under Secrets, then attach them to the stages "
                    "that need them — a stage cannot reference a secret that does "
                    "not exist, so they were left off."
                ),
            },
        )
        draft["counts"]["errors"] += 1

    # The draft is run through the same normalizer that saving uses, so the
    # editor is told now about anything that would be refused later. Reported,
    # never repaired: a stage silently rewritten to be saveable is a stage that
    # no longer matches the Jenkinsfile it came from.
    blocking: List[str] = []
    normalized_draft: List[Dict[str, Any]] = []
    for index, stage in enumerate(draft["stages"]):
        try:
            normalized_draft.append(normalize_stage(stage, index, known))
        except PipelineError as exc:
            blocking.append(str(exc))
    if len(normalized_draft) == len(draft["stages"]):
        # Parallel groups are a rule about the whole list, so they can only be
        # judged once every stage normalized.
        try:
            check_parallel_groups(normalized_draft)
        except PipelineError as exc:
            blocking.append(str(exc))
    try:
        _parameters(draft["parameters"])
    except PipelineError as exc:
        blocking.append(str(exc))
    draft["blocking"] = blocking

    return draft


def _demote_other_defaults(service_id: int, keep_id: int) -> None:
    CiPipeline.query.filter(
        CiPipeline.service_id == service_id,
        CiPipeline.id != keep_id,
        CiPipeline.is_default.is_(True),
    ).update({"is_default": False}, synchronize_session=False)



def parameter_definitions(pipeline: CiPipeline) -> List[Dict[str, Any]]:
    """The stored definitions, unresolved — what a snapshot keeps."""
    from .serializers import _json_list

    return [p for p in _json_list(pipeline.parameters) if isinstance(p, dict)]


def resolve_parameters(service: CiService, pipeline: CiPipeline) -> List[Dict[str, Any]]:
    """The pipeline's parameters with dynamic choices filled in.

    Resolution happens here, server-side, so the Run Build dialog receives a
    ready list rather than learning how to build one. A repository that cannot
    be reached yields an empty ``choices`` and an ``error`` the dialog shows —
    the parameter stays usable by typing, because a listing outage should not
    stop a release.
    """
    from .serializers import _json_list

    resolved: List[Dict[str, Any]] = []
    listing: Optional[List[Dict[str, str]]] = None
    listing_error = ""

    for param in _json_list(pipeline.parameters):
        if not isinstance(param, dict):
            continue
        param = dict(param)
        if param.get("type") == "dynamic_choice":
            if listing is None:
                listing, listing_error = _repository_refs(service)
            source = param.get("source") or "branches"
            wanted = (
                ("branch", "tag")
                if source == "branches_and_tags"
                else ("tag",) if source == "tags" else ("branch",)
            )
            param["choices"] = [item["value"] for item in listing if item["type"] in wanted]
            if listing_error:
                param["error"] = listing_error
        resolved.append(param)
    return resolved


def _repository_refs(service: CiService) -> Tuple[List[Dict[str, str]], str]:
    """Branches and tags, or an empty list and why not."""
    if not service.source_ready():
        return [], "The service's source is not configured yet."
    try:
        from . import source as source_port

        handler = source_port.get_provider(service.repository_provider)
        ref = handler.parse_repository_url(service.repository_url)
        items = handler.list_revisions(ref, service.credential_profile)
        return [
            {"value": item.value, "type": item.kind}
            for item in items
            if getattr(item, "value", "")
        ], ""
    except Exception as exc:  # A listing failure must not block a build.
        return [], str(exc) or "The repository could not be listed."


def validate_parameter_values(
    pipeline: CiPipeline, values: Optional[Dict[str, Any]]
) -> Dict[str, str]:
    """Submitted values checked against the pipeline's own definitions.

    Returns the accepted values, which the caller passes as the build's
    ``variables``. Rejects rather than drops: a build that silently ignored a
    parameter would run differently from what was asked for, and say nothing.
    """
    from .serializers import _json_list

    definitions = [p for p in _json_list(pipeline.parameters) if isinstance(p, dict)]
    submitted = {str(k): v for k, v in (values or {}).items()}
    known = {str(p.get("name")) for p in definitions}

    unknown = [
        name
        for name in submitted
        if name not in known and name not in RESERVED_VARIABLES
    ]
    if unknown and definitions:
        # Only complain when the pipeline HAS parameters: automation still
        # passes free-form variables to pipelines that declare none.
        raise PipelineError(
            f"This pipeline has no parameter named '{sorted(unknown)[0]}'."
        )

    accepted: Dict[str, str] = {
        name: str(value)[:MAX_TEXT_CHARS]
        for name, value in submitted.items()
        if name in RESERVED_VARIABLES and name not in known
    }
    for param in definitions:
        name = str(param.get("name") or "")
        if not name:
            continue
        param_type = param.get("type") or "text"
        raw = submitted.get(name, None)

        if param_type == "boolean":
            accepted[name] = "true" if _truthy(
                param.get("default") if raw is None else raw
            ) else "false"
            continue

        value = str(param.get("default") or "" if raw is None else raw).strip()
        if not value:
            if param.get("required"):
                raise PipelineError(f"'{param.get('label') or name}' is required.")
            accepted[name] = ""
            continue

        if param_type == "multiline":
            # A whole file, kept as typed apart from surrounding blank lines.
            # Truncating it silently would hand the build a Dockerfile missing
            # its last instruction, so an oversized value is refused instead.
            if len(value) > MAX_MULTILINE_CHARS:
                raise PipelineError(
                    f"'{param.get('label') or name}' is {len(value)} characters; "
                    f"the limit is {MAX_MULTILINE_CHARS}."
                )
            accepted[name] = value
            continue

        if param_type == "choice":
            choices = [str(c) for c in (param.get("choices") or [])]
            if value not in choices:
                raise PipelineError(
                    f"'{param.get('label') or name}' must be one of: {', '.join(choices)}."
                )
        # dynamic_choice is deliberately not checked against the live list: it
        # is a convenience, and a ref created seconds ago must still be usable.
        accepted[name] = value[:MAX_TEXT_CHARS]

    # Values a pipeline without parameters was given still travel through.
    if not definitions:
        accepted = {str(k): str(v)[:MAX_MULTILINE_CHARS] for k, v in submitted.items()}
    _check_trigger_values(accepted, known)
    return accepted


def _check_trigger_values(values: Dict[str, str], declared) -> None:
    """Refuse trigger values that would reach generated shell or the loader.

    The engine splices DOCKERFILE_PATH / IMAGE_TAG / IMAGE_NAME into the image
    stage's buildctl line, next to the registry push credentials — whoever may
    RUN a build must not thereby be able to rewrite that line. Checked on the
    final values whether or not the pipeline declares the name, because a
    declared parameter is a free-text field too. Undeclared names must also be
    real variable names and not ones that redirect the shell (PATH, LD_*...).
    """
    for name, value in values.items():
        if name not in declared and name not in RESERVED_VARIABLES:
            problem = build_inputs.env_name_problem(name)
            if problem:
                raise PipelineError(problem)
        problem = build_inputs.reserved_value_problem(name, value)
        if problem:
            raise PipelineError(problem)


def resolve_for_build(
    service: CiService, pipeline_id: Optional[int] = None, *, revision: str = ""
) -> Tuple[Any, List[Any]]:
    """The pipeline a Run Build should execute, with its runnable stages."""
    if pipeline_id:
        pipeline = db.session.get(CiPipeline, int(pipeline_id))
        if pipeline is None or pipeline.service_id != service.id:
            raise PipelineError("That pipeline does not belong to this service.")
    else:
        pipeline = service.default_pipeline()
    if pipeline is not None and not pipeline.enabled:
        raise PipelineError(f"Pipeline '{pipeline.name}' is disabled.")
    if pipeline is not None and pipeline.linked_pipeline_id:
        return _shared_for_build(pipeline)
    if pipeline is None or not pipeline.stages:
        generated = _generated_pipeline(service, pipeline, revision)
        return generated, list(generated.stages)
    stages = [stage for stage in pipeline.stages if stage.enabled]
    if not stages:
        raise PipelineError(f"Pipeline '{pipeline.name}' has no enabled stages.")
    return pipeline, stages


def _shared_for_build(pipeline: CiPipeline) -> Tuple[Any, List[Any]]:
    """A service pipeline that builds with a shared one: that pipeline's stages,
    build inputs and post actions, under the service row's identity.

    The build keeps ``pipeline_id`` = the service's own row, so its history,
    stage matrix and default-pipeline lookups stay where they were; the
    snapshot records which shared pipeline, at which version, actually ran.
    """
    from .shared_pipelines import home_of

    shared = pipeline.linked_pipeline
    home = home_of(shared)
    if shared is None or home is None:
        raise PipelineError(
            "This service builds with a shared pipeline that no longer exists. "
            "Pick another one, or go back to the service's own pipeline.",
            code="shared_pipeline_missing",
        )
    if not shared.enabled or home.status != "active":
        raise PipelineError(
            f"The shared pipeline '{home.name}' is turned off, so services that use it cannot "
            "build. Turn it back on from the Pipelines page.",
            code="shared_pipeline_disabled",
        )
    stages = [stage for stage in shared.stages if stage.enabled]
    if not stages:
        raise PipelineError(
            f"The shared pipeline '{home.name}' has no enabled stages.",
            code="shared_pipeline_empty",
        )
    proxy = SimpleNamespace(
        id=pipeline.id,
        service_id=pipeline.service_id,
        name=pipeline.name,
        description=shared.description,
        is_default=pipeline.is_default,
        enabled=True,
        version=shared.version,
        parameters=shared.parameters,
        # Never None: of_pipeline would otherwise fall back to the SERVICE
        # row's own (dormant) post actions.
        post_actions=list(shared.post_actions or []),
        stages=list(shared.stages),
        created_at=shared.created_at,
        updated_at=shared.updated_at,
        shared={
            "id": home.id,
            "slug": home.slug,
            "name": home.name,
            "pipelineId": shared.id,
            "version": shared.version,
            "homeServiceId": home.id,
        },
    )
    return proxy, stages
