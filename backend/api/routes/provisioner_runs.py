"""What the OKE provisioner Jobs call (``provisioning.remote``).

  GET  /api/internal/provisioner/runs/<id>?phase=   the step's inputs
  POST /api/internal/provisioner/runs/<id>/plan     the saved plan and its hash
  POST /api/internal/provisioner/runs/<id>/facts    the cluster's facts (K22)
  POST /api/internal/provisioner/runs/<id>/errored-state
  POST /api/internal/provisioner/runs/<id>/bound    the role binding's outcome
  POST /api/internal/provisioner/runs/<id>/failed

Not for people. Every call carries the Job's projected service-account token
(audience ``kubesight-backend``) as a Bearer token; the backend checks it with
a TokenReview and accepts only ``kubesight-provisioner`` from the pod of the
step it is waiting for (``remote.authenticate``).
"""

from __future__ import annotations

from flask import Blueprint, jsonify, request

from ..db import db
from ..models import ClusterProvisionJob
from ..services.cluster_build.provisioning import remote

provisioner_runs_bp = Blueprint("provisioner_runs", __name__)


def _bearer() -> str:
    header = request.headers.get("Authorization", "")
    return header.split(" ", 1)[1].strip() if header.lower().startswith("bearer ") else ""


def _caller(run_id: int, phase=None):
    job = db.session.get(ClusterProvisionJob, int(run_id))
    if job is None:
        return None, (jsonify({"error": "unauthorized"}), 401)
    why = remote.authenticate(job, _bearer(), phase)
    if why is not None:
        remote.audit("provisioner-refused", job=job, reason=why, path=request.path)
        return None, (jsonify({"error": "unauthorized"}), 401)
    return job, None


def _body():
    data = request.get_json(silent=True)
    return data if isinstance(data, dict) else {}


@provisioner_runs_bp.route("/api/internal/provisioner/runs/<int:run_id>", methods=["GET"])
def run_spec(run_id: int):
    phase = request.args.get("phase") or ""
    if phase not in remote.PHASES:
        return jsonify({"error": "phase must be plan, apply or bind"}), 400
    job, err = _caller(run_id, phase)
    if err:
        return err
    try:
        return jsonify(remote.run_spec(job, phase))
    except PermissionError as exc:
        return jsonify({"error": str(exc)}), 403
    except remote.RemoteError as exc:
        return jsonify({"error": str(exc)}), 409


def _receiver(run_id: int, phase: str, handler):
    job, err = _caller(run_id, phase)
    if err:
        return err
    try:
        handler(job, _body())
    except ValueError as exc:
        db.session.rollback()
        return jsonify({"error": str(exc)}), 400
    return jsonify({"ok": True})


@provisioner_runs_bp.route("/api/internal/provisioner/runs/<int:run_id>/plan", methods=["POST"])
def plan(run_id: int):
    return _receiver(run_id, "plan", remote.record_plan)


@provisioner_runs_bp.route("/api/internal/provisioner/runs/<int:run_id>/facts", methods=["POST"])
def facts(run_id: int):
    return _receiver(run_id, "apply", remote.record_facts)


@provisioner_runs_bp.route("/api/internal/provisioner/runs/<int:run_id>/errored-state", methods=["POST"])
def errored_state(run_id: int):
    return _receiver(run_id, "apply", remote.record_errored_state)


@provisioner_runs_bp.route("/api/internal/provisioner/runs/<int:run_id>/bound", methods=["POST"])
def bound(run_id: int):
    return _receiver(run_id, "bind", remote.record_bound)


@provisioner_runs_bp.route("/api/internal/provisioner/runs/<int:run_id>/failed", methods=["POST"])
def failed(run_id: int):
    phase = str(_body().get("phase") or "")
    if phase not in remote.PHASES:
        return jsonify({"error": "phase must be plan, apply or bind"}), 400
    return _receiver(run_id, phase, remote.record_failed)
