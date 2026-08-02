"""Closing analyses that stopped reporting.

An analysis runs in a container that reports back over a callback. Nothing
watched for the container that never calls -- it died, its host went away, or
the process restarted before the first callback -- so the row stayed
non-terminal forever, reading as "Running" to an operator who had no way to tell
it from a real one.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from api.db import db
from api.models import AuditLog, User
from api.models_application_intelligence import (
    ApplicationAnalysis,
    BitbucketCredentialProfile,
    IntelligenceApplication,
)
from api.services import application_intelligence_service as svc


@pytest.fixture()
def ctx(app):
    with app.app_context():
        yield


_seq = [0]


def _analysis(status="Running", age_hours=0, stage="scanning"):
    """Build the object graph an analysis needs: profile -> application -> run.

    Heavier than the test wants, but the FKs are non-null and inventing a
    lighter fixture would test something other than the real rows.
    """
    _seq[0] += 1
    n = _seq[0]
    admin = User.query.filter_by(username="admin").first()

    profile = BitbucketCredentialProfile(
        name=f"profile-{n}", credential_type="token", secret_cipher="x"
    )
    db.session.add(profile)
    db.session.flush()

    application = IntelligenceApplication(
        name=f"app-{n}",
        slug=f"app-{n}",
        repository_url="https://bitbucket.org/acme/app",
        repository_workspace="acme",
        repository_name="app",
        credential_profile_id=profile.id,
        created_by_user_id=admin.id,
    )
    db.session.add(application)
    db.session.flush()

    analysis = ApplicationAnalysis(
        application_id=application.id,
        status=status,
        analysis_mode="local",
        requested_by_user_id=admin.id,
        current_stage=stage,
        created_at=datetime.now(timezone.utc) - timedelta(hours=age_hours),
    )
    db.session.add(analysis)
    db.session.commit()
    return analysis


def test_an_analysis_that_stopped_reporting_is_failed(ctx):
    analysis = _analysis(age_hours=12)
    assert svc.reap_stale_analyses() == 1

    db.session.refresh(analysis)
    assert analysis.status == "Failed"
    assert analysis.failed_at is not None
    assert "stopped reporting" in analysis.safe_error_message
    assert analysis.failure_stage == "scanning", "the stage it died in is the useful detail"


def test_a_slow_analysis_is_left_alone(ctx):
    """Killing a slow analysis is worse than leaving a dead one a bit longer."""
    analysis = _analysis(age_hours=1)
    assert svc.reap_stale_analyses() == 0
    db.session.refresh(analysis)
    assert analysis.status == "Running"


@pytest.mark.parametrize("status", ["Completed", "Failed", "Cancelled"])
def test_terminal_analyses_are_never_touched(ctx, status):
    analysis = _analysis(status=status, age_hours=99)
    assert svc.reap_stale_analyses() == 0
    db.session.refresh(analysis)
    assert analysis.status == status


def test_queued_analyses_are_reaped_too(ctx):
    """A worker that died before starting leaves Queued, not Running."""
    analysis = _analysis(status="Queued", age_hours=12, stage=None)
    assert svc.reap_stale_analyses() == 1
    db.session.refresh(analysis)
    assert analysis.status == "Failed"
    assert analysis.failure_stage == "unknown"


def test_it_is_failed_not_cancelled(ctx):
    """Cancelled means a person decided to stop it. Nobody did."""
    analysis = _analysis(age_hours=12)
    svc.reap_stale_analyses()
    db.session.refresh(analysis)
    assert analysis.status != "Cancelled"


def test_the_timeout_is_audited(ctx):
    analysis = _analysis(age_hours=12)
    svc.reap_stale_analyses()

    entry = AuditLog.query.filter_by(action="application_analysis_timed_out").one()
    assert entry.target_id == str(analysis.id)
    assert entry.details["ageSeconds"] > 0


def test_reaping_unblocks_deleting_the_application(ctx):
    """The operator-visible consequence: a non-terminal analysis blocks delete."""
    analysis = _analysis(age_hours=12)
    admin = User.query.filter_by(username="admin").first()

    with pytest.raises(ValueError, match="Cancel active analyses"):
        svc.delete_application(analysis.application_id, admin)

    svc.reap_stale_analyses()
    svc.delete_application(analysis.application_id, admin)


def test_reaping_is_idempotent(ctx):
    _analysis(age_hours=12)
    assert svc.reap_stale_analyses() == 1
    assert svc.reap_stale_analyses() == 0, "already terminal on the second pass"
