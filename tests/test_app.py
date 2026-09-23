"""
Unit tests for the demo app.

These run in the "test" job of the Deploy workflow, BEFORE anything is built or
pushed. If any of them fail, no image reaches ECR and nothing reaches ECS — that
is principle #2, "only tested images ship".
"""

import json
import logging

import pytest
from fastapi.testclient import TestClient

from app.logging_config import JsonFormatter
from app.main import APP_COLOR, BANNER_MESSAGE, app

client = TestClient(app)

# Every environment variable the app understands. Cleared before each test so
# one test's setting can never leak into the next.
DEMO_VARS = ("GIT_SHA", "APP_ENV", "FAIL_HEALTH", "SIMULATE_ERRORS")


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    """Start every test from a known-empty environment."""
    for name in DEMO_VARS:
        monkeypatch.delenv(name, raising=False)


def test_home_page_renders_banner_and_version(monkeypatch):
    """GET / returns an HTML page containing the banner, colour and version."""
    monkeypatch.setenv("GIT_SHA", "abc1234")
    monkeypatch.setenv("APP_ENV", "prod")

    response = client.get("/")

    assert response.status_code == 200
    assert "text/html" in response.headers["content-type"]
    body = response.text
    assert BANNER_MESSAGE in body
    assert APP_COLOR in body
    assert "abc1234" in body
    assert "prod" in body


def test_health_ok_by_default(monkeypatch):
    """GET /health is 200 with the running version when nothing is overridden."""
    monkeypatch.setenv("GIT_SHA", "deadbee")

    response = client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok", "version": "deadbee"}


def test_health_returns_503_when_fail_health_is_true(monkeypatch):
    """FAIL_HEALTH=true -> 503, which is what triggers the ECS circuit breaker."""
    monkeypatch.setenv("FAIL_HEALTH", "true")

    response = client.get("/health")

    assert response.status_code == 503
    assert response.json() == {"status": "unhealthy"}


def test_home_returns_500_when_simulate_errors_is_true(monkeypatch):
    """SIMULATE_ERRORS=true -> every page view is a 5xx, for the alarm demo."""
    monkeypatch.setenv("SIMULATE_ERRORS", "true")

    response = client.get("/")

    assert response.status_code == 500
    assert "Application Error" in response.text


def test_version_endpoint_reports_build_metadata(monkeypatch):
    """GET /version is the machine-readable proof of which image is live."""
    monkeypatch.setenv("GIT_SHA", "1a2b3c4")
    monkeypatch.setenv("APP_ENV", "prod")

    response = client.get("/version")

    assert response.status_code == 200
    assert response.json() == {
        "version": "1a2b3c4",
        "env": "prod",
        "color": APP_COLOR,
        "message": BANNER_MESSAGE,
    }


# ---------------------------------------------------------------------------
# Structured logging and version traceability
# ---------------------------------------------------------------------------
# These matter operationally, not just cosmetically: per-version log filtering
# in CloudWatch Logs Insights depends on every line being valid JSON carrying a
# `version` field. If that contract breaks, the Version logs workflow silently
# returns nothing, so it is worth a test.


def test_json_formatter_emits_one_line_of_json_with_the_version(monkeypatch):
    """Every log record must be a single valid JSON object tagged with the build."""
    monkeypatch.setenv("GIT_SHA", "abc1234")
    monkeypatch.setenv("APP_ENV", "prod")

    record = logging.LogRecord(
        name="app", level=logging.INFO, pathname=__file__, lineno=1,
        msg="request", args=None, exc_info=None,
    )
    record.extra_fields = {"path": "/health", "status": 200, "duration_ms": 1.5}

    line = JsonFormatter().format(record)

    # One record per line - CloudWatch treats a newline as a record boundary.
    assert "\n" not in line

    payload = json.loads(line)
    assert payload["version"] == "abc1234"
    assert payload["env"] == "prod"
    assert payload["level"] == "INFO"
    assert payload["message"] == "request"
    assert payload["path"] == "/health"
    assert payload["status"] == 200


def test_json_formatter_never_raises_on_odd_values():
    """A log call must not be able to take the application down."""
    record = logging.LogRecord(
        name="app", level=logging.INFO, pathname=__file__, lineno=1,
        msg="odd", args=None, exc_info=None,
    )
    record.extra_fields = {"obj": object()}          # not JSON-serialisable

    payload = json.loads(JsonFormatter().format(record))
    assert "obj" in payload


def test_responses_carry_the_version_header(monkeypatch):
    """X-App-Version lets you confirm the live build with curl, no console needed."""
    monkeypatch.setenv("GIT_SHA", "abc1234")

    response = client.get("/health")

    assert response.headers["x-app-version"] == "abc1234"
    assert response.headers["x-request-id"]


def test_an_upstream_request_id_is_preserved():
    """If a proxy already set a request id, keep it so traces join up."""
    response = client.get("/health", headers={"X-Request-Id": "trace-me"})

    assert response.headers["x-request-id"] == "trace-me"
