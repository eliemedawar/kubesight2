"""Cloud accounts for the Cluster Builder: Oracle Cloud, AWS and Google Cloud.

  /api/cloud-accounts                        list (+ the providers' form fields), add
  /api/cloud-accounts/<id>                   edit, delete
  /api/cloud-accounts/<id>/test              sign in with the stored keys
  /api/cloud-accounts/<id>/regions           regions the account may use
  /api/cloud-accounts/<id>/inventory         networks, zones, images, machine types
  /api/cloud-accounts/<id>/resolve-sizes     CPU + memory per role → machine type

Changing an account needs ``vsphere:manage`` (the same people who manage vCenter
connections); reading one, as the wizard does, needs ``cluster_builds:view``.
"""

from __future__ import annotations

from flask import Blueprint, request

from ..audit import log_audit
from ..auth_utils import get_current_user
from ..decorators import require_permission
from ..response import error_response, success_response
from ..services.cluster_build.provisioning import clouds
from ..services.cluster_build.provisioning.clouds import accounts
from ..services.cluster_build.provisioning.clouds.base import CloudError

cloud_accounts_bp = Blueprint("cloud_accounts", __name__)


def _actor():
    user = get_current_user()
    return user, getattr(user, "username", "") or ""


def _refresh() -> bool:
    return request.args.get("refresh") in ("1", "true", "yes")


@cloud_accounts_bp.route("/api/cloud-accounts", methods=["GET"])
@require_permission("cluster_builds:view")
def list_cloud_accounts():
    return success_response({
        "accounts": accounts.list_accounts(),
        "providers": clouds.catalog(),
        "simulated": accounts.simulated(),
    })


@cloud_accounts_bp.route("/api/cloud-accounts", methods=["POST"])
@require_permission("vsphere:manage")
def create_cloud_account():
    user, actor = _actor()
    try:
        account = accounts.create(request.get_json(silent=True) or {}, actor)
    except ValueError as exc:
        return error_response(str(exc), 400)
    log_audit("cloud_account_created", actor=user, target_type="cloud_account",
              target_id=str(account["id"]),
              details={"name": account["name"], "provider": account["provider"]})
    return success_response({"account": account}, 201)


@cloud_accounts_bp.route("/api/cloud-accounts/<int:account_id>", methods=["PUT"])
@require_permission("vsphere:manage")
def update_cloud_account(account_id: int):
    user, _ = _actor()
    try:
        account = accounts.update(account_id, request.get_json(silent=True) or {})
    except LookupError as exc:
        return error_response(str(exc), 404)
    except ValueError as exc:
        return error_response(str(exc), 400)
    log_audit("cloud_account_updated", actor=user, target_type="cloud_account",
              target_id=str(account_id),
              details={"name": account["name"], "provider": account["provider"]})
    return success_response({"account": account})


@cloud_accounts_bp.route("/api/cloud-accounts/<int:account_id>", methods=["DELETE"])
@require_permission("vsphere:manage")
def delete_cloud_account(account_id: int):
    user, _ = _actor()
    try:
        accounts.delete(account_id)
    except LookupError as exc:
        return error_response(str(exc), 404)
    except ValueError as exc:
        return error_response(str(exc), 400)
    log_audit("cloud_account_deleted", actor=user, target_type="cloud_account",
              target_id=str(account_id))
    return success_response({"deleted": True})


@cloud_accounts_bp.route("/api/cloud-accounts/<int:account_id>/test", methods=["POST"])
@require_permission("vsphere:manage")
def test_cloud_account(account_id: int):
    user, _ = _actor()
    try:
        result = accounts.test(account_id)
    except LookupError as exc:
        return error_response(str(exc), 404)
    log_audit("cloud_account_tested", actor=user, target_type="cloud_account",
              target_id=str(account_id), details={"ok": result["ok"]})
    return success_response(result)


@cloud_accounts_bp.route("/api/cloud-accounts/<int:account_id>/regions", methods=["GET"])
@require_permission("cluster_builds:view")
def cloud_account_regions(account_id: int):
    try:
        return success_response({"regions": accounts.regions(account_id, refresh=_refresh())})
    except LookupError as exc:
        return error_response(str(exc), 404)
    except CloudError as exc:
        return error_response(f"The cloud could not be read: {exc}", 502)


@cloud_accounts_bp.route("/api/cloud-accounts/<int:account_id>/inventory", methods=["GET"])
@require_permission("cluster_builds:view")
def cloud_account_inventory(account_id: int):
    scope = {"compartmentId": (request.args.get("compartmentId") or "").strip()}
    try:
        data = accounts.inventory(
            account_id, request.args.get("region") or "", scope, refresh=_refresh(),
        )
    except LookupError as exc:
        return error_response(str(exc), 404)
    except ValueError as exc:
        return error_response(str(exc), 400)
    except CloudError as exc:
        return error_response(f"The cloud could not be read: {exc}", 502)
    return success_response(data)


@cloud_accounts_bp.route("/api/cloud-accounts/<int:account_id>/resolve-sizes", methods=["POST"])
@require_permission("cluster_builds:view")
def cloud_account_resolve_sizes(account_id: int):
    try:
        return success_response(accounts.resolve_sizes(account_id, request.get_json(silent=True) or {}))
    except LookupError as exc:
        return error_response(str(exc), 404)
    except ValueError as exc:
        return error_response(str(exc), 400)
