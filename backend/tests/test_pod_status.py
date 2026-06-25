"""Unit tests for kubectl-style pod display status derivation."""

from api.k8s_provider import compute_pod_display_status, is_failed_pod_status


def _pod(phase, container_states=None, *, conditions=None, deletion=None, reason=None):
    container_statuses = []
    for name, state, ready in container_states or []:
        container_statuses.append({"name": name, "state": state, "ready": ready, "restartCount": 0})
    meta = {"name": "p", "namespace": "ns"}
    if deletion:
        meta["deletionTimestamp"] = deletion
    status = {"phase": phase, "containerStatuses": container_statuses}
    if conditions is not None:
        status["conditions"] = conditions
    if reason is not None:
        status["reason"] = reason
    return {"metadata": meta, "spec": {"containers": [{"name": "c"}]}, "status": status}


def test_running_and_ready_stays_running():
    pod = _pod("Running", [("c", {"running": {}}, True)])
    assert compute_pod_display_status(pod) == "Running"


def test_crashloop_reported_despite_running_phase():
    pod = _pod("Running", [("c", {"waiting": {"reason": "CrashLoopBackOff"}}, False)])
    assert compute_pod_display_status(pod) == "CrashLoopBackOff"


def test_image_pull_error_reported():
    pod = _pod("Pending", [("c", {"waiting": {"reason": "ImagePullBackOff"}}, False)])
    assert compute_pod_display_status(pod) == "ImagePullBackOff"


def test_terminated_non_zero_uses_reason():
    pod = _pod("Running", [("c", {"terminated": {"reason": "Error", "exitCode": 1}}, False)])
    assert compute_pod_display_status(pod) == "Error"


def test_terminated_no_reason_uses_exit_code():
    pod = _pod("Failed", [("c", {"terminated": {"exitCode": 137}}, False)])
    assert compute_pod_display_status(pod) == "ExitCode:137"


def test_completed_with_running_sidecar_not_ready():
    pod = _pod(
        "Running",
        [
            ("done", {"terminated": {"reason": "Completed", "exitCode": 0}}, False),
            ("side", {"running": {}}, True),
        ],
        conditions=[{"type": "Ready", "status": "False"}],
    )
    assert compute_pod_display_status(pod) == "NotReady"


def test_deletion_timestamp_is_terminating():
    pod = _pod("Running", [("c", {"running": {}}, True)], deletion="2026-06-25T00:00:00Z")
    assert compute_pod_display_status(pod) == "Terminating"


def test_is_failed_pod_status_classification():
    assert is_failed_pod_status("CrashLoopBackOff")
    assert is_failed_pod_status("ImagePullBackOff")
    assert is_failed_pod_status("Error")
    assert is_failed_pod_status("ExitCode:1")
    assert not is_failed_pod_status("Running")
    assert not is_failed_pod_status("Completed")
    assert not is_failed_pod_status("Pending")
