"""The dispatch function's behaviour — INFRA-DISP-003 through -009, -015, -016; INFRA-TEST-004.

Nothing here reaches AWS or GitHub: the Secrets Manager client and ``urlopen`` are both replaced.
"""

from __future__ import annotations

import io
import json
import urllib.error
import urllib.request

import pytest

from tests.infrastructure.conftest import GITHUB_OWNER, GITHUB_REPO

TOKEN = "ghp-not-a-real-token"
SECRET_NAME = "aqdt/github-dispatch-token"
WORKFLOW = "ingest-purpleair.yaml"


@pytest.fixture(autouse=True)
def environment(monkeypatch):
    monkeypatch.setenv("GITHUB_OWNER", GITHUB_OWNER)
    monkeypatch.setenv("GITHUB_REPO", GITHUB_REPO)
    monkeypatch.setenv("GITHUB_REF", "main")
    monkeypatch.setenv("GITHUB_TOKEN_SECRET_NAME", SECRET_NAME)


class FakeSecrets:
    def __init__(self):
        self.requested: list[str] = []

    def get_secret_value(self, SecretId):  # noqa: N803 — boto3's parameter name
        self.requested.append(SecretId)
        return {"SecretString": TOKEN}


# @spec INFRA-TEST-004
@pytest.fixture
def secrets(handler, monkeypatch):
    fake = FakeSecrets()

    class FakeBoto3:
        @staticmethod
        def client(service_name, *args, **kwargs):
            assert service_name == "secretsmanager"
            return fake

    monkeypatch.setattr(handler, "boto3", FakeBoto3, raising=False)
    return fake


class Response:
    def __init__(self, status=204, headers=None):
        self.status = status
        self.headers = headers or {}

    def read(self):
        return b""

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


@pytest.fixture
def calls(monkeypatch):
    """Record every request, and answer with whatever the test queued."""
    recorded: list[urllib.request.Request] = []
    answer = {"response": Response()}

    def fake_urlopen(request, *args, **kwargs):
        recorded.append(request)
        result = answer["response"]
        if isinstance(result, Exception):
            raise result
        return result

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    return recorded, answer


def http_error(status: int, headers: dict | None = None) -> urllib.error.HTTPError:
    return urllib.error.HTTPError(
        url="https://api.github.com/", code=status, msg="", hdrs=headers or {}, fp=io.BytesIO(b"")
    )


def invoke(handler, workflow=WORKFLOW):
    return handler.lambda_handler({"workflow": workflow}, None)


# @spec INFRA-DISP-003
def test_token_comes_from_the_named_secret(handler, secrets, calls):
    invoke(handler)
    assert secrets.requested == [SECRET_NAME]


# @spec INFRA-DISP-004
def test_posts_a_dispatch_for_the_workflow_its_input_names(handler, secrets, calls):
    recorded, _ = calls
    invoke(handler)

    (request,) = recorded
    assert request.full_url == (
        f"https://api.github.com/repos/{GITHUB_OWNER}/{GITHUB_REPO}"
        f"/actions/workflows/{WORKFLOW}/dispatches"
    )
    assert request.get_method() == "POST"
    headers = {key.lower(): value for key, value in request.header_items()}
    assert headers["authorization"] == f"Bearer {TOKEN}"


# @spec INFRA-DISP-015
def test_body_carries_ref_only(handler, secrets, calls):
    recorded, _ = calls
    invoke(handler)

    (request,) = recorded
    assert json.loads(request.data) == {"ref": "main"}


# @spec INFRA-DISP-006
def test_a_204_succeeds(handler, secrets, calls):
    _, answer = calls
    answer["response"] = Response(status=204)
    invoke(handler)


# @spec INFRA-DISP-005
@pytest.mark.parametrize(
    "event",
    [{}, {"workflow": ""}, {"workflow": "not-a-workflow.yaml"}, {"workflow": "../../etc/passwd"}],
)
def test_an_input_outside_the_allow_list_never_reaches_github(handler, secrets, calls, event):
    recorded, _ = calls
    with pytest.raises(Exception):
        handler.lambda_handler(event, None)
    assert recorded == [], "no request may be issued for an input that was not accepted"


# @spec INFRA-DISP-007
def test_an_unexpected_status_raises_without_leaking_the_token(handler, secrets, calls):
    _, answer = calls
    answer["response"] = http_error(500)

    with pytest.raises(Exception) as raised:
        invoke(handler)

    message = str(raised.value)
    assert "500" in message
    assert WORKFLOW in message
    assert "main" in message
    assert TOKEN not in message


# @spec INFRA-DISP-008
def test_a_404_names_every_cause_it_could_be(handler, secrets, calls):
    _, answer = calls
    answer["response"] = http_error(404)

    with pytest.raises(Exception) as raised:
        invoke(handler)

    message = str(raised.value).lower()
    assert "workflow" in message
    assert "workflow_dispatch" in message
    assert "ref" in message
    assert "token" in message or "access" in message


# @spec INFRA-DISP-017
def test_a_401_names_the_secret_to_replace(handler, secrets, calls):
    _, answer = calls
    answer["response"] = http_error(401)

    with pytest.raises(Exception) as raised:
        invoke(handler)

    message = str(raised.value)
    assert SECRET_NAME in message
    assert "expired" in message
    assert TOKEN not in message


# @spec INFRA-DISP-009
def test_a_403_tells_a_permission_failure_from_a_rate_limit(handler, secrets, calls):
    _, answer = calls

    answer["response"] = http_error(403, {"x-ratelimit-remaining": "0"})
    with pytest.raises(Exception) as rate_limited:
        invoke(handler)

    answer["response"] = http_error(403, {"x-ratelimit-remaining": "4999"})
    with pytest.raises(Exception) as forbidden:
        invoke(handler)

    assert str(rate_limited.value) != str(forbidden.value)
    assert "rate" in str(rate_limited.value).lower()


# @spec INFRA-DISP-007
def test_a_transport_failure_raises(handler, secrets, calls):
    _, answer = calls
    answer["response"] = urllib.error.URLError("connection reset")
    with pytest.raises(Exception):
        invoke(handler)


# @spec INFRA-DISP-016
def test_a_repeated_dispatch_is_sent_again_rather_than_suppressed(handler, secrets, calls):
    recorded, _ = calls
    invoke(handler)
    invoke(handler)
    assert len(recorded) == 2, "dispatch is at-least-once; nothing deduplicates it"
