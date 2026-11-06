"""The in-app assistant (Ctrl+G): admins chat with Hermes, Hermes is faked at ``hermes.chat``.

In TESTING there is no worker pool, so a reply is produced inline during the
send request and the response already carries it.
"""

from datetime import datetime, timedelta, timezone

import pytest

from api.db import db
from api.models import AssistantConversation, AssistantMessage, User
from api.services.assistant import hermes
from api.services.application_intelligence_hermes import HermesTransientError

from .conftest import auth_headers

URL = "/api/assistant/conversations"


@pytest.fixture()
def configured(monkeypatch):
    monkeypatch.setenv("ASSISTANT_HERMES_URL", "https://hermes.example/v1/chat/completions")
    monkeypatch.setenv("ASSISTANT_HERMES_TOKEN", "hermes-key")


@pytest.fixture()
def calls(monkeypatch, configured):
    log = []

    def fake_chat(messages, session_key, on_progress=None):
        log.append({"messages": messages, "session_key": session_key})
        return f"answer {len(log)}", "hermes-agent"

    monkeypatch.setattr(hermes, "chat", fake_chat)
    return log


def _send(client, token, message, conversation_id=None, context=None):
    url = URL if conversation_id is None else f"{URL}/{conversation_id}/messages"
    return client.post(url, json={"message": message, "context": context}, headers=auth_headers(token))


def test_admin_asks_and_gets_the_reply(client, admin_token, calls):
    response = _send(
        client, admin_token, "why is payments-api crashing?",
        context={"pageLabel": "Pods", "clusterName": "prod-us-east", "namespace": "payments"},
    )
    assert response.status_code == 201
    data = response.get_json()["data"]
    assert data["title"] == "why is payments-api crashing?"
    assert [(m["role"], m["status"], m["content"]) for m in data["messages"]] == [
        ("user", "done", "why is payments-api crashing?"),
        ("assistant", "done", "answer 1"),
    ]
    assert data["pending"] is False

    system = calls[0]["messages"][0]
    assert system["role"] == "system"
    assert "Cluster: prod-us-east" in system["content"]
    assert "Namespace: payments" in system["content"]
    assert calls[0]["session_key"].startswith("kubesight-assistant-user-")


def test_the_conversation_so_far_travels_with_each_turn(client, admin_token, calls):
    first = _send(client, admin_token, "list my clusters").get_json()["data"]
    second = _send(client, admin_token, "and the first one?", conversation_id=first["id"])
    assert second.status_code == 200
    sent = [(m["role"], m["content"]) for m in calls[1]["messages"][1:]]
    assert sent == [
        ("user", "list my clusters"),
        ("assistant", "answer 1"),
        ("user", "and the first one?"),
    ]
    listed = client.get(URL, headers=auth_headers(admin_token)).get_json()["data"]
    assert [c["id"] for c in listed] == [first["id"]]


@pytest.mark.parametrize("who", ["viewer_token", "operator_token"])
def test_only_admins_may_use_it(client, request, calls, who):
    token = request.getfixturevalue(who)
    assert _send(client, token, "hello").status_code == 403
    assert client.get(URL, headers=auth_headers(token)).status_code == 403
    assert client.get("/api/assistant/status", headers=auth_headers(token)).status_code == 403
    assert calls == []


def test_another_users_conversation_is_not_found(client, admin_token, calls):
    viewer = User.query.filter_by(username="viewer").first()
    theirs = AssistantConversation(user_id=viewer.id, title="private")
    db.session.add(theirs)
    db.session.commit()
    headers = auth_headers(admin_token)
    assert client.get(f"{URL}/{theirs.id}", headers=headers).status_code == 404
    assert _send(client, admin_token, "hi", conversation_id=theirs.id).status_code == 404
    assert client.delete(f"{URL}/{theirs.id}", headers=headers).status_code == 404
    assert client.get(URL, headers=headers).get_json()["data"] == []


def test_not_configured_says_what_to_set(client, admin_token, monkeypatch):
    for name in ("ASSISTANT_HERMES_URL", "TICKET_AGENT_HERMES_URL"):
        monkeypatch.delenv(name, raising=False)
    status = client.get("/api/assistant/status", headers=auth_headers(admin_token)).get_json()["data"]
    assert status["configured"] is False
    assert "ASSISTANT_HERMES_URL" in status["hint"]
    response = _send(client, admin_token, "hello")
    assert response.status_code == 503
    assert AssistantConversation.query.count() == 0


def test_falls_back_to_the_ticket_agents_hermes(monkeypatch):
    monkeypatch.delenv("ASSISTANT_HERMES_URL", raising=False)
    monkeypatch.delenv("ASSISTANT_HERMES_TOKEN", raising=False)
    monkeypatch.setenv("TICKET_AGENT_HERMES_URL", "https://hermes.example/v1/chat/completions")
    monkeypatch.setenv("TICKET_AGENT_HERMES_TOKEN", "ticket-key")
    assert hermes.is_configured()


def test_a_hermes_failure_is_shown_on_the_reply(client, admin_token, configured, monkeypatch):
    def down(messages, session_key, on_progress=None):
        raise HermesTransientError("Hermes is unavailable or took too long to answer.")

    monkeypatch.setattr(hermes, "chat", down)
    data = _send(client, admin_token, "hello").get_json()["data"]
    reply = data["messages"][-1]
    assert reply["status"] == "failed"
    assert "unavailable" in reply["error"]
    # A failed reply does not block the next question, and is not sent as history.
    log = []
    monkeypatch.setattr(hermes, "chat", lambda messages, session_key, on_progress=None: (log.append(messages), ("ok", "m"))[1])
    again = _send(client, admin_token, "hello again", conversation_id=data["id"])
    assert again.status_code == 200
    assert [m["role"] for m in log[0][1:]] == ["user", "user"]


def test_one_question_at_a_time(client, admin_token, calls):
    data = _send(client, admin_token, "first").get_json()["data"]
    db.session.add(AssistantMessage(conversation_id=data["id"], role="assistant", status="pending"))
    db.session.commit()
    assert _send(client, admin_token, "second", conversation_id=data["id"]).status_code == 409


def test_a_reply_left_pending_by_a_restart_is_closed(client, admin_token, calls):
    data = _send(client, admin_token, "first").get_json()["data"]
    old = datetime.now(timezone.utc) - timedelta(seconds=hermes.timeout_seconds() + 300)
    db.session.add(AssistantMessage(conversation_id=data["id"], role="assistant", status="pending", created_at=old))
    db.session.commit()
    fetched = client.get(f"{URL}/{data['id']}", headers=auth_headers(admin_token)).get_json()["data"]
    assert fetched["pending"] is False
    assert fetched["messages"][-1]["status"] == "failed"
    assert "Ask again" in fetched["messages"][-1]["error"]


def test_delete_removes_the_conversation(client, admin_token, calls):
    data = _send(client, admin_token, "first").get_json()["data"]
    assert client.delete(f"{URL}/{data['id']}", headers=auth_headers(admin_token)).status_code == 200
    assert AssistantConversation.query.count() == 0
    assert AssistantMessage.query.count() == 0


def test_empty_and_oversized_messages_are_refused(client, admin_token, calls):
    assert _send(client, admin_token, "   ").status_code == 400
    assert _send(client, admin_token, "x" * 9000).status_code == 400
    assert calls == []


class _FakeStream:
    """What urlopen returns for a streamed Hermes reply."""

    def __init__(self, body: str, content_type="text/event-stream"):
        self.headers = {"Content-Type": content_type}
        self._lines = [line.encode() for line in body.splitlines(keepends=True)]
        self._body = body.encode()

    def __iter__(self):
        return iter(self._lines)

    def read(self, _limit=None):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _chunk(content=None, finish=None):
    import json

    delta = {"content": content} if content is not None else {}
    return "data: " + json.dumps({"choices": [{"delta": delta, "finish_reason": finish}]}) + "\n\n"


def test_a_streamed_reply_is_read_with_tool_progress(configured, monkeypatch):
    body = (
        _chunk()
        + 'event: hermes.tool.progress\ndata: {"tool": "kubesight_pods_list", "label": "Listing pods in payments", "status": "running"}\n\n'
        + ": keepalive\n\n"
        + _chunk("Two pods ")
        + _chunk("are failing.")
        + _chunk(finish="stop")
        + "data: [DONE]\n\n"
    )
    monkeypatch.setattr(hermes, "urlopen", lambda request, timeout: _FakeStream(body))
    seen = []
    text, _ = hermes.chat([{"role": "user", "content": "hi"}], "key", on_progress=lambda **kw: seen.append(kw))
    assert text == "Two pods are failing."
    assert seen == [
        {"activity": "Listing pods in payments"},
        {"text": "Two pods "},
        {"text": "Two pods are failing."},
    ]


def test_a_stream_that_breaks_off_is_an_error(configured, monkeypatch):
    body = _chunk("Half an ans") + _chunk(finish="error") + "data: [DONE]\n\n"
    monkeypatch.setattr(hermes, "urlopen", lambda request, timeout: _FakeStream(body))
    with pytest.raises(HermesTransientError):
        hermes.chat([{"role": "user", "content": "hi"}], "key")


def test_a_plain_json_reply_still_works(configured, monkeypatch):
    import json

    body = json.dumps({"choices": [{"message": {"role": "assistant", "content": "ok"}}]})
    monkeypatch.setattr(hermes, "urlopen", lambda request, timeout: _FakeStream(body, "application/json"))
    assert hermes.chat([{"role": "user", "content": "hi"}], "key")[0] == "ok"


def test_the_reply_row_fills_in_while_hermes_streams(client, admin_token, configured, monkeypatch):
    from api.services.assistant import service

    monkeypatch.setattr(service, "PROGRESS_WRITE_SECONDS", 0)
    seen_mid_stream = {}

    def streaming(messages, session_key, on_progress=None):
        on_progress(activity="Reading logs of payments-api")
        row = AssistantMessage.query.filter_by(status="pending").one()
        seen_mid_stream["activity"] = row.activity
        on_progress(text="Partial answer")
        db.session.expire_all()
        seen_mid_stream["content"] = AssistantMessage.query.filter_by(status="pending").one().content
        return "Partial answer, finished.", "hermes-agent"

    monkeypatch.setattr(hermes, "chat", streaming)
    data = _send(client, admin_token, "why?").get_json()["data"]
    assert seen_mid_stream == {"activity": "Reading logs of payments-api", "content": "Partial answer"}
    reply = data["messages"][-1]
    assert reply["content"] == "Partial answer, finished."
    assert reply["activity"] is None
