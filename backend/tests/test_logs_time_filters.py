"""Logs API time-range filtering."""

from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from api.log_time_filters import (
    advance_log_cursor,
    filter_log_lines_after,
    filter_log_lines_until,
    parse_log_time_filters,
    parse_rfc3339,
)
from tests.conftest import auth_headers

LOGS_URL = "/api/logs"


def _logs_params(**extra):
    return {
        "cluster": "prod-us-east",
        "namespace": "payments",
        "pod": "payments-api-84b5d5",
        **extra,
    }


def test_parse_log_time_filters_accepts_quick_ranges():
    filters, error = parse_log_time_filters("900", "", "")
    assert error is None
    assert filters is not None
    assert filters.since_seconds == 900
    assert filters.since_time is None
    assert filters.until_time is None


def test_parse_log_time_filters_rejects_invalid_since_seconds():
    _, error = parse_log_time_filters("120", "", "")
    assert error is not None
    assert "sinceSeconds" in error


def test_parse_log_time_filters_rejects_mixed_params():
    _, error = parse_log_time_filters(
        "900",
        "2024-01-01T00:00:00Z",
        "2024-01-01T01:00:00Z",
    )
    assert error is not None
    assert "Cannot combine" in error


def test_parse_log_time_filters_validates_custom_order():
    _, error = parse_log_time_filters(
        "",
        "2024-01-02T00:00:00Z",
        "2024-01-01T00:00:00Z",
    )
    assert error is not None
    assert "before" in error


def test_parse_log_time_filters_validates_max_custom_range():
    start = "2024-01-01T00:00:00Z"
    end = "2024-01-10T00:00:00Z"
    _, error = parse_log_time_filters("", start, end)
    assert error is not None
    assert "7 days" in error


def test_filter_log_lines_after_accepts_space_separated_timestamps():
    lines = [
        "2024-01-01 10:00:00Z first",
        "2024-01-01 11:00:00Z second",
        "2024-01-01T12:00:00Z third",
    ]
    since = parse_rfc3339("2024-01-01T11:00:00Z")
    filtered = filter_log_lines_after(lines, since)
    assert filtered == ["2024-01-01T12:00:00Z third"]


def test_advance_log_cursor_moves_forward_one_millisecond():
    from api.log_time_filters import format_rfc3339_z

    since = parse_rfc3339("2024-01-01T11:00:00.123Z")
    advanced = advance_log_cursor(since)
    assert format_rfc3339_z(advanced) == "2024-01-01T11:00:00.124Z"


def test_filter_log_lines_until():
    lines = [
        "2024-01-01T10:00:00Z first",
        "2024-01-01T11:00:00Z second",
        "2024-01-01T12:00:00Z third",
    ]
    until = parse_rfc3339("2024-01-01T11:00:00Z")
    filtered = filter_log_lines_until(lines, until)
    assert filtered == lines[:2]


def test_logs_endpoint_returns_validation_error(client, admin_token):
    response = client.get(
        LOGS_URL,
        query_string=_logs_params(sinceSeconds="120"),
        headers=auth_headers(admin_token),
    )
    assert response.status_code == 400
    payload = response.get_json()
    assert payload["success"] is False
    assert "sinceSeconds" in payload["error"]


def test_logs_endpoint_accepts_since_seconds_in_mock_mode(client, admin_token):
    response = client.get(
        LOGS_URL,
        query_string=_logs_params(sinceSeconds="900", live="true"),
        headers=auth_headers(admin_token),
    )
    assert response.status_code == 200
    data = response.get_json()["data"]
    assert data["query"]["sinceSeconds"] == 900
    assert data["lines"]


def test_logs_endpoint_filters_mock_lines_by_since_seconds(client, admin_token):
    response = client.get(
        LOGS_URL,
        query_string=_logs_params(sinceSeconds="900"),
        headers=auth_headers(admin_token),
    )
    assert response.status_code == 200
    data = response.get_json()["data"]
    assert data["query"]["sinceSeconds"] == 900
    assert len(data["lines"]) >= 1


def test_logs_endpoint_without_time_filters_keeps_mock_behavior(client, admin_token):
    response = client.get(
        LOGS_URL,
        query_string=_logs_params(live="true"),
        headers=auth_headers(admin_token),
    )
    assert response.status_code == 200
    data = response.get_json()["data"]
    assert "sinceSeconds" not in data["query"]
    assert data["stream"] == "live"


@patch("api.routes.logs.fetch_pod_logs")
def test_logs_endpoint_passes_time_filters_to_k8s(
    mock_fetch_logs,
    client,
    admin_token,
):
    mock_fetch_logs.return_value = ({"query": {}, "stream": "snapshot", "lines": []}, None)
    since = datetime.now(timezone.utc) - timedelta(hours=1)
    until = datetime.now(timezone.utc)
    response = client.get(
        LOGS_URL,
        query_string=_logs_params(
            sinceTime=since.isoformat().replace("+00:00", "Z"),
            untilTime=until.isoformat().replace("+00:00", "Z"),
            live="true",
        ),
        headers=auth_headers(admin_token),
    )
    assert response.status_code == 200
    mock_fetch_logs.assert_called_once()
    kwargs = mock_fetch_logs.call_args.kwargs
    params = kwargs["params"]
    assert params["live"] is True
    assert params["time_filters"].since_time is not None
    assert params["time_filters"].until_time is not None


@patch("api.routes.logs.fetch_pod_logs")
def test_logs_endpoint_passes_since_seconds_to_k8s_in_live_mode(
    mock_fetch_logs,
    client,
    admin_token,
):
    mock_fetch_logs.return_value = ({"query": {}, "stream": "live", "lines": []}, None)
    response = client.get(
        LOGS_URL,
        query_string=_logs_params(sinceSeconds="900", live="true"),
        headers=auth_headers(admin_token),
    )
    assert response.status_code == 200
    mock_fetch_logs.assert_called_once()
    params = mock_fetch_logs.call_args.kwargs["params"]
    assert params["time_filters"].since_seconds == 900
    assert params["live"] is True
