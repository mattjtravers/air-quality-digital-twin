"""The foundation stack — INFRA-FOUND."""

from __future__ import annotations

from tests.infrastructure.conftest import (
    ARCHIVE_BUCKET,
    ARCHIVE_PREFIX,
    GITHUB_OWNER,
    GITHUB_REPO,
    as_list,
    resources_of_type,
    settles_to,
    statements,
    the_resource,
)

BUCKET = "AWS::S3::Bucket"
ROLE = "AWS::IAM::Role"
OIDC_PROVIDER = "AWS::IAM::OIDCProvider"


# @spec INFRA-FOUND-001
def test_archive_bucket_is_named_explicitly(foundation):
    bucket = the_resource(foundation, BUCKET)
    assert settles_to(foundation, bucket["Properties"]["BucketName"]) == ARCHIVE_BUCKET


# @spec INFRA-FOUND-002
def test_archive_bucket_is_retained_on_delete_and_replace(foundation):
    bucket = the_resource(foundation, BUCKET)
    assert bucket.get("DeletionPolicy") == "Retain"
    assert bucket.get("UpdateReplacePolicy") == "Retain"


# @spec INFRA-FOUND-003
def test_archive_bucket_is_private_and_owner_enforced(foundation):
    properties = the_resource(foundation, BUCKET)["Properties"]
    block = properties["PublicAccessBlockConfiguration"]
    assert block["BlockPublicAcls"] is True
    assert block["BlockPublicPolicy"] is True
    assert block["IgnorePublicAcls"] is True
    assert block["RestrictPublicBuckets"] is True
    (ownership,) = properties["OwnershipControls"]["Rules"]
    assert ownership["ObjectOwnership"] == "BucketOwnerEnforced"


# @spec INFRA-FOUND-004
def test_archive_bucket_is_versioned(foundation):
    properties = the_resource(foundation, BUCKET)["Properties"]
    assert properties["VersioningConfiguration"]["Status"] == "Enabled"


# @spec INFRA-FOUND-005
def test_lifecycle_bounds_what_versioning_accumulates(foundation):
    properties = the_resource(foundation, BUCKET)["Properties"]
    rules = [rule for rule in properties["LifecycleConfiguration"]["Rules"] if _enabled(rule)]

    noncurrent = [r for r in rules if "NoncurrentVersionExpiration" in r]
    assert len(noncurrent) == 1
    assert noncurrent[0]["NoncurrentVersionExpiration"]["NoncurrentDays"] == 30

    markers = [r for r in rules if (r.get("ExpirationInDays") is None) and _expires_markers(r)]
    assert markers, "no rule removes expired object delete markers"

    aborts = [r for r in rules if "AbortIncompleteMultipartUpload" in r]
    assert len(aborts) == 1
    assert aborts[0]["AbortIncompleteMultipartUpload"]["DaysAfterInitiation"] == 7


def _enabled(rule: dict) -> bool:
    return rule.get("Status", "Enabled") == "Enabled"


def _expires_markers(rule: dict) -> bool:
    expiration = rule.get("ExpiredObjectDeleteMarker")
    if expiration is not None:
        return bool(expiration)
    return bool((rule.get("Expiration") or {}).get("ExpiredObjectDeleteMarker"))


# @spec INFRA-FOUND-006
def test_runner_role_trusts_only_this_repository(foundation):
    role = _runner_role(foundation)
    (statement,) = statements(role["Properties"]["AssumeRolePolicyDocument"])
    assert statement["Effect"] == "Allow"
    assert "sts:AssumeRoleWithWebIdentity" in as_list(statement["Action"])

    conditions = statement["Condition"]
    subject = _condition_value(conditions, "token.actions.githubusercontent.com:sub")
    assert [settles_to(foundation, value) for value in subject] == [
        f"repo:{GITHUB_OWNER}/{GITHUB_REPO}:*"
    ]
    audience = _condition_value(conditions, "token.actions.githubusercontent.com:aud")
    assert audience == ["sts.amazonaws.com"]


def _condition_value(conditions: dict, key: str) -> list[str]:
    for operator in conditions.values():
        if key in operator:
            return as_list(operator[key])
    raise AssertionError(f"no trust-policy condition on {key}: {conditions}")


def _runner_role(foundation: dict) -> dict:
    roles = [
        body
        for body in resources_of_type(foundation, ROLE).values()
        if "AssumeRoleWithWebIdentity" in str(body["Properties"]["AssumeRolePolicyDocument"])
    ]
    assert len(roles) == 1, "expected exactly one web-identity role in the foundation stack"
    return roles[0]


# @spec INFRA-FOUND-007
def test_runner_role_reaches_the_archive_prefix_and_nothing_else(foundation):
    role = _runner_role(foundation)
    (policy,) = role["Properties"]["Policies"]
    granted = statements(policy["PolicyDocument"])
    assert all(s["Effect"] == "Allow" for s in granted)

    actions = {action for s in granted for action in as_list(s["Action"])}
    assert actions == {"s3:GetObject", "s3:PutObject", "s3:DeleteObject", "s3:ListBucket"}

    resources = " ".join(
        str(settles_to(foundation, r)) for s in granted for r in as_list(s["Resource"])
    )
    assert f"{ARCHIVE_PREFIX}/*" in resources
    assert "*/*" not in resources.replace(f"{ARCHIVE_PREFIX}/*", "")

    listing = [s for s in granted if "s3:ListBucket" in as_list(s["Action"])]
    assert listing and all("Condition" in s for s in listing), (
        "s3:ListBucket must be conditioned on the archive prefix"
    )


# @spec INFRA-FOUND-007
def test_archive_prefix_is_a_parameter_not_a_literal(foundation):
    parameter = (foundation.get("Parameters") or {})["ArchivePrefix"]
    assert parameter["Default"] == ARCHIVE_PREFIX


# @spec INFRA-FOUND-008
def test_oidc_provider_creation_is_optional(foundation):
    parameters = foundation.get("Parameters") or {}
    assert parameters["CreateGitHubOidcProvider"]["AllowedValues"] == ["true", "false"]
    assert "GitHubOidcProviderArn" in parameters, (
        "an existing provider's ARN must be accepted when the stack does not create one"
    )

    providers = resources_of_type(foundation, OIDC_PROVIDER)
    assert len(providers) == 1
    (provider,) = providers.values()
    assert "CreateGitHubOidcProvider" in str(provider.get("Condition", "")), (
        "the provider must be created conditionally"
    )
    url = provider["Properties"]["Url"]
    assert "token.actions.githubusercontent.com" in str(url)


# @spec INFRA-FOUND-009
def test_oidc_provider_survives_stack_deletion(foundation):
    (provider,) = resources_of_type(foundation, OIDC_PROVIDER).values()
    assert provider.get("DeletionPolicy") == "Retain"


# @spec INFRA-FOUND-010
def test_bucket_and_prefix_are_exported(foundation):
    outputs = foundation.get("Outputs") or {}
    exported = " ".join(str(settles_to(foundation, body.get("Value"))) for body in outputs.values())
    assert ARCHIVE_BUCKET in exported or "ArchiveBucket" in exported
    assert any("prefix" in name.lower() for name in outputs), (
        f"no output carries the archive prefix: {sorted(outputs)}"
    )
