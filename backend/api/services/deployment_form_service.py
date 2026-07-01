"""Orchestration for the Smart Deployment Form feature.

Ties together the field schema, Excel generator/parser, and validation layer, and
reuses the existing template resolver + change-bundle workflow. Every function
returns the repo-standard ``(data, error, status)`` tuple and audits mutations.

The template is always the source of truth: generation reads the current template;
import re-reads the *current* template, rebuilds ``answers``, and re-validates —
the uploaded Excel only supplies the editable answers.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

from ..access_engine import can_access_cluster, can_access_namespace, is_admin
from ..audit import log_audit
from ..db import db
from ..models import DeploymentFormGeneration, DeploymentFormImport, User
from .deployment_form_excel import build_workbook
from .deployment_form_parser import parse_form
from .deployment_form_schema import (
    FORM_SCHEMA_VERSION,
    assemble_answers,
    build_form_fields,
    template_version,
)
from .deployment_form_validation import validate_import
from .user_template_service import get_user_template_detail
from .wizard_templates import get_template as get_builtin_template

# Generated forms are valid for this long; imports past expiry warn but still parse.
FORM_TTL_DAYS = 30

Result = Tuple[Optional[Dict[str, Any]], Optional[str], int]


def _get_template_detail(template_id: str) -> Optional[Dict[str, Any]]:
    if not template_id:
        return None
    return get_builtin_template(template_id) or get_user_template_detail(template_id)


def _name_of(item: Any) -> str:
    return item.get("name") if isinstance(item, dict) else (item or "")


def collect_form_dropdowns(
    user: Optional[User], cluster_id: Optional[str], namespace: Optional[str]
) -> Dict[str, List[str]]:
    """Gather dropdown values for a cluster/namespace, mirroring the Deploy Wizard.

    Uses the *same* sources the wizard's namespace / config-resources / storage-class
    endpoints use: live Kubernetes when the cluster runs in real-k8s mode, otherwise
    the canned mock data. This keeps the Excel form's dropdowns identical to what a
    deployer sees in the wizard (including mock/demo clusters). Access is still
    re-checked by the caller; unavailable data simply yields fewer keys.
    """
    dd: Dict[str, List[str]] = {}
    if not cluster_id:
        return dd

    from ..access_engine import filter_namespaces_for_user
    from ..k8s_provider import (
        list_configmaps_secrets_from_k8s,
        list_namespaces_from_k8s,
        list_storage_classes_from_k8s,
        resolve_cluster_access,
        should_use_real_k8s,
    )
    from ..mock_data import NAMESPACE_RESOURCES, NAMESPACES, STORAGE_CLASSES

    access = resolve_cluster_access(cluster_id) if should_use_real_k8s(cluster_id) else None

    # --- Namespaces (cluster-scoped) ---
    ns_items = None
    if access:
        try:
            ns_items = list_namespaces_from_k8s(access).get("items")
        except Exception:
            ns_items = None
    if ns_items is None:
        ns_items = NAMESPACES.get(cluster_id)
    if ns_items is not None:
        if user:
            ns_items = filter_namespaces_for_user(user, cluster_id, ns_items)
        dd["namespaces"] = sorted({_name_of(n) for n in ns_items if _name_of(n)})

    # --- Storage classes (cluster-scoped) ---
    sc_items = None
    if access:
        try:
            sc_items = list_storage_classes_from_k8s(access)
        except Exception:
            sc_items = None
    if sc_items is None:
        sc_items = STORAGE_CLASSES.get(cluster_id)
    if sc_items is not None:
        dd["storageClasses"] = sorted({_name_of(s) for s in sc_items if _name_of(s)})

    # --- ConfigMaps / Secrets (namespace-scoped) ---
    if namespace:
        cfg = None
        if access:
            try:
                cfg = list_configmaps_secrets_from_k8s(access, namespace)
            except Exception:
                cfg = None
        if cfg is None:
            ns_res = (NAMESPACE_RESOURCES.get(cluster_id) or {}).get(namespace) or {}
            cfg = {"configMaps": ns_res.get("configMaps", []), "secrets": ns_res.get("secrets", [])}
        secrets = cfg.get("secrets") or []
        dd["configMaps"] = sorted({_name_of(c) for c in (cfg.get("configMaps") or []) if _name_of(c)})
        dd["secrets"] = sorted({_name_of(s) for s in secrets if _name_of(s)})
        dd["tlsSecrets"] = sorted(
            {_name_of(s) for s in secrets if isinstance(s, dict) and s.get("type") == "kubernetes.io/tls" and _name_of(s)}
        )
        dd["imagePullSecrets"] = sorted(
            {
                _name_of(s)
                for s in secrets
                if isinstance(s, dict) and s.get("type") == "kubernetes.io/dockerconfigjson" and _name_of(s)
            }
        )
    return dd


# ---------------------------------------------------------------------------
# Generate
# ---------------------------------------------------------------------------

def generate_form(
    user: Optional[User],
    template_id: str,
    *,
    cluster_id: Optional[str] = None,
    namespace: Optional[str] = None,
) -> Result:
    template = _get_template_detail(template_id)
    if not template:
        return None, "Template not found", 404

    if user and cluster_id and not can_access_cluster(user, cluster_id):
        return None, "You do not have access to the selected cluster.", 403
    if user and cluster_id and namespace and not can_access_namespace(user, cluster_id, namespace):
        return None, "You do not have access to the selected namespace.", 403

    fields = build_form_fields(template)
    # Prefill the cluster/namespace the user chose in the modal.
    for field in fields:
        if field["key"] == "basics.clusterId" and cluster_id:
            field["default"] = cluster_id
        elif field["key"] == "basics.namespace" and namespace:
            field["default"] = namespace

    dropdowns = collect_form_dropdowns(user, cluster_id, namespace)
    if cluster_id:
        # The cluster is fixed by the modal choice — offer it as the only option.
        dropdowns["clusters"] = [cluster_id]

    form_uid = uuid.uuid4().hex
    now = datetime.now(timezone.utc)
    version = template_version(template)
    metadata = {
        "templateId": template_id,
        "templateName": template.get("name") or template_id,
        "templateVersion": version,
        "schemaVersion": FORM_SCHEMA_VERSION,
        "generatedFormId": form_uid,
        "generatedBy": user.id if user else None,
        "generatedByName": (user.full_name or user.username) if user else None,
        "generatedAt": now.isoformat(),
        "clusterId": cluster_id or "",
        "namespace": namespace or "",
    }

    try:
        xlsx = build_workbook(fields, dropdowns, metadata)
    except Exception as exc:  # openpyxl failure — surface cleanly
        return None, f"Failed to generate the Excel form: {exc}", 500

    gen = DeploymentFormGeneration(
        form_uid=form_uid,
        template_slug=template_id,
        template_version=version,
        schema_version=FORM_SCHEMA_VERSION,
        generated_by=user.id if user else None,
        cluster_id=cluster_id,
        namespace=namespace,
        schema_json={"fields": fields},
        metadata_json=metadata,
        status="active",
        expires_at=now + timedelta(days=FORM_TTL_DAYS),
    )
    db.session.add(gen)
    db.session.commit()

    log_audit(
        "deployment_form_generated",
        actor=user,
        target_type="deployment_form",
        target_id=form_uid,
        details={"templateId": template_id, "clusterId": cluster_id, "namespace": namespace},
    )

    safe_slug = "".join(c if c.isalnum() or c in "-_" else "-" for c in str(template_id))[:40]
    filename = f"deployment-form-{safe_slug}-{form_uid[:8]}.xlsx"
    return {"bytes": xlsx, "filename": filename, "formUid": form_uid}, None, 200


# ---------------------------------------------------------------------------
# Import
# ---------------------------------------------------------------------------

def _validate_and_context(
    user: Optional[User],
    template: Optional[Dict[str, Any]],
    metadata: Dict[str, Any],
    answers: Dict[str, Any],
) -> Tuple[Dict[str, Any], Optional[str], Optional[str]]:
    """Re-run authorization + validation for an import. Returns (result, cluster_id, namespace)."""
    basics = answers.get("basics") or {}
    cluster_id = str(basics.get("clusterId") or metadata.get("clusterId") or "").strip() or None
    namespace = str(basics.get("namespace") or metadata.get("namespace") or "").strip() or None

    template_exists = template is not None
    version_matches = template_exists and template_version(template) == metadata.get("templateVersion")

    cluster_accessible: Optional[bool] = None
    namespace_accessible: Optional[bool] = None
    if user and cluster_id:
        cluster_accessible = can_access_cluster(user, cluster_id)
        if cluster_accessible and namespace:
            namespace_accessible = can_access_namespace(user, cluster_id, namespace)

    dropdowns = (
        collect_form_dropdowns(user, cluster_id, namespace) if cluster_id and cluster_accessible else {}
    )
    context = {
        "templateExists": template_exists,
        "versionMatches": version_matches,
        "clusterAccessible": cluster_accessible,
        "namespaceAccessible": namespace_accessible,
        "clusterId": cluster_id,
        "namespace": namespace,
    }
    result = validate_import(template, answers, dropdown_data=dropdowns, context=context)
    return result, cluster_id, namespace


def import_form(user: Optional[User], file_bytes: bytes) -> Result:
    raw_values, metadata, errors = parse_form(file_bytes)
    if errors:
        return None, errors[0], 400

    template_id = str(metadata.get("templateId") or "").strip()
    template = _get_template_detail(template_id)
    fields = build_form_fields(template) if template else []
    answers = assemble_answers(raw_values, fields)

    result, cluster_id, namespace = _validate_and_context(user, template, metadata, answers)

    generation = None
    form_uid = metadata.get("generatedFormId")
    if form_uid:
        generation = DeploymentFormGeneration.query.filter_by(form_uid=form_uid).first()

    imp = DeploymentFormImport(
        generation_id=generation.id if generation else None,
        form_uid=form_uid,
        template_slug=template_id,
        template_version=str(metadata.get("templateVersion") or ""),
        uploaded_by=user.id if user else None,
        cluster_id=cluster_id,
        namespace=namespace,
        parsed_answers_json=answers,
        validation_result_json=result,
        status="invalid" if result.get("blocking") else "valid",
    )
    db.session.add(imp)
    db.session.commit()

    log_audit(
        "deployment_form_imported",
        actor=user,
        target_type="deployment_form_import",
        target_id=str(imp.id),
        details={"templateId": template_id, "blocking": result.get("blocking"), "formUid": form_uid},
    )
    return _serialize_import(imp, template), None, 200


# ---------------------------------------------------------------------------
# Fetch / re-validate
# ---------------------------------------------------------------------------

def _load_import(user: Optional[User], import_id: int) -> Tuple[Optional[DeploymentFormImport], Optional[str], int]:
    imp = DeploymentFormImport.query.filter_by(id=import_id).first()
    if not imp:
        return None, "Import not found", 404
    if user and imp.uploaded_by and imp.uploaded_by != user.id and not is_admin(user):
        return None, "Forbidden", 403
    return imp, None, 200


def get_import(user: Optional[User], import_id: int) -> Result:
    imp, error, status = _load_import(user, import_id)
    if error:
        return None, error, status
    return _serialize_import(imp, _get_template_detail(imp.template_slug)), None, 200


def revalidate_import(user: Optional[User], import_id: int) -> Result:
    imp, error, status = _load_import(user, import_id)
    if error:
        return None, error, status
    template = _get_template_detail(imp.template_slug)
    metadata = {"templateVersion": imp.template_version, "clusterId": imp.cluster_id, "namespace": imp.namespace}
    result, cluster_id, namespace = _validate_and_context(user, template, metadata, imp.parsed_answers_json or {})
    imp.validation_result_json = result
    imp.cluster_id = cluster_id
    imp.namespace = namespace
    if imp.status in ("valid", "invalid", "parsed"):
        imp.status = "invalid" if result.get("blocking") else "valid"
    db.session.commit()
    log_audit(
        "deployment_form_validated",
        actor=user,
        target_type="deployment_form_import",
        target_id=str(imp.id),
        details={"blocking": result.get("blocking")},
    )
    return _serialize_import(imp, template), None, 200


# ---------------------------------------------------------------------------
# Apply to wizard / bundle / approval
# ---------------------------------------------------------------------------

def build_wizard_state(user: Optional[User], import_id: int) -> Result:
    imp, error, status = _load_import(user, import_id)
    if error:
        return None, error, status
    template = _get_template_detail(imp.template_slug)
    if not template:
        return None, "The source template no longer exists.", 404
    if imp.status in ("valid", "invalid", "parsed"):
        imp.status = "applied"
        db.session.commit()
    log_audit(
        "deployment_form_applied_to_wizard",
        actor=user,
        target_type="deployment_form_import",
        target_id=str(imp.id),
        details={"templateId": imp.template_slug},
    )
    return {
        "template": template,
        "answers": imp.parsed_answers_json or {},
        "validation": imp.validation_result_json or {},
    }, None, 200


def _bundle_payload(imp: DeploymentFormImport) -> Dict[str, Any]:
    return {
        "actionType": "create_from_template",
        "templateId": imp.template_slug,
        "answers": imp.parsed_answers_json or {},
        "clusterId": imp.cluster_id or "",
        "namespace": imp.namespace or "",
    }


def add_import_to_bundle(user: Optional[User], import_id: int) -> Result:
    imp, error, status = _load_import(user, import_id)
    if error:
        return None, error, status
    if (imp.validation_result_json or {}).get("blocking"):
        return None, "Resolve the blocking validation errors before adding to a bundle.", 400

    from .change_bundle_service import ChangeBundleError, add_item, get_or_create_draft

    try:
        bundle = get_or_create_draft(user)
        serialized = add_item(user, bundle.id, _bundle_payload(imp))
    except ChangeBundleError as exc:
        return None, str(exc), getattr(exc, "status_code", 400)

    imp.status = "bundled"
    db.session.commit()
    log_audit(
        "deployment_form_added_to_bundle",
        actor=user,
        target_type="deployment_form_import",
        target_id=str(imp.id),
        details={"bundleId": bundle.id, "templateId": imp.template_slug},
    )
    return {"bundle": serialized, "import": _serialize_import(imp, None)}, None, 200


def send_import_for_approval(
    user: Optional[User],
    import_id: int,
    *,
    note: str = "",
    window_start: Any = None,
    window_end: Any = None,
    window_timezone: Any = None,
    stop_on_failure: bool = True,
) -> Result:
    imp, error, status = _load_import(user, import_id)
    if error:
        return None, error, status
    if (imp.validation_result_json or {}).get("blocking"):
        return None, "Resolve the blocking validation errors before sending for approval.", 400

    from .change_bundle_service import ChangeBundleError, add_item, get_or_create_draft, submit_bundle

    try:
        bundle = get_or_create_draft(user)
        add_item(user, bundle.id, _bundle_payload(imp))
        serialized = submit_bundle(
            user,
            bundle.id,
            note=note,
            window_start=window_start,
            window_end=window_end,
            window_timezone=window_timezone,
            stop_on_failure=stop_on_failure,
        )
    except ChangeBundleError as exc:
        return None, str(exc), getattr(exc, "status_code", 400)

    imp.status = "submitted"
    db.session.commit()
    log_audit(
        "deployment_form_sent_for_approval",
        actor=user,
        target_type="deployment_form_import",
        target_id=str(imp.id),
        details={"bundleId": bundle.id, "templateId": imp.template_slug},
    )
    return {"bundle": serialized, "import": _serialize_import(imp, None)}, None, 200


# ---------------------------------------------------------------------------
# Serialization
# ---------------------------------------------------------------------------

def _serialize_import(imp: DeploymentFormImport, template: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    return {
        "id": imp.id,
        "status": imp.status,
        "templateId": imp.template_slug,
        "templateName": (template or {}).get("name") or imp.template_slug,
        "clusterId": imp.cluster_id,
        "namespace": imp.namespace,
        "answers": imp.parsed_answers_json or {},
        "validation": imp.validation_result_json or {},
        "createdAt": imp.created_at.isoformat() if imp.created_at else None,
    }
