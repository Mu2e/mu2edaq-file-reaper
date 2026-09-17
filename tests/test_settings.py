import os

from mu2edaq_file_reaper.settings import ENV_PREFIX, load_env_file, redact_url


def test_apply_skips_none_and_env_layer(settings):
    settings.apply(web_port=None, label="x")
    assert settings.web_port == 5004 and settings.label == "x"
    issues = settings.apply_env({ENV_PREFIX + "WEB_PORT": "6000", ENV_PREFIX + "DRY_RUN": "yes",
                                 ENV_PREFIX + "WORKERS": "abc", ENV_PREFIX + "DAEMON": "maybe"})
    assert settings.web_port == 6000 and settings.dry_run is True
    assert len(issues) == 2


def test_as_dict_redacts(settings):
    settings.apply(admin_token="s3cret", database_url="postgresql+psycopg://u:pw@h/db")
    d = settings.as_dict()
    assert d["admin_token"] == "***" and d["database_url"] == "postgresql+psycopg://u:***@h/db"
    assert redact_url("sqlite:///x.db") == "sqlite:///x.db"


def test_load_env_file(tmp_path, monkeypatch):
    p = tmp_path / ".env"
    p.write_text('# comment\nexport A=1\nB="two words"\nC=\'x\'\nBAD LINE\nD=already\n')
    monkeypatch.setenv("D", "env-wins")
    vals = load_env_file(str(p))
    assert vals == {"A": "1", "B": "two words", "C": "x"}
    assert load_env_file(str(tmp_path / "missing")) == {}
