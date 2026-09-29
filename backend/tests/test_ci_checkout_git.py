"""Run the Kubernetes checkout script against real, shallow Git repositories."""

import os
from pathlib import Path
import shutil
import shlex
import subprocess

import pytest

from api.services.ci.runners.kubernetes import _CHECKOUT_SCRIPT


GIT = shutil.which("git")
SH = shutil.which("sh")
if not SH and GIT and os.name == "nt":
    git_sh = Path(GIT).parent.parent / "bin" / "sh.exe"
    if git_sh.exists():
        SH = str(git_sh)

pytestmark = pytest.mark.skipif(not GIT or not SH, reason="requires Git and a POSIX shell")


@pytest.fixture
def repository(tmp_path):
    repo = tmp_path / "origin"
    repo.mkdir()
    env = dict(os.environ, GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull)

    def git(*args):
        return subprocess.check_output(
            [GIT, "-c", "user.name=Checkout test", "-c", "user.email=checkout@example.test", *args],
            cwd=repo, env=env, text=True, stderr=subprocess.STDOUT,
        ).strip()

    git("init", "-b", "main")
    (repo / "source.txt").write_text("default branch")
    git("add", ".")
    git("commit", "-m", "default")
    default = git("rev-parse", "HEAD")
    git("checkout", "-b", "test/merge-check")
    (repo / "source.txt").write_text("requested PR commit")
    git("commit", "-am", "PR commit")
    requested = git("rev-parse", "HEAD")
    git("tag", "release-test")
    (repo / "source.txt").write_text("newer PR commit")
    git("commit", "-am", "branch moved after webhook")
    latest = git("rev-parse", "HEAD")
    git("checkout", "main")
    return repo, env, default, requested, latest


@pytest.mark.parametrize("revision_kind", ["short", "full", "branch", "tag", "default", "missing"])
def test_checkout_resolves_requested_revision(tmp_path, repository, revision_kind):
    repo, env, default, requested, latest = repository
    revision, expected = {
        "short": (requested[:12], requested),
        "full": (requested, requested),
        "branch": ("test/merge-check", latest),
        "tag": ("release-test", requested),
        "default": ("", default),
        "missing": ("0" * 12, None),
    }[revision_kind]
    checkout = tmp_path / "checkout"
    # Run production authentication + checkout, stopping before the HTTP callback.
    script = _CHECKOUT_SCRIPT.split('COMMIT="$(git rev-parse HEAD)"', 1)[0]
    script = script.replace("/workspace/source", shlex.quote(checkout.as_posix()))
    env.update(
        KUBESIGHT_REPO_URL=repo.as_uri(),
        KUBESIGHT_GIT_TOKEN="local-test-only",
        KUBESIGHT_REVISION=revision,
        KUBESIGHT_BRANCH="test/merge-check",
    )
    result = subprocess.run([SH, "-c", script], env=env, text=True, capture_output=True, timeout=30)
    if expected is None:
        assert result.returncode != 0
        return
    assert result.returncode == 0, result.stdout + result.stderr
    actual = subprocess.check_output([GIT, "rev-parse", "HEAD"], cwd=checkout, text=True).strip()
    assert actual == expected
