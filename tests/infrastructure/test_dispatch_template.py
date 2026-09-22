"""The dispatch stack — INFRA-SCHED, INFRA-DISP, INFRA-OPS-007."""

from __future__ import annotations

import json

import pytest

from tests.infrastructure.conftest import (
    DISPATCH_TEMPLATE,
    ROOT,
    SCHEDULES,
    as_list,
    resources_of_type,
    settles_to,
    statements,
    the_resource,
)

SCHEDULE = "AWS::Scheduler::Schedule"
FUNCTION = "AWS::Serverless::Function"
ROLE = "AWS::IAM::Role"
LOG_GROUP = "AWS::Logs::LogGroup"
ALARM = "AWS::CloudWatch::Alarm"


def schedules_by_name(dispatch: dict) -> dict[str, dict]:
    return {
        body["Properties"].get("Name", logical): body
        for logical, body in resources_of_type(dispatch, SCHEDULE).items()
    }


@pytest.fixture(params=sorted(SCHEDULES))
def schedule(request, dispatch):
    found = schedules_by_name(dispatch)
    assert request.param in found, f"no schedule named {request.param}: {sorted(found)}"
    return request.param, found[request.param]["Properties"]


# @spec INFRA-SCHED-001
def test_exactly_four_schedules(dispatch):
    assert set(schedules_by_name(dispatch)) == set(SCHEDULES)


# @spec INFRA-SCHED-002
def test_cron_expressions_in_utc(schedule):
    name, properties = schedule
    expression, _ = SCHEDULES[name]
    assert properties["ScheduleExpression"] == expression
    assert properties.get("ScheduleExpressionTimezone") == "UTC"


# @spec INFRA-SCHED-003
def test_no_jitter_window(schedule):
    name, properties = schedule
    assert properties["FlexibleTimeWindow"]["Mode"] == "OFF", name


# @spec INFRA-SCHED-004
def test_schedules_are_created_disabled(schedule):
    name, properties = schedule
    assert properties["State"] == "DISABLED", name


# @spec INFRA-SCHED-005
def test_retries_are_bounded_well_below_the_service_default(schedule):
    name, properties = schedule
    retry = properties["Target"]["RetryPolicy"]
    assert retry["MaximumRetryAttempts"] == 2, name
    assert retry["MaximumEventAgeInSeconds"] == 300, name


# @spec INFRA-SCHED-006
def test_input_names_the_workflow_to_dispatch(schedule):
    name, properties = schedule
    _, workflow = SCHEDULES[name]
    payload = json.loads(properties["Target"]["Input"])
    assert payload == {"workflow": workflow}, name


# @spec INFRA-DISP-014
def test_no_schedule_carries_a_ref(schedule):
    name, properties = schedule
    assert "ref" not in properties["Target"]["Input"], name


# @spec INFRA-SCHED-007
def test_every_schedule_invokes_through_a_scheduler_role(dispatch, schedule):
    name, properties = schedule
    assert properties["Target"]["RoleArn"], name

    role = _scheduler_role(dispatch)
    (policy,) = role["Properties"]["Policies"]
    granted = statements(policy["PolicyDocument"])
    actions = {action for s in granted for action in as_list(s["Action"])}
    assert actions == {"lambda:InvokeFunction"}


def _scheduler_role(dispatch: dict) -> dict:
    roles = [
        body
        for body in resources_of_type(dispatch, ROLE).values()
        if "scheduler.amazonaws.com" in str(body["Properties"]["AssumeRolePolicyDocument"])
    ]
    assert len(roles) == 1, "expected exactly one role trusted by EventBridge Scheduler"
    return roles[0]


# @spec INFRA-DISP-001
def test_dispatch_function_runtime_and_source(dispatch):
    properties = the_resource(dispatch, FUNCTION)["Properties"]
    assert properties["Runtime"] == "python3.13"
    assert properties["Handler"] == "handler.lambda_handler"

    # SAM resolves CodeUri relative to the template, so resolve it the same way before comparing.
    resolved = (DISPATCH_TEMPLATE.parent / str(properties["CodeUri"])).resolve()
    assert resolved == (ROOT / "infra" / "dispatch" / "app").resolve()


# @spec INFRA-DISP-011
def test_dispatch_function_timeout_and_memory(dispatch):
    properties = the_resource(dispatch, FUNCTION)["Properties"]
    assert properties["Timeout"] == 10
    assert properties["MemorySize"] == 128


# @spec INFRA-DISP-013, INFRA-DISP-014
def test_dispatch_function_environment(dispatch):
    properties = the_resource(dispatch, FUNCTION)["Properties"]
    variables = properties["Environment"]["Variables"]
    assert set(variables) == {
        "GITHUB_OWNER",
        "GITHUB_REPO",
        "GITHUB_REF",
        "GITHUB_TOKEN_SECRET_NAME",
    }
    assert settles_to(dispatch, variables["GITHUB_REF"]) == "main"



# @spec INFRA-DISP-010
def test_secret_grant_is_one_secret_and_tolerates_its_suffix(dispatch):
    properties = the_resource(dispatch, FUNCTION)["Properties"]
    granted = [
        statement
        for policy in properties["Policies"]
        for statement in statements(_policy_document(policy))
    ]
    reading = [s for s in granted if "secretsmanager:GetSecretValue" in as_list(s["Action"])]
    assert len(reading) == 1

    resources = as_list(reading[0]["Resource"])
    assert len(resources) == 1
    assert str(resources[0]).endswith("-??????"), (
        "a Secrets Manager ARN carries a six-character suffix assigned at creation"
    )

    actions = {action for s in granted for action in as_list(s["Action"])}
    assert not {a for a in actions if a.startswith("s3:")}, "the dispatcher never touches S3"


def _policy_document(policy) -> dict:
    """SAM policies are either a document or a named policy template."""
    if isinstance(policy, dict) and "Statement" in policy:
        return policy
    if isinstance(policy, dict) and len(policy) == 1:
        (body,) = policy.values()
        return body if isinstance(body, dict) and "Statement" in body else {"Statement": []}
    return {"Statement": []}


# @spec INFRA-DISP-012
def test_log_group_is_declared_with_retention(dispatch):
    group = the_resource(dispatch, LOG_GROUP)
    assert group["Properties"]["RetentionInDays"] == 30


# @spec INFRA-OPS-007
def test_errors_alarm(dispatch):
    properties = the_resource(dispatch, ALARM)["Properties"]
    assert properties["MetricName"] == "Errors"
    assert properties["Namespace"] == "AWS/Lambda"
    assert properties["Statistic"] == "Sum"
    assert properties["Period"] == 900
    assert properties["Threshold"] == 1
    assert properties["EvaluationPeriods"] == 1
    assert properties["ComparisonOperator"] == "GreaterThanOrEqualToThreshold"
    assert properties["TreatMissingData"] == "notBreaching"
