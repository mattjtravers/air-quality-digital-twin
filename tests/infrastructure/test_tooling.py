"""Deployment config, the maintenance switch, and the suite's own limits — INFRA-OPS, INFRA-TEST."""

from __future__ import annotations

import os
import shutil
import subprocess
import tomllib
from pathlib import Path

import pytest

from tests.infrastructure.conftest import (
    ARCHIVE_BUCKET,
    DISPATCH_TEMPLATE,
    FOUNDATION_TEMPLATE,
    ROOT,
    SAMCONFIG,
    SCHEDULES,
    WORKFLOWS,
    load_template,
    settles_to,
    the_resource,
)

REGION = "us-east-1"
SCHEDULES_SCRIPT = ROOT / "bin" / "schedules.sh"


def samconfig() -> dict:
    if not SAMCONFIG.exists():
        pytest.fail("samconfig.toml not found at the repository root")
    return tomllib.loads(SAMCONFIG.read_text())


# @spec INFRA-OPS-001
def test_sam_cli_is_a_dev_dependency():
    project = tomllib.loads((ROOT / "pyproject.toml").read_text())
    dev = project["dependency-groups"]["dev"]
    assert any(entry.startswith("aws-sam-cli") for entry in dev), dev


# @spec INFRA-OPS-002
def test_one_config_environment_per_stack():
    config = samconfig()
    assert {"foundation", "dispatch"} <= set(config)
    for name in ("foundation", "dispatch"):
        assert config[name]["deploy"]["parameters"]["stack_name"]


# @spec INFRA-OPS-003
@pytest.mark.parametrize("environment", ["foundation", "dispatch"])
def test_region_is_pinned_and_iam_is_acknowledged(environment):
    parameters = samconfig()[environment]["deploy"]["parameters"]
    assert parameters["region"] == REGION
    assert "CAPABILITY_IAM" in parameters["capabilities"]


# @spec INFRA-OPS-004
def test_samconfig_holds_no_credential():
    text = SAMCONFIG.read_text() if SAMCONFIG.exists() else pytest.fail("samconfig.toml missing")
    lowered = text.lower()
    for forbidden in (
        "aws_secret_access_key",
        "aws_access_key_id",
        "aws_session_token",
        "ghp_",
        "gho_",
        "ghs_",
        "github_pat_",
    ):
        assert forbidden not in lowered, forbidden


# @spec INFRA-OPS-005
def test_sam_build_output_is_ignored():
    ignored = (ROOT / ".gitignore").read_text().splitlines()
    assert any(line.strip().rstrip("/") == ".aws-sam" for line in ignored)


# @spec INFRA-OPS-006
def test_schedules_script_covers_every_schedule_and_all_three_verbs():
    if not SCHEDULES_SCRIPT.exists():
        pytest.fail("bin/schedules.sh not found")
    assert os.access(SCHEDULES_SCRIPT, os.X_OK), "bin/schedules.sh must be executable"

    text = SCHEDULES_SCRIPT.read_text()
    for verb in ("on", "off", "status"):
        assert verb in text, verb
    for name in SCHEDULES:
        assert name in text, name
    assert "update-schedule" in text


# @spec INFRA-DISP-002
def test_the_dispatch_function_ships_nothing_but_its_own_source():
    app = ROOT / "infra" / "dispatch" / "app"
    if not app.exists():
        pytest.fail("infra/dispatch/app not found")
    for manifest in ("requirements.txt", "pyproject.toml", "Pipfile", "poetry.lock", "uv.lock"):
        assert not (app / manifest).exists(), (
            f"{manifest} would make sam build install dependencies into the package"
        )
    assert [p.name for p in app.glob("*.py")] == ["handler.py"]


# @spec INFRA-TEST-005
def test_allow_list_matches_the_workflows_the_schedules_target(handler):
    targeted = {workflow for _, workflow in SCHEDULES.values()}
    assert set(handler.ALLOWED_WORKFLOWS) == targeted
    for workflow in targeted:
        assert (WORKFLOWS / workflow).exists(), workflow


# @spec INFRA-TEST-006
def test_template_and_code_agree_on_the_archive_bucket():
    from aqdt.observation_store import ARCHIVE_BUCKET as constant

    template = load_template(FOUNDATION_TEMPLATE)
    bucket = the_resource(template, "AWS::S3::Bucket")
    declared = settles_to(template, bucket["Properties"]["BucketName"])
    assert declared == constant == ARCHIVE_BUCKET


# @spec INFRA-TEST-003
@pytest.mark.parametrize("template", [FOUNDATION_TEMPLATE, DISPATCH_TEMPLATE])
def test_sam_validate_accepts_the_template(template: Path):
    sam = shutil.which("sam")
    if sam is None:
        pytest.skip("SAM CLI not installed")
    if not template.exists():
        pytest.fail(f"template not found: {template.relative_to(ROOT)}")

    result = subprocess.run(
        [sam, "validate", "--region", REGION, "--template", str(template), "--lint"],
        capture_output=True,
        text=True,
        cwd=ROOT,
    )
    assert result.returncode == 0, result.stdout + result.stderr


# @spec INFRA-TEST-002
def test_infrastructure_tests_reach_no_network_and_need_no_credentials():
    here = Path(__file__).parent
    source = "\n".join(
        path.read_text() for path in here.glob("*.py") if path.name != Path(__file__).name
    )
    assert "import boto3" not in source, "the suite must not build real AWS clients"
    assert "import httpx" not in source and "requests." not in source

    for line in source.splitlines():
        if "urlopen(" not in line:
            continue
        assert any(marker in line for marker in ("monkeypatch", "fake", "def ")), (
            f"urlopen may only be stubbed, never called: {line.strip()}"
        )
