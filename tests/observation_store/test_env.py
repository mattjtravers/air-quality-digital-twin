"""Configuration, devcontainer, CI, packaging — OBS-ENV."""

import importlib.metadata
from pathlib import Path

import pytest

from aqdt.observation_store.archive import read_observations, write_observations

from .conftest import make_observation

ROOT = Path(__file__).resolve().parents[2]


# @spec OBS-ENV-001
def test_no_configuration_files_in_the_repository():
    for name in (".env", "config.yaml", "config.yml", "config.toml", "settings.yaml", ".aqdt"):
        assert not (ROOT / name).exists(), name
    source = "\n".join(p.read_text() for p in (ROOT / "src" / "aqdt").rglob("*.py"))
    assert "dotenv" not in source
    assert "configparser" not in source


# @spec OBS-ENV-001
# @spec OBS-ENV-002
def test_archive_uri_defaults_to_the_environment_variable(tmp_path, monkeypatch):
    monkeypatch.setenv("AQDT_ARCHIVE_URI", str(tmp_path / "from-env"))
    write_observations([make_observation()])
    assert (tmp_path / "from-env").exists()
    assert len(read_observations()) == 1

    monkeypatch.delenv("AQDT_ARCHIVE_URI")
    with pytest.raises(Exception, match="AQDT_ARCHIVE_URI"):
        write_observations([make_observation()])
    with pytest.raises(Exception, match="AQDT_ARCHIVE_URI"):
        read_observations()


# @spec OBS-ENV-003
def test_devcontainer_runs_a_postgis_sidecar_via_docker_compose():
    devcontainer = (ROOT / ".devcontainer" / "devcontainer.json").read_text()
    assert "dockerComposeFile" in devcontainer
    compose = (ROOT / ".devcontainer" / "docker-compose.yml").read_text()
    assert "postgis/postgis" in compose
    assert "DATABASE_URL" in compose


# @spec OBS-ENV-004
def test_post_create_syncs_applies_schema_and_rebuilds():
    devcontainer = (ROOT / ".devcontainer" / "devcontainer.json").read_text()
    assert "postCreateCommand" in devcontainer
    script = (ROOT / ".devcontainer" / "post-create.sh").read_text()
    assert "uv sync --all-groups" in script
    assert "apply_schema" in script
    assert "rebuild" in script


# @spec OBS-ENV-005
def test_archive_tests_use_local_directories_and_an_in_process_s3_mock():
    conftest = (Path(__file__).parent / "conftest.py").read_text()
    assert "moto" in conftest
    assert "AWS_ENDPOINT_URL" in conftest
    workflow = (ROOT / ".github" / "workflows" / "ci.yaml").read_text()
    assert "AWS_ACCESS_KEY_ID" not in workflow.replace("AWS_ACCESS_KEY_ID: testing", "")


# @spec OBS-ENV-007
def test_ci_provides_a_postgis_service_and_database_url():
    workflow = (ROOT / ".github" / "workflows" / "ci.yaml").read_text()
    assert "postgis/postgis" in workflow
    assert "DATABASE_URL" in workflow
    assert "services:" in workflow


# @spec OBS-ENV-008
def test_installed_as_aqdt_from_src_layout():
    import aqdt

    assert Path(aqdt.__file__).resolve().parent == ROOT / "src" / "aqdt"
    assert importlib.metadata.distribution("air-quality-digital-twin") is not None
