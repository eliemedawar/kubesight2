import os
from pathlib import Path
from unittest.mock import MagicMock, patch

from api.services.deployment_service import (
    friendly_kubectl_diff_error,
    kubectl_external_diff_env_value,
    resolve_kubectl_external_diff,
)


def test_friendly_kubectl_diff_error_windows_path_message():
    message = friendly_kubectl_diff_error(
        'error: failed to run "diff": executable file not found in %PATH%'
    )
    assert "diff utility" in message
    assert "KUBECTL_EXTERNAL_DIFF" in message


def test_resolve_kubectl_external_diff_honors_env_override():
    with patch.dict(os.environ, {"KUBECTL_EXTERNAL_DIFF": r"C:\tools\diff.exe"}, clear=False):
        assert resolve_kubectl_external_diff() == r"C:\tools\diff.exe"


def test_resolve_kubectl_external_diff_finds_user_local_git_install():
    expected = str(
        Path(r"C:\Users\Example")
        / "Programs"
        / "Git"
        / "usr"
        / "bin"
        / "diff.exe"
    )
    git_diff = MagicMock()
    git_diff.is_file.return_value = True
    git_diff.__str__.return_value = expected
    with patch.dict(os.environ, {"LOCALAPPDATA": r"C:\Users\Example"}, clear=False):
        os.environ.pop("KUBECTL_EXTERNAL_DIFF", None)
        with patch("api.services.deployment_service.shutil.which", return_value=None):
            with patch(
                "api.services.deployment_service._windows_diff_candidates",
                return_value=[git_diff],
            ):
                assert resolve_kubectl_external_diff() == expected


def test_kubectl_external_diff_env_value_uses_short_path_on_windows():
    spaced = r"C:\Users\Elie Medawer\AppData\Local\Programs\Git\usr\bin\diff.exe"
    with patch("api.services.deployment_service.os.name", "nt"):
        with patch(
            "api.services.deployment_service._windows_short_path",
            return_value=r"C:\Users\ELIEME~1\AppData\Local\Programs\Git\usr\bin\diff.exe",
        ) as mock_short:
            result = kubectl_external_diff_env_value(spaced)
            mock_short.assert_called_once_with(spaced)
            assert " " not in result


def test_resolve_kubectl_external_diff_uses_path_lookup():
    with patch.dict(os.environ, {}, clear=False):
        os.environ.pop("KUBECTL_EXTERNAL_DIFF", None)
        with patch(
            "api.services.deployment_service.shutil.which",
            return_value="/usr/bin/diff",
        ):
            assert resolve_kubectl_external_diff() == "/usr/bin/diff"
