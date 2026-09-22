"""Shared loading for the infrastructure tests — INFRA-TEST-001, INFRA-TEST-002.

Templates are read as data. Nothing here calls AWS, opens a socket, or needs credentials: a
CloudFormation template is a document, and every property these tests assert is visible in it.
"""

from __future__ import annotations

import importlib.util
import re
from pathlib import Path
from types import ModuleType

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
INFRA = ROOT / "infra"
FOUNDATION_TEMPLATE = INFRA / "foundation" / "template.yaml"
DISPATCH_TEMPLATE = INFRA / "dispatch" / "template.yaml"
HANDLER = INFRA / "dispatch" / "app" / "handler.py"
SAMCONFIG = ROOT / "samconfig.toml"
WORKFLOWS = ROOT / ".github" / "workflows"

ARCHIVE_BUCKET = "air-quality-digital-twin-585949919812-us-east-1-archive"
ARCHIVE_PREFIX = "dc-metro"
GITHUB_OWNER = "mattjtravers"
GITHUB_REPO = "air-quality-digital-twin"

#: The four workflows the schedules dispatch, and the cadence each one fires on.
SCHEDULES = {
    "aqdt-ingest-purpleair": ("cron(0/15 * * * ? *)", "ingest-purpleair.yaml"),
    "aqdt-ingest-airnow": ("cron(20 * * * ? *)", "ingest-airnow.yaml"),
    "aqdt-calibrate-fit": ("cron(30 0 * * ? *)", "calibrate-fit.yaml"),
    "aqdt-calibrate-apply": ("cron(40 * * * ? *)", "calibrate-apply.yaml"),
}


# @spec INFRA-TEST-001
def _sam_loader() -> type[yaml.SafeLoader]:
    """SAM's ``!Ref`` / ``!GetAtt`` / ``!Sub`` short tags are not YAML PyYAML knows."""

    class SamLoader(yaml.SafeLoader):
        pass

    def passthrough(loader, tag_suffix, node):
        if isinstance(node, yaml.ScalarNode):
            return loader.construct_scalar(node)
        if isinstance(node, yaml.SequenceNode):
            return loader.construct_sequence(node, deep=True)
        return loader.construct_mapping(node, deep=True)

    SamLoader.add_multi_constructor("!", passthrough)
    return SamLoader


def load_template(path: Path) -> dict:
    if not path.exists():
        pytest.fail(f"template not found: {path.relative_to(ROOT)}")
    return yaml.load(path.read_text(), Loader=_sam_loader())


def resources_of_type(template: dict, type_name: str) -> dict:
    return {
        name: body
        for name, body in (template.get("Resources") or {}).items()
        if body.get("Type") == type_name
    }


def the_resource(template: dict, type_name: str) -> dict:
    """The single resource of a type, failing when there is not exactly one."""
    found = resources_of_type(template, type_name)
    assert len(found) == 1, f"expected exactly one {type_name}, found {sorted(found)}"
    return next(iter(found.values()))


def statements(policy_document: dict) -> list[dict]:
    statement = policy_document.get("Statement") or []
    return statement if isinstance(statement, list) else [statement]


def as_list(value) -> list:
    return value if isinstance(value, list) else [value]


def settles_to(template: dict, value):
    """What a template value becomes once default parameters are applied.

    A value read out of a parsed template is a literal, a parameter name (what ``!Ref`` leaves
    behind), or a ``!Sub`` string carrying ``${Parameter}`` placeholders. These tests care about
    the value that settles out, not which of the three spellings produced it.
    """
    defaults = {
        name: body.get("Default") for name, body in (template.get("Parameters") or {}).items()
    }
    if not isinstance(value, str):
        return value
    if value in defaults:
        return defaults[value]
    return re.sub(
        r"\$\{([A-Za-z0-9:]+)\}",
        lambda match: str(defaults.get(match.group(1), match.group(0))),
        value,
    )


@pytest.fixture(scope="session")
def foundation() -> dict:
    return load_template(FOUNDATION_TEMPLATE)


@pytest.fixture(scope="session")
def dispatch() -> dict:
    return load_template(DISPATCH_TEMPLATE)


@pytest.fixture(scope="session")
def handler() -> ModuleType:
    """The Lambda handler, imported by path — it lives outside the ``aqdt`` package on purpose."""
    if not HANDLER.exists():
        pytest.fail(f"handler not found: {HANDLER.relative_to(ROOT)}")
    spec = importlib.util.spec_from_file_location("aqdt_dispatch_handler", HANDLER)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module
