from types import SimpleNamespace

import pytest

from api.services import application_analysis_local_docker as local_docker


def test_worker_runtime_preflight_imports_both_entry_points(monkeypatch):
    calls = []

    def fake_docker(*args, **kwargs):
        calls.append((args, kwargs))
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(local_docker, "_docker", fake_docker)

    local_docker._require_worker_runtime("worker:test")

    assert calls == [
        (
            (
                "run",
                "--rm",
                "--entrypoint",
                "python",
                "worker:test",
                "-c",
                "import api.application_checkout; import api.application_worker",
            ),
            {"timeout": 60},
        )
    ]


def test_worker_runtime_preflight_rejects_stale_image(monkeypatch):
    monkeypatch.setattr(
        local_docker,
        "_docker",
        lambda *args, **kwargs: SimpleNamespace(returncode=1),
    )

    with pytest.raises(local_docker.LocalDockerLaunchError, match="incompatible"):
        local_docker._require_worker_runtime("worker:stale")
