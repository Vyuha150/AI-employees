"""Parts 21-23: packaged resources, configuration loader, placeholder gate, CLI outside a checkout."""

import os
import shutil
import subprocess
import sys
import venv
from pathlib import Path

import pytest
import yaml

from command_center.config import Environment, load_config, validate_config
from command_center.errors import ConfigurationError
from command_center.resources import default_config_dir, read_text, resource

ROOT = Path(__file__).resolve().parents[1]
PKG_CONFIG = ROOT / "src" / "command_center" / "resources" / "config"


def org_config(tmp_path: Path, *, strip_placeholders: bool) -> Path:
    """A copy of the packaged config as an organization would deploy it."""
    d = tmp_path / "orgcfg"
    shutil.copytree(PKG_CONFIG, d)
    pol = yaml.safe_load((d / "organization_policy.example.yaml").read_text())
    (d / "organization_policy.example.yaml").unlink()
    if strip_placeholders:
        pol["placeholders"] = []
        for f in (d / "agents").glob("*.yaml"):
            raw = yaml.safe_load(f.read_text())
            raw["placeholders"] = []
            f.write_text(yaml.safe_dump(raw, sort_keys=False))
    (d / "organization_policy.yaml").write_text(yaml.safe_dump(pol, sort_keys=False))
    return d


def test_resources_load_via_importlib():
    assert "{{AGENT_PROFILE}}" in read_text("prompts", "core_operating_principles.md")
    assert resource("schemas", "weekly_review.schema.json").is_file()
    assert len([n for n in default_config_dir().joinpath("agents").iterdir() if n.name.endswith(".yaml")]) == 12


def test_environment_must_be_explicit():
    report = validate_config(env={})
    assert not report.valid and any("environment mode not set" in e for e in report.errors)
    with pytest.raises(ConfigurationError, match="invalid environment"):
        load_config(environment="staging", env={})


def test_development_accepts_placeholders_with_warnings():
    cfg = load_config(environment="development", env={})
    assert cfg.environment is Environment.DEVELOPMENT and cfg.using_example_policy
    report = validate_config(environment="development", env={})
    assert report.valid and report.placeholders


def test_production_refuses_placeholders_and_example_policy():
    report = validate_config(environment="production", env={"ANTHROPIC_API_KEY": "x"})
    assert not report.valid
    assert any("refuses placeholder value" in e and "create_purchase_order.red_at" in e for e in report.errors)
    assert any("organization_policy.example.yaml" in e for e in report.errors)
    with pytest.raises(ConfigurationError):
        load_config(environment="production", env={"ANTHROPIC_API_KEY": "x"})


def test_production_valid_once_reviewed(tmp_path):
    d = org_config(tmp_path, strip_placeholders=True)
    assert not validate_config(d, "production", env={}).valid          # credential missing
    cfg = load_config(d, "production", env={"ANTHROPIC_API_KEY": "sk-ant-not-a-real-key-123456"})
    assert cfg.environment is Environment.PRODUCTION and not cfg.using_example_policy
    assert "sk-ant" not in repr(cfg)


def test_secrets_in_config_files_are_rejected_and_never_printed(tmp_path):
    d = org_config(tmp_path, strip_placeholders=False)
    models = yaml.safe_load((d / "models.yaml").read_text())
    models["providers"]["anthropic"]["api_key"] = "sk-ant-api03-SECRETSECRETSECRET"
    (d / "models.yaml").write_text(yaml.safe_dump(models))
    report = validate_config(d, "development", env={"ANTHROPIC_API_KEY": "sk-ant-envsecret-999999"})
    assert not report.valid and any("looks like a secret" in e for e in report.errors)
    rendered = report.render()
    assert "SECRETSECRET" not in rendered and "envsecret" not in rendered
    assert "ANTHROPIC_API_KEY: set" in rendered


def test_unresolved_references_reported(tmp_path):
    d = org_config(tmp_path, strip_placeholders=False)
    cfo = yaml.safe_load((d / "agents" / "cfo.yaml").read_text())
    cfo["model_profile"] = "turbo"
    cfo["schedule_policy"]["jobs"][0]["workflow"] = "astrology_review"
    (d / "agents" / "cfo.yaml").write_text(yaml.safe_dump(cfo, sort_keys=False))
    pol = yaml.safe_load((d / "organization_policy.yaml").read_text())
    pol["escalations"][1]["escalate_to"] = "ghost_agent"
    (d / "organization_policy.yaml").write_text(yaml.safe_dump(pol, sort_keys=False))
    r = validate_config(d, "development", env={})
    text = "\n".join(r.unresolved)
    assert "model_profile 'turbo'" in text and "astrology_review" in text and "ghost_agent" in text
    assert not r.valid


def test_unsupported_env_overrides_are_ignored_and_reported():
    r = validate_config(environment="test", env={"CC_RISK_RED_AT": "1"})
    assert any("CC_RISK_RED_AT" in w for w in r.warnings)


def test_supported_model_profile_override_must_exist():
    assert not validate_config(environment="test", env={"CC_DEFAULT_MODEL_PROFILE": "nope"}).valid
    assert validate_config(environment="test", env={"CC_DEFAULT_MODEL_PROFILE": "deep"}).valid


def test_placeholder_marker_must_point_at_a_field(tmp_path):
    d = org_config(tmp_path, strip_placeholders=False)
    pol = yaml.safe_load((d / "organization_policy.yaml").read_text())
    pol["placeholders"].append("risk.nonexistent.field")
    (d / "organization_policy.yaml").write_text(yaml.safe_dump(pol, sort_keys=False))
    assert not validate_config(d, "development", env={}).valid


def _cli(args, cwd, extra_env=None, python=sys.executable, script=None):
    env = {k: v for k, v in os.environ.items() if not k.startswith("CC_")}
    env.pop("PYTHONPATH", None)
    env.update(extra_env or {})
    cmd = [script] + args if script else [python, "-m", "command_center.cli"] + args
    return subprocess.run(cmd, cwd=cwd, env=env, capture_output=True, text=True, timeout=120)


def test_cli_works_from_another_directory(tmp_path):
    out = _cli(["config", "validate", "--env", "development"], tmp_path,
               {"PYTHONPATH": str(ROOT / "src")})
    assert out.returncode == 0, out.stderr
    assert "configuration: VALID" in out.stdout and "packaged defaults" in out.stdout
    bad = _cli(["config", "validate", "--env", "production"], tmp_path, {"PYTHONPATH": str(ROOT / "src")})
    assert bad.returncode == 1 and "refuses placeholder" in bad.stdout


@pytest.mark.packaging
def test_wheel_installs_and_runs_outside_checkout(tmp_path):
    wheel_dir = tmp_path / "wheel"
    build = subprocess.run([sys.executable, "-m", "pip", "wheel", "--no-deps", "-q", "-w", str(wheel_dir), str(ROOT)],
                           capture_output=True, text=True, timeout=600)
    if build.returncode != 0:
        pytest.skip(f"wheel build unavailable here: {build.stderr[-300:]}")
    wheel = next(wheel_dir.glob("*.whl"))
    env_dir = tmp_path / "venv"
    venv.EnvBuilder(with_pip=True, system_site_packages=True).create(env_dir)
    py = env_dir / "bin" / "python"
    inst = subprocess.run([str(py), "-m", "pip", "install", "--no-deps", "-q", str(wheel)], capture_output=True,
                          text=True, timeout=600)
    assert inst.returncode == 0, inst.stderr
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    check = subprocess.run([str(py), "-c", "import command_center, sys; from command_center.resources import resource;"
                            "assert 'site-packages' in command_center.__file__, command_center.__file__;"
                            "assert resource('migrations','0001_core.sql').is_file(); print('ok')"],
                           cwd=elsewhere, capture_output=True, text=True, env={"PATH": os.environ["PATH"]})
    assert check.returncode == 0 and "ok" in check.stdout, check.stderr
    out = _cli(["config", "validate", "--env", "development"], elsewhere, script=str(env_dir / "bin" / "ai-command-center"))
    assert out.returncode == 0 and "configuration: VALID" in out.stdout, out.stdout + out.stderr
