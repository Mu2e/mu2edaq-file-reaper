"""The daemon's argument/config layering (mu2edaq_file_reaper.cli)."""

import pytest

from mu2edaq_file_reaper import cli
from mu2edaq_file_reaper.settings import ENV_PREFIX


def write_cfg(tmp_path, body):
    p = tmp_path / "r.yaml"
    p.write_text(body)
    return str(p)


def test_precedence_cli_over_env_over_dotenv_over_yaml(tmp_path, monkeypatch):
    area = tmp_path / "a" / "b"
    area.mkdir(parents=True)
    cfg = write_cfg(tmp_path, f"reaper:\n  web_port: 6001\n  label: yaml\n  scan_interval: 11\nareas:\n  - path: {area}\n")
    (tmp_path / ".env").write_text(f"{ENV_PREFIX}LABEL=dotenv\n{ENV_PREFIX}SCAN_INTERVAL=22\n")
    monkeypatch.setenv(ENV_PREFIX + "SCAN_INTERVAL", "33")
    monkeypatch.delenv(ENV_PREFIX + "CONFIG", raising=False)
    monkeypatch.chdir(tmp_path)
    settings, args = cli.load_settings(["--config", cfg, "--port", "7000"])
    assert settings.web_port == 7000            # CLI wins
    assert settings.scan_interval == 33         # env beats .env
    assert settings.label == "dotenv"           # .env beats yaml
    assert settings.config_path == cfg and settings.env_file.endswith(".env")
    assert len(settings.areas) == 1 and settings.areas[0].tiers.keys() == {"warning", "critical", "full"}


def test_missing_explicit_config_is_fatal(tmp_path, monkeypatch):
    monkeypatch.delenv(ENV_PREFIX + "CONFIG", raising=False)
    with pytest.raises(SystemExit):
        cli.load_settings(["--config", str(tmp_path / "nope.yaml")])


def test_default_config_may_be_absent(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv(ENV_PREFIX + "CONFIG", raising=False)
    settings, _ = cli.load_settings([])
    assert settings.config_path is None and settings.areas == []


def test_check_config_exit_codes(tmp_path, monkeypatch, capsys):
    area = tmp_path / "a" / "b"
    area.mkdir(parents=True)
    good = write_cfg(tmp_path, f"areas:\n  - path: {area}\n")
    monkeypatch.delenv(ENV_PREFIX + "CONFIG", raising=False)
    with pytest.raises(SystemExit) as exc:
        cli.main(["--config", good, "--check-config"])
    assert exc.value.code == 0
    assert "area " in capsys.readouterr().out
    bad = write_cfg(tmp_path, "areas:\n  - path: /etc\n")
    with pytest.raises(SystemExit) as exc:
        cli.main(["--config", bad, "--check-config"])
    assert exc.value.code == 1


def test_version_flag(capsys):
    with pytest.raises(SystemExit) as exc:
        cli.main(["--version"])
    assert exc.value.code == 0
