import glob
import os
import re
import subprocess
import sys

import pytest

import mu2edaq_file_reaper
from mu2edaq_file_reaper import __version__

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def test_version_is_semver():
    assert re.fullmatch(r"\d+\.\d+\.\d+", __version__)


def test_pyproject_declares_scripts_and_version_source():
    text = open(os.path.join(ROOT, "pyproject.toml")).read()
    assert 'mu2edaq-file-reaper = "mu2edaq_file_reaper.cli:main"' in text
    assert 'mu2edaq-reaper = "mu2edaq_reaper_cli.cli:main"' in text
    assert 'version = { attr = "mu2edaq_file_reaper.__version__" }' in text
    assert 'requires-python = ">=3.9"' in text


def test_shim_and_scripts_exist():
    for name in ("file_reaper.py", "bootstrap.sh", "start-mu2edaq-file-reaper.sh",
                 "stop-mu2edaq-file-reaper.sh", "lib/file-reaper-proc.sh", "CMakeLists.txt",
                 "config/mu2edaq-file-reaper.yaml", "LICENSE", "README.md", "CHANGELOG.md"):
        assert os.path.exists(os.path.join(ROOT, name)), name


def test_man_pages_carry_current_version():
    pages = glob.glob(os.path.join(ROOT, "man", "man*", "*.[13578]"))
    assert pages, "no man pages"
    for page in pages:
        head = open(page, encoding="utf-8", errors="replace").read(2000)
        th = re.search(r'^\.TH\s+(\S+)\s+(\d)\s+"[^"]*"\s+"([^"]*)"', head, re.M)
        assert th, f"{page}: missing .TH"
        assert __version__ in th.group(3), f"{page}: .TH version {th.group(3)!r} != {__version__}"


def test_changelog_mentions_version():
    text = open(os.path.join(ROOT, "CHANGELOG.md"), encoding="utf-8").read()
    assert re.search(rf"^##\s+\[.*\].*{re.escape(__version__)}", text, re.M), "CHANGELOG has no heading for __version__"


def test_nav_endpoints_exist_and_templates_reachable():
    from mu2edaq_file_reaper.web import create_app
    from mu2edaq_file_reaper.web.nav import NAV_ITEMS
    app = create_app()
    endpoints = {r.endpoint for r in app.url_map.iter_rules()}
    for ep, _label, _icon in NAV_ITEMS:
        assert ep in endpoints, ep
    tdir = os.path.join(os.path.dirname(mu2edaq_file_reaper.__file__), "web", "templates")
    assert os.path.isfile(os.path.join(tdir, "base.html"))
    sdir = os.path.join(os.path.dirname(mu2edaq_file_reaper.__file__), "web", "static")
    assert glob.glob(os.path.join(sdir, "*.js")) and glob.glob(os.path.join(sdir, "*.css"))


def test_python39_typing_syntax():
    """No `X | None` unions or other 3.10+ syntax in the sources."""
    bad = []
    for path in glob.glob(os.path.join(ROOT, "src", "**", "*.py"), recursive=True):
        src = open(path, encoding="utf-8").read()
        if re.search(r"\b(?:int|str|float|bool|dict|list|Dict|List|[A-Z]\w+)\s*\|\s*None\b", src):
            bad.append(path)
        if "match " in src and re.search(r"^\s*match\s+\w+\s*:", src, re.M):
            bad.append(path + " (match statement)")
    assert not bad, bad


def test_module_help_and_version_run():
    env = dict(os.environ, PYTHONPATH=os.path.join(ROOT, "src"))
    r = subprocess.run([sys.executable, "-m", "mu2edaq_file_reaper", "--version"], env=env,
                       capture_output=True, text=True, cwd=ROOT)
    assert r.returncode == 0 and __version__ in r.stdout + r.stderr
    r = subprocess.run([sys.executable, os.path.join(ROOT, "file_reaper.py"), "--help"], env=env,
                       capture_output=True, text=True, cwd=ROOT)
    assert r.returncode == 0 and "--scan-interval" in r.stdout
