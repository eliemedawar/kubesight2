"""kubesight_ticket_history: past tickets like this one, and how each ended.

What the ticket agent learns from is KubeSight's own record — what Hermes
understood, the diagnosis it gave, what the approvers decided and how the run
ended, failure text included. Tested here:

* matching by words, application and environment; the ticket in hand is left out;
* with only ticketRecordId, it searches for tickets like that one;
* problemsOnly keeps failed runs, rejected approvals and agent errors;
* tickets nothing was done on are skipped;
* it is read-only, reachable through MCP with ticketing:view.
"""

from datetime import datetime, timedelta, timezone

from api.db import db
from api.models import DeployAutomationRun, TicketInterpretation
from api.services.ticket_agent import history

from tests.test_ticket_agent import (  # noqa: F401  (fixtures are used by name)
    CLUSTER,
    DEPLOYMENT,
    NAMESPACE,
    _inbound_secret,
    _ticket,
    agent,
)


def _task(ticket, **fields):
    task = TicketInterpretation(
        ticket_record_id=ticket.id, provider="zoho", kind=fields.pop("kind", "handle"),
        ticket_number=ticket.ticket_number, **fields,
    )
    db.session.add(task)
    db.session.commit()
    return task


def _run(ticket, status, error=None, tag="v1"):
    run = DeployAutomationRun(
        ticket_record_id=ticket.id, ticket_number=ticket.ticket_number, cluster_id=CLUSTER,
        namespace=NAMESPACE, deployment_name=DEPLOYMENT, image_tag=tag, ticket_tag=tag,
        status=status, error=error, triggered_by="hermes",
    )
    db.session.add(run)
    db.session.commit()
    return run


def _ids(result):
    return [t["ticketRecordId"] for t in result["tickets"]]


def test_it_finds_what_happened_to_tickets_like_this_one(agent):
    failed = _ticket(tag="v1", subject="Deploy payments-api v1 to payments")
    _task(failed, route="execute", status="executed", confidence="High",
          understanding="Deploy payments-api v1 to payments.", namespace=NAMESPACE,
          deployment_name=DEPLOYMENT)
    _run(failed, "failed", error="ImagePullBackOff: manifest for payments-api:v1 not found")

    answered = _ticket(tag="", subject="payments-api returns 502 since this morning")
    _task(answered, route="answered", status="done", confidence="High",
          understanding="DB pool exhausted.", namespace=NAMESPACE,
          decision={"troubleshooting": {"diagnosis": "Connection pool exhausted on payments-db.",
                                        "recommendation": "Raise DB_POOL_SIZE."}})

    untouched = _ticket(tag="v3", subject="Deploy payments-api v3 to payments")  # never handled
    current = _ticket(tag="v2", subject="Deploy payments-api v2 to payments")

    found = history.similar({"ticketRecordId": current.id})
    ids = _ids(found)
    assert current.id not in ids and untouched.id not in ids
    assert failed.id in ids

    failed_entry = next(t for t in found["tickets"] if t["ticketRecordId"] == failed.id)
    assert failed_entry["result"] == "deploy_failed"
    assert "ImagePullBackOff" in failed_entry["runs"][0]["error"]
    assert failed_entry["agent"][0]["understanding"].startswith("Deploy payments-api v1")

    by_words = history.similar({"query": "502 errors on payments-api"})
    entry = next(t for t in by_words["tickets"] if t["ticketRecordId"] == answered.id)
    assert entry["result"] == "answered"
    assert entry["agent"][0]["diagnosis"].startswith("Connection pool exhausted")
    assert entry["agent"][0]["recommendation"] == "Raise DB_POOL_SIZE."


def test_filters_and_problems_only(agent):
    ok = _ticket(tag="v5", subject="Deploy payments-api v5")
    _task(ok, route="execute", status="executed", namespace=NAMESPACE, deployment_name=DEPLOYMENT)
    _run(ok, "deployed", tag="v5")

    rejected = _ticket(tag="v6", subject="Deploy payments-api v6")
    _task(rejected, route="approval", status="rejected", decided_by="elie",
          decision_note="Freeze week", namespace=NAMESPACE, deployment_name=DEPLOYMENT)

    problems = history.similar({"application": DEPLOYMENT, "problemsOnly": True})
    assert _ids(problems) == [rejected.id]
    assert problems["tickets"][0]["result"] == "approval_rejected"
    assert problems["tickets"][0]["agent"][0]["decisionNote"] == "Freeze week"

    assert set(_ids(history.similar({"environment": NAMESPACE}))) >= {ok.id, rejected.id}
    assert _ids(history.similar({"environment": "no-such-env"})) == []
    assert history.similar({"application": DEPLOYMENT, "limit": 1})["tickets"][0]["ticketRecordId"] in (
        ok.id, rejected.id)


def test_bad_arguments_are_refused(agent):
    import pytest

    with pytest.raises(history.HistoryError):
        history.similar({"limit": "lots"})
    with pytest.raises(history.HistoryError):
        history.similar({"query": ["not", "a", "string"]})


def test_through_mcp(client, agent, admin_token):
    from tests.test_mcp_server import call_tool

    done = _ticket(tag="v7", subject="Deploy payments-api v7 to payments")
    _task(done, route="execute", status="executed", namespace=NAMESPACE, deployment_name=DEPLOYMENT)
    _run(done, "deployed", tag="v7")

    got = call_tool(client, admin_token, "kubesight_ticket_history", {"application": DEPLOYMENT})
    assert not got.get("isError"), got
    assert got["structuredContent"]["tickets"][0]["ticketRecordId"] == done.id
    assert got["structuredContent"]["tickets"][0]["result"] == "deployed"
