"""The GitHub Actions schedules — PIPE-SCHED, PIPE-CFG-001, PIPE-CFG-003."""

import json
import re
import tomllib
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
WORKFLOWS = ROOT / ".github" / "workflows"

EXPECTED = {
    "ingest-purpleair.yaml": {
        "command": "aqdt ingest purpleair",
        "group": "ingest-purpleair",
        "timeout": 10,
        "inputs": set(),
        "secrets": {"PURPLEAIR_API_KEY"},
    },
    "ingest-airnow.yaml": {
        "command": "aqdt ingest airnow",
        "group": "ingest-airnow",
        "timeout": 20,
        "inputs": {"start", "end"},
        "secrets": {"AIRNOW_API_KEY"},
    },
    "calibrate-fit.yaml": {
        "command": "aqdt calibrate fit",
        "group": "calibrate-fit",
        "timeout": 30,
        "inputs": {"as_of"},
        "secrets": set(),
    },
    "calibrate-apply.yaml": {
        "command": "aqdt calibrate apply",
        "group": "calibrate-apply",
        "timeout": 20,
        "inputs": {"start", "end"},
        "secrets": set(),
    },
    "fuse.yaml": {
        "command": "aqdt fuse",
        "group": "fuse",
        "timeout": 20,
        "inputs": {"start", "end"},
        "secrets": set(),
    },
}
DASHBOARD = "dashboard.yaml"
AWS_SECRETS = {"AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY"}
VARIABLES = {"AQDT_ARCHIVE_URI", "AWS_DEFAULT_REGION"}


def load(name: str) -> dict:
    return yaml.safe_load((WORKFLOWS / name).read_text())


def triggers(workflow: dict) -> dict:
    return workflow.get("on") or workflow.get(True)  # PyYAML reads a bare `on` key as True


def the_job(workflow: dict) -> dict:
    (job,) = workflow["jobs"].values()
    return job


def run_steps(job: dict) -> list[str]:
    return [step["run"] for step in job["steps"] if "run" in step]


def env_of(workflow: dict) -> dict:
    """Every env mapping in the file, merged: workflow, job, and step level."""
    merged = dict(workflow.get("env") or {})
    job = the_job(workflow)
    merged.update(job.get("env") or {})
    for step in job["steps"]:
        merged.update(step.get("env") or {})
    return merged


@pytest.fixture(params=sorted(EXPECTED))
def workflow(request):
    return request.param, load(request.param), EXPECTED[request.param]


def installs_the_project(job: dict) -> bool:
    return any("uv sync" in step.get("run", "") for step in job["steps"])


# @spec PIPE-SCHED-001
@pytest.mark.parametrize("name", [*sorted(EXPECTED), DASHBOARD])
def test_workflow_dispatch_is_the_only_trigger(name):
    wf = load(name)
    on = triggers(wf)
    assert "workflow_dispatch" in on, name
    assert set(on) == {"workflow_dispatch"}, (
        f"{name}: every run arrives by dispatch; a schedule trigger would be a second, "
        f"unreliable path (found {sorted(on)})"
    )


# @spec PIPE-SCHED-003
def test_one_aqdt_command_after_a_no_dev_sync(workflow):
    name, wf, expected = workflow
    steps = run_steps(the_job(wf))
    assert any("uv sync --no-dev" in s for s in steps), name
    aqdt_steps = [s for s in steps if "aqdt " in s]
    assert len(aqdt_steps) == 1, name
    assert f"uv run {expected['command']}" in aqdt_steps[0], name
    assert aqdt_steps[0].count("aqdt ") == 1, name


# @spec PIPE-SCHED-004
def test_concurrency_groups_never_cancel_in_progress(workflow):
    name, wf, expected = workflow
    concurrency = wf.get("concurrency") or the_job(wf).get("concurrency")
    assert concurrency["group"] == expected["group"], name
    assert concurrency["cancel-in-progress"] is False, name


# @spec PIPE-SCHED-005
def test_job_timeouts(workflow):
    name, wf, expected = workflow
    assert the_job(wf)["timeout-minutes"] == expected["timeout"], name


# @spec PIPE-SCHED-006
def test_dispatch_inputs_map_to_command_options(workflow):
    name, wf, expected = workflow
    dispatch = triggers(wf)["workflow_dispatch"] or {}
    inputs = set((dispatch.get("inputs") or {}).keys())
    assert inputs == expected["inputs"], name
    (command,) = [s for s in run_steps(the_job(wf)) if "aqdt " in s]
    for input_name in expected["inputs"]:
        option = "--" + input_name.replace("_", "-")
        assert f"inputs.{input_name}" in command and option in command, (name, input_name)
    for input_name in expected["inputs"]:
        assert (dispatch["inputs"][input_name].get("required", False)) is False, name


# @spec PIPE-SCHED-007
def test_no_workflow_sets_database_url(workflow):
    name, _, _ = workflow
    assert "DATABASE_URL" not in (WORKFLOWS / name).read_text(), name


# @spec PIPE-SCHED-008
@pytest.mark.parametrize("name", [*sorted(EXPECTED), DASHBOARD])
def test_no_workflow_writes_to_the_repository(name):
    wf = load(name)
    text = (WORKFLOWS / name).read_text()
    assert "git push" not in text and "git commit" not in text, name
    for job in wf["jobs"].values():
        for step in job["steps"]:
            assert "contents: write" not in str(step)
            if name != DASHBOARD:
                assert "artifact" not in step.get("uses", ""), (name, step)
        assert "contents: write" not in str(job.get("permissions", ""))
    assert "contents: write" not in str(wf.get("permissions", ""))


# @spec PIPE-SCHED-007, PIPE-SCHED-009
def test_dashboard_workflow_sets_no_database_and_gates_no_job():
    text = (WORKFLOWS / DASHBOARD).read_text()
    assert "DATABASE_URL" not in text and "AQDT_SCHEDULES_ENABLED" not in text
    for name, job in load(DASHBOARD)["jobs"].items():
        assert "if" not in job, name


def dashboard_jobs() -> tuple[dict, dict]:
    jobs = load(DASHBOARD)["jobs"]
    assert set(jobs) == {"build", "deploy"}
    return jobs["build"], jobs["deploy"]


def uses(job: dict) -> list[str]:
    return [step["uses"].split("@")[0] for step in job["steps"] if "uses" in step]


# @spec PIPE-SCHED-013
def test_dashboard_workflow_builds_once_and_never_interrupts_a_deployment():
    wf = load(DASHBOARD)
    assert wf["concurrency"] == {"group": "dashboard", "cancel-in-progress": False}
    build, _ = dashboard_jobs()
    assert build["timeout-minutes"] == 15
    steps = run_steps(build)
    assert any("uv sync --no-dev --group analysis" in s for s in steps)
    aqdt_steps = [s for s in steps if "aqdt " in s]
    assert len(aqdt_steps) == 1 and aqdt_steps[0].count("aqdt ") == 1
    assert "uv run aqdt dashboard build --out site" in aqdt_steps[0]


# @spec PIPE-SCHED-014
def test_dashboard_workflow_publishes_the_site_to_pages():
    build, deploy = dashboard_jobs()
    used = uses(build)
    assert used.index("actions/configure-pages") < used.index("actions/upload-pages-artifact")
    (upload,) = [s for s in build["steps"] if "upload-pages-artifact" in s.get("uses", "")]
    assert upload["with"] == {"path": "site"}
    assert deploy["needs"] == "build" or deploy["needs"] == ["build"]
    assert deploy["timeout-minutes"] == 10
    assert deploy["environment"]["name"] == "github-pages"
    (step,) = [s for s in deploy["steps"] if "deploy-pages" in s.get("uses", "")]
    assert deploy["environment"]["url"] == "${{ steps.%s.outputs.page_url }}" % step["id"]
    assert uses(deploy) == ["actions/deploy-pages"]


# @spec PIPE-SCHED-015
def test_dashboard_workflow_permissions():
    wf = load(DASHBOARD)
    assert wf["permissions"] == {"contents": "read", "pages": "write", "id-token": "write"}
    for name, job in wf["jobs"].items():
        assert "permissions" not in job, name


# @spec PIPE-CFG-001
def test_dashboard_workflow_exposes_the_archive_and_no_api_key():
    build, deploy = dashboard_jobs()
    env: dict = {}
    for step in build["steps"]:
        env.update(step.get("env") or {})
    assert set(env) == AWS_SECRETS | VARIABLES
    for secret in AWS_SECRETS:
        assert env[secret] == "${{ secrets.%s }}" % secret
    for variable in VARIABLES:
        assert env[variable] == "${{ vars.%s }}" % variable
    text = (WORKFLOWS / DASHBOARD).read_text()
    assert "PURPLEAIR_API_KEY" not in text and "AIRNOW_API_KEY" not in text
    assert "AQDT_BBOX" not in text
    assert not any(step.get("env") for step in deploy["steps"])


# @spec PIPE-SCHED-009
def test_no_workflow_gates_its_job(workflow):
    name, wf, _ = workflow
    assert "if" not in the_job(wf), (
        f"{name}: scheduled runs are turned off by disabling the EventBridge schedules, so a "
        f"workflow that receives a dispatch always runs it"
    )
    assert "AQDT_SCHEDULES_ENABLED" not in (WORKFLOWS / name).read_text(), name


# @spec PIPE-SCHED-010
def test_every_workflow_names_its_runner_image():
    for path in sorted(WORKFLOWS.glob("*.yaml")):
        for job in yaml.safe_load(path.read_text())["jobs"].values():
            assert job["runs-on"] == "ubuntu-24.04", path.name


# @spec PIPE-SCHED-011
def test_every_workflow_reads_python_version_file():
    assert re.fullmatch(r"3\.\d+\n?", (ROOT / ".python-version").read_text())
    for path in sorted(WORKFLOWS.glob("*.yaml")):
        for job in yaml.safe_load(path.read_text())["jobs"].values():
            if not installs_the_project(job):
                continue
            setups = [
                s for s in job["steps"] if s.get("uses", "").startswith("actions/setup-python@")
            ]
            assert len(setups) == 1, path.name
            assert setups[0]["with"] == {"python-version-file": ".python-version"}, path.name


# @spec PIPE-SCHED-012
def test_uv_release_is_pinned_once_in_pyproject():
    pin = tomllib.loads((ROOT / "pyproject.toml").read_text())["tool"]["uv"]["required-version"]
    release = re.fullmatch(r"==(\d+\.\d+\.\d+)", pin).group(1)
    for path in sorted(WORKFLOWS.glob("*.yaml")):
        for job in yaml.safe_load(path.read_text())["jobs"].values():
            if not installs_the_project(job):
                continue
            setups = [
                s for s in job["steps"] if s.get("uses", "").startswith("astral-sh/setup-uv@")
            ]
            assert len(setups) == 1, path.name
            assert "version" not in setups[0].get("with", {}), path.name
    devcontainer = json.loads((ROOT / ".devcontainer" / "devcontainer.json").read_text())
    feature = devcontainer["features"]["ghcr.io/jsburckhardt/devcontainer-features/uv:1"]
    assert feature == {"version": release}


# @spec PIPE-CFG-001
def test_each_workflow_exposes_only_what_its_command_needs(workflow):
    name, wf, expected = workflow
    env = env_of(wf)
    for secret in expected["secrets"] | AWS_SECRETS:
        assert env.get(secret) == "${{ secrets.%s }}" % secret, (name, secret)
    for variable in VARIABLES:
        assert env.get(variable) == "${{ vars.%s }}" % variable, (name, variable)
    for other in {"PURPLEAIR_API_KEY", "AIRNOW_API_KEY"} - expected["secrets"]:
        assert other not in env, (name, other)
    assert "AQDT_BBOX" not in env, name
    assert set(env) == expected["secrets"] | AWS_SECRETS | VARIABLES, name
    text = (WORKFLOWS / name).read_text()
    assert not re.search(r"secrets\.(?!%s)" % "|".join(expected["secrets"] | AWS_SECRETS), text)


# @spec PIPE-CFG-003
def test_pipeline_tests_need_no_network_or_database():
    here = Path(__file__).parent
    others = [p for p in here.glob("*.py") if p.name != Path(__file__).name]
    source = "\n".join(p.read_text() for p in others)
    assert "import httpx" not in source and "import psycopg" not in source
    assert "requests." not in source
