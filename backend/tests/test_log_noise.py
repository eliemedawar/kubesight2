"""Health probe log noise filtering."""

from api.log_noise import (
    filter_health_probe_log_lines,
    filter_live_log_noise,
    filter_logs_api_self_lines,
    is_health_probe_log_line,
    is_logs_api_self_line,
)


def test_is_health_probe_log_line():
    assert is_health_probe_log_line('2026-06-08T11:04:32Z 10.244.0.1 - - "GET /health HTTP/1.1" 200 -')
    assert is_health_probe_log_line('2026-06-08T11:04:32Z "HEAD /healthz HTTP/1.1" 200')
    assert not is_health_probe_log_line("2026-06-08T11:04:32Z INFO payment processed")


def test_filter_health_probe_log_lines():
    lines = [
        '2026-06-08T11:04:32Z 10.244.0.1 - - "GET /health HTTP/1.1" 200 -',
        "2026-06-08T11:04:33Z INFO payment processed",
        '2026-06-08T11:04:34Z "GET /readyz HTTP/1.1" 200',
    ]
    filtered = filter_health_probe_log_lines(lines)
    assert filtered == ["2026-06-08T11:04:33Z INFO payment processed"]


def test_filter_health_probe_log_lines_keeps_probe_only_tail():
    probe_only = [
        '2026-06-08T11:04:32Z 10.244.0.1 - - "GET /health HTTP/1.1" 200 -',
        '2026-06-08T11:04:33Z "GET /readyz HTTP/1.1" 200',
    ]
    assert filter_health_probe_log_lines(probe_only) == probe_only


def test_is_logs_api_self_line():
    assert is_logs_api_self_line(
        '2026-06-08T11:04:32Z 127.0.0.1 - - "GET /api/logs?cluster=docker-desktop HTTP/1.1" 200 -'
    )
    assert is_logs_api_self_line(
        '2026-06-08T11:04:32Z 127.0.0.1 - - "GET /api/clusters/docker-desktop/namespaces/default/pods/backend-api/containers/backend-api/logs HTTP/1.1" 200 -'
    )
    assert is_logs_api_self_line(
        "2026-06-08T16:08:31.123Z INFO:kubesight.api:GET /api/clusters/docker-desktop/namespaces/kubesight/pods/backend/containers/backend/logs 200 5ms"
    )
    assert is_logs_api_self_line(
        "2026-06-08T16:08:31.123Z GET /api/logs?cluster=docker-desktop&live=true 200 5ms"
    )
    assert not is_logs_api_self_line("2026-06-08T11:04:32Z INFO payment processed")


def test_filter_logs_api_self_lines():
    lines = [
        '2026-06-08T11:04:32Z 127.0.0.1 - - "GET /api/logs?pod=backend HTTP/1.1" 200 -',
        "2026-06-08T11:04:33Z INFO payment processed",
    ]
    assert filter_logs_api_self_lines(lines) == ["2026-06-08T11:04:33Z INFO payment processed"]


def test_filter_live_log_noise_removes_probe_and_logs_api_lines():
    lines = [
        '2026-06-08T11:04:32Z 10.244.0.1 - - "GET /health HTTP/1.1" 200 -',
        '2026-06-08T11:04:33Z 127.0.0.1 - - "GET /api/logs HTTP/1.1" 200 -',
        "2026-06-08T11:04:34Z INFO payment processed",
    ]
    assert filter_live_log_noise(lines) == ["2026-06-08T11:04:34Z INFO payment processed"]


def test_filter_live_log_noise_does_not_replay_logs_api_only_tail():
    logs_api_only = [
        "2026-06-08T16:08:31.123Z GET /api/clusters/docker-desktop/namespaces/kubesight/pods/backend/containers/backend/logs 200 5ms",
        '2026-06-08T16:08:34.123Z 127.0.0.1 - - "GET /api/logs?live=true HTTP/1.1" 200 -',
    ]
    assert filter_live_log_noise(logs_api_only) == []
