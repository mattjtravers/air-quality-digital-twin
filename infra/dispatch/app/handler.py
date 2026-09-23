"""Turn one EventBridge schedule firing into one GitHub ``workflow_dispatch`` call.

This function is deliberately outside the ``aqdt`` package: ``CodeUri`` bundles what it points
at, and the pipeline package carries GeoPandas, Pandera and PyArrow, none of which a dispatcher
needs. It has no dependencies beyond the Lambda runtime.

@spec INFRA-DISP-003, INFRA-DISP-004, INFRA-DISP-005, INFRA-DISP-006, INFRA-DISP-007
@spec INFRA-DISP-008, INFRA-DISP-009, INFRA-DISP-015, INFRA-DISP-016, INFRA-DISP-017
"""

from __future__ import annotations

import json
import logging
import os
import urllib.error
import urllib.request

import boto3

log = logging.getLogger()
log.setLevel(logging.INFO)

GITHUB_API = "https://api.github.com"
TIMEOUT_SECONDS = 8

#: The only workflows a schedule may name. The value becomes a path segment in the URL below, so
#: it is checked against this list rather than forwarded. Kept equal to the workflows the
#: schedules target by a test, so renaming one cannot leave this behind.
ALLOWED_WORKFLOWS = frozenset(
    {
        "ingest-purpleair.yaml",
        "ingest-airnow.yaml",
        "calibrate-fit.yaml",
        "calibrate-apply.yaml",
    }
)


class DispatchError(RuntimeError):
    """A dispatch did not happen. The message says which of several causes it could be."""


def _read_token(secret_name: str) -> str:
    response = boto3.client("secretsmanager").get_secret_value(SecretId=secret_name)
    return response["SecretString"]


def _explain(status: int, workflow: str, ref: str, headers, secret_name: str) -> str:
    where = f"dispatching {workflow} at ref {ref}"
    if status == 401:
        return (
            f"{status} {where}: the token is invalid or expired; replace the value of the "
            f"secret {secret_name}"
        )
    if status == 403:
        remaining = (headers or {}).get("x-ratelimit-remaining")
        if remaining is not None and str(remaining) == "0":
            return f"{status} {where}: GitHub API rate limit exhausted"
        return f"{status} {where}: the token lacks the Actions:write permission"
    if status == 404:
        # GitHub answers 404 for four different faults; saying so is the difference between a
        # five-minute fix and an afternoon.
        return (
            f"{status} {where}: any of — the workflow file is absent, the workflow declares no "
            f"workflow_dispatch trigger, the ref does not exist, or the token cannot see the "
            f"repository"
        )
    return f"{status} {where}"


def lambda_handler(event, context):
    workflow = (event or {}).get("workflow")
    if workflow not in ALLOWED_WORKFLOWS:
        raise DispatchError(
            f"refusing to dispatch {workflow!r}: expected one of {sorted(ALLOWED_WORKFLOWS)}"
        )

    owner = os.environ["GITHUB_OWNER"]
    repo = os.environ["GITHUB_REPO"]
    ref = os.environ.get("GITHUB_REF", "main")
    secret_name = os.environ["GITHUB_TOKEN_SECRET_NAME"]
    token = _read_token(secret_name)

    # Body carries ref only: a dispatched run resolves its own routine window, exactly as a
    # hand-run command does, so the trigger holds no window logic.
    request = urllib.request.Request(
        url=f"{GITHUB_API}/repos/{owner}/{repo}/actions/workflows/{workflow}/dispatches",
        data=json.dumps({"ref": ref}).encode(),
        method="POST",
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "Content-Type": "application/json",
            "User-Agent": "aqdt-dispatch",
        },
    )

    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS) as response:
            status = response.status
    except urllib.error.HTTPError as error:
        raise DispatchError(
            _explain(error.code, workflow, ref, getattr(error, "headers", None), secret_name)
        ) from error
    except urllib.error.URLError as error:
        raise DispatchError(
            f"could not reach GitHub dispatching {workflow}: {error.reason}"
        ) from error

    if status != 204:
        raise DispatchError(_explain(status, workflow, ref, None, secret_name))

    log.info("dispatched %s at ref %s", workflow, ref)
    return {"workflow": workflow, "ref": ref}
